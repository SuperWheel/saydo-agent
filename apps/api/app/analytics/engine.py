"""账单分析引擎（F-BA-02 ~ 05）。

- 分类：自动代扣 → MCC → 商户名关键词（确定性规则），都分不出时才交给隔离模型（只回枚举）。
- 商户名、备注、来账户名只经过 agent/quarantine.py 处理；这里产出的 Txn 只带可信标签（party）
  和给卡片用的展示名（display），后者不进主模型。
- 所有金额是整数分；占比用最大余数法，保证各项加起来正好 100.0%。
"""

import hashlib
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent import quarantine
from ..core_mock.merchants import RULES_VERSION, mcc_category
from ..models import Account, Contact, Transaction
from ..money import fmt_yuan
from .periods import Period

KIND_SPEND, KIND_TRANSFER, KIND_INCOME = "消费", "转账", "收入"
BASELINE_DAYS = 90


@dataclass
class Txn:
    id: int
    txn_no: str
    at: datetime
    direction: str
    amount_fen: int
    kind: str
    category: str
    cat_source: str  # 自动代扣 / MCC / 关键词 / 隔离模型 / 待确认 / 交易码 / 收款人
    party: str  # 可信标签，可以给主模型看：登记商户规范名、「未登记商户」、脱敏户名
    display: str  # 卡片展示名（外部文本规范化 + 脱敏 + 截断），只进前端卡片
    merchant_key: str | None  # 同一商户的归并键，只在系统内部用
    auto: bool = False  # 自动代扣
    cross_border: bool = False
    contact_id: int | None = None
    untrusted_flags: list[str] = field(default_factory=list)  # 备注 / 商户名里的注入特征，只记审计


def _mask_name(raw: str | None) -> str:
    s = quarantine.normalize(raw)
    if not s:
        return "对方"
    if len(s) <= 2:
        return s[0] + "*"
    return s[0] + "*" * min(len(s) - 2, 4) + s[-1]


def _spend_rule(t: Transaction) -> tuple[str, str] | None:
    if t.channel == "auto":
        return "订阅", "自动代扣"
    if cat := mcc_category(t.mcc):
        return cat, "MCC"
    if cat := quarantine.keyword_category(t.merchant):
        return cat, "关键词"
    return None


def coverage_start(db: Session, user_id: int) -> datetime | None:
    """有流水的最早时间（不含期初余额），用来判断环比的上一期数据是否完整。"""
    return db.scalar(select(Transaction.created_at).join(Account, Account.id == Transaction.account_id)
                     .where(Account.user_id == user_id, Transaction.channel != "system")
                     .order_by(Transaction.created_at).limit(1))


def load(db: Session, user_id: int, start: datetime, end: datetime, *,
         before_llm: Callable[[], None] | None = None) -> list[Txn]:
    """读出 [start, end) 内已入账的交易并分类。期初余额不算交易。

    before_llm：要调用隔离模型之前执行（主循环用它结束事务，避免调模型时占着数据库写锁）。
    """
    rows = db.scalars(
        select(Transaction).join(Account, Account.id == Transaction.account_id)
        .where(Account.user_id == user_id, Transaction.status == "succeeded", Transaction.channel != "system",
               Transaction.created_at >= start, Transaction.created_at < end)
        .order_by(Transaction.created_at, Transaction.id)
    ).all()
    contacts = {c.id: c for c in db.scalars(select(Contact).where(Contact.user_id == user_id))}
    out: list[Txn] = []
    pending: list[tuple[Txn, Transaction]] = []
    for t in rows:
        flags = sorted(set(quarantine.scan(t.memo)) | set(quarantine.scan(t.merchant)))
        base = dict(id=t.id, txn_no=t.txn_no, at=t.created_at, direction=t.direction, amount_fen=t.amount_fen,
                    untrusted_flags=flags)
        if t.direction == "in":
            cat = "工资" if t.category == "工资" else "转入"
            name = _mask_name(t.counterparty)
            out.append(Txn(**base, kind=KIND_INCOME, category=cat, cat_source="交易码", party=name,
                           display=quarantine.display_name(t.counterparty), merchant_key=None))
        elif t.category == "理财":  # 理财申购（WS5）：本人资金换成理财份额，不算消费（技术文档 §7.4）
            out.append(Txn(**base, kind=KIND_TRANSFER, category="理财", cat_source="交易码", party="理财申购",
                           display=quarantine.display_name(t.counterparty), merchant_key=None))
        elif t.contact_id is not None:
            c = contacts.get(t.contact_id)
            name = f"{c.masked_name}（{c.relation or c.note}）" if c and (c.relation or c.note) else (
                c.masked_name if c else _mask_name(t.counterparty))
            out.append(Txn(**base, kind=KIND_TRANSFER, category="转账", cat_source="收款人", party=name, display=name,
                           merchant_key=None, contact_id=t.contact_id))
        else:
            reg = quarantine.registered(t.merchant)
            x = Txn(**base, kind=KIND_SPEND, category="其他", cat_source="待确认",
                    party=reg or quarantine.UNREGISTERED, display=quarantine.display_name(t.merchant),
                    merchant_key=(reg or quarantine.normalize(t.merchant)).lower() or None, auto=t.channel == "auto",
                    cross_border=quarantine.cross_border(t.merchant))
            if rule := _spend_rule(t):
                x.category, x.cat_source = rule
            else:
                pending.append((x, t))
            out.append(x)
    if pending:
        _classify_pending(pending, before_llm)
    return out


