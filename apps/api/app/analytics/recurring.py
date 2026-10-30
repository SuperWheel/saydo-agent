"""订阅 / 周期扣费识别（F-SB-01，技术文档 §7.3）。

按商户分组（金额差异大的套餐再按金额聚类拆分），对每个周期算：
    p = 相邻间隔落在周期窗口内的比例
    a = 金额在容差 max(2 元, 中位金额 × 10%) 内的比例
    n = min(1, 笔数 / 3)            # 年付 2 笔可成为候选，但不算成熟
    m = 商户归一化可信度（登记商户 1.0，未登记 0.5）
    confidence = 0.40p + 0.30a + 0.20n + 0.10m
≥ 0.80 记为订阅；0.65–0.80 记为待确认（问用户「这是周期付款吗？」）。

公式之外的三条硬条件，防止误报：
- PRD 对订阅的定义是「同一商户、金额接近、间隔规律」，所以 p、a 都要 ≥ 0.6：每周买菜（间隔规律、金额分散）不算；
- 只看消费：转账（如每月给家人的生活费）和收入不参与；
- 房租这类居住支出虽然周期固定，但不是订阅，单独列为「已排除」。

输入是 analytics/engine.py 的 Txn：商户只用可信标签（登记商户规范名或「未登记商户」）和归并键，原文不进主模型。
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median

from sqlalchemy.orm import Session

from ..agent import quarantine
from . import engine
from .engine import KIND_SPEND, Txn

LOOKBACK_DAYS = 400  # 年付要看到上一年同期
SUBSCRIPTION_AT, ASK_AT = 0.80, 0.65
MIN_P, MIN_A = 0.6, 0.6
EXCLUDED_CATEGORIES = {"居住": "房租等居住支出，周期固定但不算订阅"}


@dataclass(frozen=True)
class Cycle:
    key: str
    label: str  # 每月 / 每年 …
    per_year: int
    days: int = 0  # 按天计的周期（周、双周）
    months: int = 0  # 按日历月计的周期（月、季、年）
    tol_days: int = 0
    lead_days: tuple[int, ...] = (3,)  # 续费提醒提前量


CYCLES = (
    Cycle("weekly", "每周", 52, days=7, tol_days=2, lead_days=(1,)),
    Cycle("biweekly", "每两周", 26, days=14, tol_days=3, lead_days=(2,)),
    Cycle("monthly", "每月", 12, months=1, tol_days=3, lead_days=(3,)),
    Cycle("quarterly", "每季度", 4, months=3, tol_days=5, lead_days=(7, 3)),
    Cycle("yearly", "每年", 1, months=12, tol_days=7, lead_days=(14, 3)),
)


@dataclass
class Recurring:
    party: str  # 可信标签：登记商户规范名或「未登记商户」，可以给主模型看
    display: str  # 卡片展示名，只进前端卡片
    merchant_key: str
    registered: bool
    cycle: Cycle
    txns: list[Txn]
    amount_fen: int  # 最近一期金额
    confidence: float
    signals: dict  # p / a / n / m
    next_due: date
    overdue: bool = False  # 按周期本该已经扣费，却没看到扣款（可能已停止）
    price_up_from_fen: int | None = None  # 最近一期比上一期贵：上一期金额
    status: str = "subscription"  # subscription / ask

    @property
    def yearly_fen(self) -> int:
        return self.amount_fen * self.cycle.per_year

    @property
    def last_at(self) -> datetime:
        return self.txns[-1].at

    @property
    def txn_ids(self) -> list[int]:
        return [t.id for t in self.txns]


@dataclass
class Detection:
    subscriptions: list[Recurring] = field(default_factory=list)
    ask: list[Recurring] = field(default_factory=list)  # 0.65–0.80：需要用户确认
    excluded: list[dict] = field(default_factory=list)  # 周期固定但不算订阅的（房租）

    def txn_ids(self) -> set[int]:
        return {i for r in self.subscriptions for i in r.txn_ids}


# ---------- 日期 ----------

def _last_day(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def add_months(d: date, months: int, anchor_day: int) -> date:
    """按日历月推算：扣费日按 anchor_day，月份没有这一天时对齐月末（31 号扣费的，2 月就是月末那天）。"""
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(anchor_day, _last_day(y, m)))


def _anchor(dates: list[date]) -> int:
    """扣费日号：取最常见的那天；出现过月末扣费的，按最大日号算（月末对齐）。"""
    days = [d.day for d in dates]
    if any(d.day == _last_day(d.year, d.month) and d.day < 31 for d in dates):
        return max(days)
    return max(set(days), key=lambda x: (days.count(x), x))


def _next(d: date, cycle: Cycle, anchor_day: int) -> date:
    return d + timedelta(days=cycle.days) if cycle.days else add_months(d, cycle.months, anchor_day)


def _fits(a: date, b: date, cycle: Cycle, anchor_day: int) -> bool:
    return abs((b - _next(a, cycle, anchor_day)).days) <= cycle.tol_days


# ---------- 识别 ----------

def _tol(amount_fen: int) -> int:
    return max(200, amount_fen // 10)


def _clusters(txns: list[Txn]) -> list[list[Txn]]:
    """金额差异大的套餐按金额拆开（如同一商户的月卡和年卡）。金额本身就很分散的（买菜、外卖）不拆，整体评估。"""
    by_amount = sorted(txns, key=lambda t: t.amount_fen)
    groups: list[list[Txn]] = []
    for t in by_amount:
        if groups and t.amount_fen - groups[-1][0].amount_fen <= _tol(groups[-1][0].amount_fen):
            groups[-1].append(t)
        else:
            groups.append([t])
    if len(groups) == 1:
        return [txns]
    big = [g for g in groups if len(g) >= 2]
    if len(groups) <= 3 and sum(len(g) for g in big) >= 0.8 * len(txns):
        return [sorted(g, key=lambda t: t.at) for g in big]
    return [txns]


def _evaluate(group: list[Txn], today: date) -> Recurring | None:
    if len(group) < 2:
        return None
    dates = [t.at.date() for t in group]
    amounts = [t.amount_fen for t in group]
    med = int(median(amounts))
    a = sum(abs(x - med) <= _tol(med) for x in amounts) / len(amounts)
    n = min(1.0, len(group) / 3)
    registered = group[0].party != quarantine.UNREGISTERED
    m = 1.0 if registered else 0.5
    anchor = _anchor(dates)
    best: tuple[float, float, Cycle] | None = None
    for cycle in CYCLES:
        p = sum(_fits(x, y, cycle, anchor) for x, y in zip(dates, dates[1:])) / (len(dates) - 1)
        conf = round(0.40 * p + 0.30 * a + 0.20 * n + 0.10 * m, 2)
        if best is None or (p, conf) > (best[1], best[0]):
            best = (conf, p, cycle)
    conf, p, cycle = best
    if p < MIN_P or a < MIN_A or conf < ASK_AT:
        return None
    nxt = _next(dates[-1], cycle, anchor)
    overdue = False
    while nxt < today - timedelta(days=cycle.tol_days):  # 本该扣费的日子过了还没扣：顺延到下一期，并标记
        overdue = True
        nxt = _next(nxt, cycle, anchor)
    last, prev = amounts[-1], amounts[-2]
    t0 = group[-1]
    return Recurring(
        party=t0.party, display=t0.display, merchant_key=t0.merchant_key or "", registered=registered, cycle=cycle,
        txns=group, amount_fen=last, confidence=conf, signals={"p": round(p, 2), "a": round(a, 2), "n": round(n, 2), "m": m},
        next_due=nxt, overdue=overdue, price_up_from_fen=prev if last > prev + _tol(prev) else None,
        status="subscription" if conf >= SUBSCRIPTION_AT else "ask",
    )


def detect(txns: list[Txn], at: datetime) -> Detection:
    """txns 是截止 at 的流水（建议覆盖 LOOKBACK_DAYS 天）。结果按下次扣费日排序。"""
    groups: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.kind == KIND_SPEND and t.merchant_key and t.at < at:
            groups[t.merchant_key].append(t)
    out = Detection()
    today = at.date()
    for key, ts in groups.items():
        ts.sort(key=lambda t: (t.at, t.id))
        for g in _clusters(ts):
            r = _evaluate(g, today)
            if r is None:
                continue
            if reason := EXCLUDED_CATEGORIES.get(g[-1].category):
                out.excluded.append({"party": r.party, "display": r.display, "reason": reason, "cycle": r.cycle.label,
                                     "amount_fen": r.amount_fen})
                continue
            (out.subscriptions if r.status == "subscription" else out.ask).append(r)
    out.subscriptions.sort(key=lambda r: (r.next_due, r.party))
    out.ask.sort(key=lambda r: (r.next_due, r.party))
    return out


def load_detection(db: Session, user_id: int, at: datetime) -> Detection:
    """读出截止 at 的近 LOOKBACK_DAYS 天流水并识别。账单报告的「订阅」口径也用它（tools/bill.py）。"""
    return detect(engine.load(db, user_id, at - timedelta(days=LOOKBACK_DAYS), at), at)