def _classify_pending(pending: list[tuple[Txn, Transaction]], before_llm) -> None:
    keys: dict[str, list[tuple[Txn, Transaction]]] = defaultdict(list)
    for x, t in pending:
        keys[x.merchant_key or ""].append((x, t))
    if quarantine.llm_available() and before_llm:
        before_llm()
    for i, group in enumerate(keys.values()):
        if i >= quarantine.MAX_LLM_CALLS:
            break  # 超出本次额度的保持「其他 / 待确认」
        x0, t0 = group[0]
        v = quarantine.classify_untrusted(t0.merchant, t0.memo)
        if v.source == "quarantine_llm":
            for x, _ in group:
                x.category, x.cat_source = v.category, "隔离模型"


def snapshot_id(txns: list[Txn]) -> str:
    """统计快照：交易 ID 清单 + 分类规则版本的摘要，写进审计，方便复核报告用的是哪批数据。"""
    h = hashlib.sha256(RULES_VERSION.encode())
    for t in txns:
        h.update(f"{t.txn_no}|{t.category};".encode())
    return h.hexdigest()[:16]


# ---------- 格式化：占比、环比 ----------

def permilles(values: list[int]) -> list[int]:
    """最大余数法：各项千分比加起来正好 1000。"""
    total = sum(values)
    if total <= 0:
        return [0] * len(values)
    parts = [divmod(v * 1000, total) for v in values]
    out = [q for q, _ in parts]
    for i in sorted(range(len(values)), key=lambda i: -parts[i][1])[:1000 - sum(out)]:
        out[i] += 1
    return out


def pct(permille: int) -> str:
    return f"{permille // 10}.{permille % 10}%"


def ratio_pct(num: int, den: int) -> str:
    return f"{(Decimal(num) * 100 / Decimal(den)).quantize(Decimal('0.1'), ROUND_HALF_UP)}%"


def change(cur: int, prev: int, comparable: bool) -> dict:
    """环比：基数为 0 或上一期数据不完整时不给百分比。"""
    if not comparable:
        return {"change": "无可比基数（上一期数据不完整）"}
    if prev == 0:
        return {"change": "无可比基数"}
    d = (Decimal(cur - prev) * 100 / Decimal(prev)).quantize(Decimal("0.1"), ROUND_HALF_UP)
    diff = cur - prev
    return {"change": f"{d:+}%", "change_yuan": ("+" if diff >= 0 else "") + fmt_yuan(diff), "change_fen": diff}


def comparable(db: Session, user_id: int, period: Period) -> bool:
    start = coverage_start(db, user_id)
    return start is not None and start.date() <= period.prev_start.date()


# ---------- 聚合 ----------

def totals(txns: list[Txn]) -> dict:
    s = {KIND_SPEND: 0, KIND_TRANSFER: 0, KIND_INCOME: 0}
    for t in txns:
        s[t.kind] += t.amount_fen
    return {"income_fen": s[KIND_INCOME], "spend_fen": s[KIND_SPEND], "transfer_out_fen": s[KIND_TRANSFER],
            "net_fen": s[KIND_INCOME] - s[KIND_SPEND] - s[KIND_TRANSFER],
            "spend_count": sum(t.kind == KIND_SPEND for t in txns)}


def by_category(txns: list[Txn]) -> list[dict]:
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for t in txns:
        if t.kind == KIND_SPEND:
            agg[t.category][0] += t.amount_fen
            agg[t.category][1] += 1
    rows = sorted(({"category": c, "amount_fen": a, "count": n} for c, (a, n) in agg.items()),
                  key=lambda r: (-r["amount_fen"], r["category"]))
    for r, p in zip(rows, permilles([r["amount_fen"] for r in rows])):
        r["permille"] = p
    return rows


def pie_slices(rows: list[dict], min_permille: int = 30) -> list[dict]:
    """饼图切片：占比 < 3% 的类目并进「其他」（技术文档 §7.4）。占比沿用 by_category 的结果，不重新取整。"""
    big = [r for r in rows if r["permille"] >= min_permille and r["category"] != "其他"]
    small = [r for r in rows if r not in big]
    slices = [dict(r) for r in big]
    if small:
        slices.append({"category": "其他", "amount_fen": sum(r["amount_fen"] for r in small),
                       "count": sum(r["count"] for r in small), "permille": sum(r["permille"] for r in small),
                       "merged": [r["category"] for r in small if r["category"] != "其他"]})
    return slices


def top_merchants(txns: list[Txn], n: int = 5) -> list[dict]:
    agg: dict[str, dict] = {}
    for t in txns:
        if t.kind != KIND_SPEND or not t.merchant_key:
            continue
        a = agg.setdefault(t.merchant_key, {"party": t.party, "display": t.display, "amount_fen": 0, "count": 0})
        a["amount_fen"] += t.amount_fen
        a["count"] += 1
    return sorted(agg.values(), key=lambda a: (-a["amount_fen"], a["display"]))[:n]


# ---------- 异常识别（技术文档 §7.2）----------

W_RULE = {"大额": 0.35, "深夜": 0.20, "境外": 0.25, "新商户": 0.10, "高频": 0.25}
# 大额 = 单笔 ≥ max(下限, 个人同类 P95 × 2)。技术文档的起点是 3000 元；按种子测试集校准为 2000 元：
# 固定的绝对线容易被「压线」绕过（如 2999 元），主要靠个人基线的相对倍数
LARGE_FLOOR_FEN = 200_000
COLD_START_LARGE_FEN = 300_000  # 基线未建立时的绝对线（技术文档原值）
REPORT_AT, ALERT_AT = 0.50, 0.75


@dataclass
class Anomaly:
    txn: Txn
    signals: list[str]
    score: float
    reason: str
    related: list[Txn] = field(default_factory=list)  # 重复扣款时，被重复的那几笔

    @property
    def level(self) -> str:
        independent = len([s for s in self.signals if s != "金额偏离"])
        if "重复扣款" in self.signals or independent >= 3 or (self.score >= ALERT_AT and independent >= 2):
            return "需要核实"
        return "值得查看"


def _p95(sample: list[int]) -> int:
    s = sorted(sample)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def _robust_z(x: int, sample: list[int]) -> float:
    logs = [math.log1p(v / 100) for v in sample]
    med = median(logs)
    mad = median(abs(v - med) for v in logs)
    return (math.log1p(x / 100) - med) / (1.4826 * mad + 0.1)


def _fmt_time(at: datetime) -> str:
    return f"{at.month}月{at.day}日 {at:%H:%M}"


def detect(txns: list[Txn], start: datetime, end: datetime, data_start: datetime | None) -> list[Anomaly]:
    """在 [start, end) 的消费里找异常。txns 要包含 start 之前 90 天的流水作为基线。

    只引用真实命中的信号做解释，不下「已确认被盗刷」之类的结论。
    """
    spends = [t for t in txns if t.kind == KIND_SPEND]
    found: list[Anomaly] = []
    for i, t in enumerate(spends):
        if not start <= t.at < end:
            continue
        hist = [h for h in spends[:i] if h.at >= t.at - timedelta(days=BASELINE_DAYS)]
        # 基线：同商户 ≥ 3 笔用商户自己的，否则同类 ≥ 10 笔用类别的，再不够用全部消费
        same_m = [h.amount_fen for h in hist if h.merchant_key == t.merchant_key]
        same_c = [h.amount_fen for h in hist if h.category == t.category]
        base, base_label = ((same_m, "在该商户") if len(same_m) >= 3 else
                            (same_c, f"{t.category}类") if len(same_c) >= 10 else
                            ([h.amount_fen for h in hist], "") if len(hist) >= 10 else ([], ""))
        # 和该商户以往的扣款金额一致（如每月房租）：不算大额，也不算偏离；10 分钟内的重复扣款另有规则
        recurring = any(abs(a.amount_fen - t.amount_fen) <= t.amount_fen // 10 and t.at - a.at > timedelta(minutes=10)
                        for a in hist if a.merchant_key == t.merchant_key)
        # 流水不满 30 天时个人基线还没建立：只看绝对金额线，不算偏离和新商户
        mature = data_start is not None and t.at - data_start >= timedelta(days=30)
        floor = max(LARGE_FLOOR_FEN, _p95(base) * 2 if base else 0) if mature else COLD_START_LARGE_FEN
        sig: dict[str, str] = {}
        if not recurring and t.amount_fen >= floor:
            sig["大额"] = "单笔金额较大"
        stat = 0.0
        if mature and not recurring and len(base) >= 10:
            stat = min(1.0, max(0.0, (_robust_z(t.amount_fen, base) - 2) / 3))
            mult = Decimal(t.amount_fen) / Decimal(int(median(base)))
            if stat > 0 and mult >= 2:
                sig["金额偏离"] = f"是你近 {BASELINE_DAYS} 天{base_label or ''}单笔消费中位数的 " \
                                f"{mult.quantize(Decimal('0.1'), ROUND_HALF_UP)} 倍"
        if t.at.hour < 6 and not t.auto:
            manual = [h for h in hist if not h.auto]
            if not manual or sum(h.at.hour < 6 for h in manual) / len(manual) < 0.05:
                sig["深夜"] = f"发生在凌晨 {t.at:%H:%M}，你平时这个时段几乎没有消费"
        if t.cross_border:
            sig["境外"] = "是境外商户"
        if mature and t.merchant_key not in {h.merchant_key for h in hist}:
            sig["新商户"] = f"该商户近 {BASELINE_DAYS} 天第一次出现"
        burst = [h for h in hist if h.at >= t.at - timedelta(minutes=10)]
        if len(burst) + 1 >= 4:
            sig["高频"] = f"10 分钟内连续消费 {len(burst) + 1} 笔"
        dups = [h for h in hist if h.merchant_key == t.merchant_key and h.amount_fen == t.amount_fen
                and t.at - h.at <= timedelta(minutes=10)]

        rule = min(1.0, sum(w for k, w in W_RULE.items() if k in sig))
        score = round(0.65 * rule + 0.35 * stat, 3)
        if dups:
            gap = max(1, math.ceil((t.at - dups[0].at).total_seconds() / 60))
            score = max(score, 0.8)
            reason = (f"{_fmt_time(dups[0].at)} 起，{t.party}在 {gap} 分钟内扣款 {len(dups) + 1} 次，"
                      f"每次 {fmt_yuan(t.amount_fen)} 元，疑似重复扣款。")
            found = [a for a in found if a.txn not in dups]  # 同一组重复扣款只报一条
            found.append(Anomaly(t, ["重复扣款"], score, reason, related=dups))
            continue
        if score < REPORT_AT:
            continue
        reason = f"{_fmt_time(t.at)} 在{t.party}消费 {fmt_yuan(t.amount_fen)} 元：" + "；".join(sig.values()) + "。"
        found.append(Anomaly(t, list(sig), score, reason))
    return sorted(found, key=lambda a: (-a.score, a.txn.at))
