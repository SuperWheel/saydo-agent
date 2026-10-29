"""账单分析工具（F-BA-02 ~ 05）：交易查询、分类统计、异常识别、月度报告。全部只读（🟢），不出确认卡。

数字一律由 analytics/engine.py 算出，卡片按模板渲染，不经过模型；给模型的 data 只放算好的数字和可信标签，
模型复述时由事实校验兜底。商户名、备注这类外部文本只在隔离处理里用（agent/quarantine.py），
data 里只有登记商户的规范名或「未登记商户」，原文只出现在卡片上。
"""

import re
from datetime import timedelta

from .. import audit
from ..agent import quarantine
from ..analytics import engine
from ..analytics.engine import KIND_INCOME, KIND_SPEND, KIND_TRANSFER, Txn
from ..analytics.periods import Period, has_period, parse_period
from ..cards import SOURCE_CORE
from ..clock import now
from ..core_mock.merchants import MERCHANTS, RULES_VERSION, SPEND_CATEGORIES, merchant_by_alias
from ..money import fmt_yuan, parse_amount
from ..nlu import resolve_payee
from ..policy import Risk
from . import ToolContext, ToolResult, bank_tool

UNTRUSTED_NOTE = "未登记商户的名称属于外部不可信文本，只在卡片上展示给用户，不提供给助手"
METRIC_NOTE = "统计口径：已入账交易，北京时间；消费不含转账；储蓄率 =（收入 − 消费）÷ 收入"
CATEGORY_NOTE = "统计口径：已入账交易，北京时间；消费不含转账；占比小于 3% 的类目在图中并入「其他」"
_PERIOD = {"type": "string", "description": "时间范围，照抄用户原话里的说法，如「上个月」「9月」「最近7天」「本月」；用户没说就不填"}


def _window(ctx: ToolContext, period: str | None, default: str) -> Period:
    return parse_period(period, at=now(), default=default)


def _load(ctx: ToolContext, start, end) -> list[Txn]:
    txns = engine.load(ctx.db, ctx.user_id, start, end, before_llm=ctx.db.commit)
    flagged = [t for t in txns if t.untrusted_flags]
    if flagged:  # 外部文本里有注入特征：只记审计（L8 监测用），不改变任何统计结果
        audit.record(ctx.db, ctx.trace_id, "untrusted_text_isolated", {
            "txn_nos": [t.txn_no for t in flagged][:20],
            "flags": sorted({f for t in flagged for f in t.untrusted_flags}),
        }, ctx.session.id)
    return txns


def _when(t: Txn) -> str:
    return f"{t.at:%Y-%m-%d %H:%M}"


def _short(t: Txn) -> str:
    return f"{t.at.month}月{t.at.day}日 {t.at:%H:%M}"


def _where(party: str) -> str:
    return "一家未登记商户（名称见卡片）" if party == quarantine.UNREGISTERED else party


def _meta() -> dict:
    return {"as_of": now().strftime("%Y-%m-%d %H:%M"), "source": SOURCE_CORE}


def _period_note(p: Period) -> str:
    return "" if p.recognized else f"没识别出时间范围，按{p.label}统计。"


# ---------- F-BA-02 交易查询 ----------

_KINDS = ("消费", "转账", "收入", "全部")


@bank_tool(
    name="bill.query", domain="bill", base_risk=Risk.GREEN,
    description="查询交易明细并汇总金额，可按时间、商户或收款人、分类、金额范围筛选，"
                "如「上个月在美团花了多少」「最近一笔超过1000的消费」「这个月给我妈转了多少」。",
    capability="查交易明细", examples=("上个月在美团花了多少",),
    parameters={"type": "object", "properties": {
        "period": _PERIOD,
        "keyword": {"type": "string", "description": "商户或收款人，照抄用户原话，如「美团」「星巴克」「我妈」"},
        "category": {"type": "string", "enum": list(SPEND_CATEGORIES), "description": "消费分类，用户提到时才填"},
        "kind": {"type": "string", "enum": list(_KINDS), "description": "交易类型，默认「消费」；问转账填「转账」，问工资或进账填「收入」"},
        "min_amount": {"type": "string", "description": "金额下限，照抄用户原话，如「1000」「两千」"},
        "max_amount": {"type": "string", "description": "金额上限，照抄用户原话"},
        "order": {"type": "string", "enum": ["最近", "金额最大"], "description": "排序，默认最近的在前"},
        "limit": {"type": "integer", "description": "返回几笔，1–20，默认 10；「最近一笔」填 1"},
    }},
)
def query(ctx: ToolContext, period: str | None = None, keyword: str | None = None, category: str | None = None,
          kind: str | None = None, min_amount: str | None = None, max_amount: str | None = None,
          order: str | None = None, limit: int | str | None = None) -> ToolResult:
    p = _window(ctx, period, "近90天")
    txns = _load(ctx, p.start, p.end)
    filters: list[str] = []  # 卡片上展示的筛选条件
    subject = ""  # 回复里的「在美团外卖」「给我妈」

    merchant = contact_ids = None
    if keyword:
        merchant = merchant_by_alias(keyword)
        if merchant is None:
            contact_ids = {c.id for c in resolve_payee(ctx.db, ctx.user_id, keyword)} or None
    if kind not in _KINDS or (contact_ids and kind == KIND_SPEND):  # 问的是收款人，就是查转账
        kind = KIND_TRANSFER if contact_ids else KIND_SPEND
    if kind != "全部":
        txns = [t for t in txns if t.kind == kind]
    if category in SPEND_CATEGORIES:
        txns = [t for t in txns if t.category == category]
        filters.append(f"分类：{category}")
    if keyword:
        word = quarantine.normalize(keyword).lower()
        if merchant:
            key = merchant.lower()
            txns = [t for t in txns if t.merchant_key == key]
            filters.append(f"商户：{merchant}")
            subject = f"在{merchant}"
        elif contact_ids:
            txns = [t for t in txns if t.contact_id in contact_ids]
            filters.append(f"收款人：{keyword}")
            subject = f"给{keyword}"
        else:  # 用户说的词（可信）在规范化商户名里做子串匹配，确定性的，不经过模型
            txns = [t for t in txns if t.merchant_key and word and word in t.merchant_key]
            filters.append(f"关键词：{keyword}")
            subject = f"与「{keyword}」相关的"
    lo = parse_amount(min_amount) if min_amount else None
    hi = parse_amount(max_amount) if max_amount else None
    if lo:
        txns = [t for t in txns if t.amount_fen >= lo]
        filters.append(f"金额 ≥ {fmt_yuan(lo)} 元")
    if hi:
        txns = [t for t in txns if t.amount_fen <= hi]
        filters.append(f"金额 ≤ {fmt_yuan(hi)} 元")

    try:
        n = max(1, min(20, int(limit))) if limit not in (None, "") else 10
    except (TypeError, ValueError):
        n = 10
    if order == "金额最大":
        txns.sort(key=lambda t: (-t.amount_fen, -t.at.timestamp()))
    else:
        txns.sort(key=lambda t: t.at, reverse=True)
    total = sum(t.amount_fen for t in txns)
    shown = txns[:n]
    label = {"消费": "消费", "转账": "转账", "收入": "收入", "全部": "交易"}[kind]
    conds = [f for f in filters if not f.startswith(("商户：", "收款人：", "关键词："))]
    cond = f"（{'；'.join(conds)}）" if conds else ""

    if not txns:
        reply = f"{p.label}没有找到{subject}符合条件的{label}记录。"
    elif n == 1:
        t = shown[0]
        reply = (f"{p.label}{subject}{'金额最大' if order == '金额最大' else '最近'}的一笔{label}{cond}是 "
                 f"{_short(t)} {_where(t.party)}，{fmt_yuan(t.amount_fen)} 元。")
    else:
        reply = f"{p.label}{subject}共{label} {len(txns)} 笔{cond}，合计 {fmt_yuan(total)} 元。"
    reply = _period_note(p) + reply

    data = {
        "period": p.label, "kind": kind, "filters": filters, "count": len(txns), "total_yuan": fmt_yuan(total),
        "items": [{"time": _when(t), "type": t.kind, "category": t.category, "party": t.party,
                   "amount_yuan": fmt_yuan(t.amount_fen), **({"tags": _tags(t)} if _tags(t) else {})} for t in shown],
        "summary": reply,
    }
    if any(t.party == quarantine.UNREGISTERED for t in shown):
        data["note"] = UNTRUSTED_NOTE
    card = {
        "type": "bill_txns", "title": "交易查询", "period": p.label, "filters": filters, "kind": label,
        "count": len(txns), "total": fmt_yuan(total), "more": max(0, len(txns) - len(shown)),
        "single": ("金额最大" if order == "金额最大" else "最近") if n == 1 else None,  # 只问一笔时不展示合计
        "items": [{"time": _short(t), "name": t.display, "category": t.category, "kind": t.kind,
                   "amount": ("+" if t.direction == "in" else "-") + fmt_yuan(t.amount_fen),
                   "direction": t.direction, "tags": _tags(t)} for t in shown],
        "reply": reply, **_meta(),
    }
    return ToolResult(data, card)


def _tags(t: Txn) -> list[str]:
    tags = []
    if t.auto:
        tags.append("自动扣费")
    if t.cross_border:
        tags.append("境外")
    if t.cat_source == "隔离模型":
        tags.append("分类待确认")
    return tags


# ---------- F-BA-03 分类统计 ----------

def _category_rows(cur: list[Txn], prev: list[Txn], ok: bool) -> list[dict]:
    prev_by = {r["category"]: r["amount_fen"] for r in engine.by_category(prev)}
    rows = engine.by_category(cur)
    for r in rows:
        r |= engine.change(r["amount_fen"], prev_by.get(r["category"], 0), ok)
    return rows


@bank_tool(
    name="bill.categories", domain="bill", base_risk=Risk.GREEN,
    description="按消费分类（餐饮、购物、交通、居住、订阅…）统计支出金额、占比和环比，并给出饼图，"
                "如「这个月钱都花哪了」「上个月各类消费占比」。",
    capability="看消费分类统计", examples=("这个月钱都花哪了",),
    parameters={"type": "object", "properties": {"period": _PERIOD}},
)
def categories(ctx: ToolContext, period: str | None = None) -> ToolResult:
    p = _window(ctx, period, "本月")
    txns = _load(ctx, p.prev_start, p.end)
    cur = [t for t in txns if t.at >= p.start]
    prev = [t for t in txns if p.prev_start <= t.at < p.prev_end]
    ok = engine.comparable(ctx.db, ctx.user_id, p)
    rows = _category_rows(cur, prev, ok)
    spend, prev_spend = engine.totals(cur)["spend_fen"], engine.totals(prev)["spend_fen"]
    total_change = engine.change(spend, prev_spend, ok)

    if not rows:
        reply = f"{p.label}没有消费记录。"
    else:
        top = "，".join(f"{r['category']}（{engine.pct(r['permille'])}）" for r in rows[:2])
        reply = f"{p.label}消费 {fmt_yuan(spend)} 元，最多的是{top}。"
        if "change_fen" in total_change:
            d = total_change["change_fen"]
            reply += f"比{p.prev_label}{'多' if d >= 0 else '少'} {fmt_yuan(abs(d))} 元（{total_change['change']}）。"
    reply = _period_note(p) + reply

    data = {
        "period": p.label, "compare_to": p.prev_label, "spend_yuan": fmt_yuan(spend), "count": len([t for t in cur if t.kind == KIND_SPEND]),
        "spend_change": total_change.get("change"), "spend_change_yuan": total_change.get("change_yuan"),
        "categories": [{"category": r["category"], "amount_yuan": fmt_yuan(r["amount_fen"]), "share": engine.pct(r["permille"]),
                        "count": r["count"], "change": r["change"], **({"change_yuan": r["change_yuan"]} if "change_yuan" in r else {})}
                       for r in rows],
        "summary": reply,
    }
    card = {
        "type": "bill_categories", "title": "消费分类", "period": p.label, "compare_to": p.prev_label,
        "total": fmt_yuan(spend), "total_change": total_change.get("change"),
        "slices": _slices(rows),
        "rows": [{"category": r["category"], "amount": fmt_yuan(r["amount_fen"]), "share": engine.pct(r["permille"]),
                  "count": r["count"], "change": r["change"]} for r in rows],
        "note": CATEGORY_NOTE, "reply": reply, **_meta(),
    }
    return ToolResult(data, card)


def _slices(rows: list[dict]) -> list[dict]:
    return [{"category": s["category"], "amount": fmt_yuan(s["amount_fen"]), "share": engine.pct(s["permille"]),
             "permille": s["permille"], "merged": s.get("merged", [])} for s in engine.pie_slices(rows)]


# ---------- F-BA-04 异常识别 ----------

def _anomalies(ctx: ToolContext, p: Period, txns: list[Txn] | None = None) -> list[engine.Anomaly]:
    if txns is None:
        txns = _load(ctx, p.start - timedelta(days=engine.BASELINE_DAYS), p.end)
    return engine.detect(txns, p.start, p.end, engine.coverage_start(ctx.db, ctx.user_id))


_ADVICE = {"重复扣款": "可以联系商户申请退还多扣的款项。"}
_ADVICE_DEFAULT = "如果不是你本人的操作，可以先锁卡并联系客服核实。"


def _anomaly_card_item(a: engine.Anomaly) -> dict:
    return {"time": _short(a.txn), "name": a.txn.display, "amount": fmt_yuan(a.txn.amount_fen), "level": a.level,
            "signals": a.signals, "reason": a.reason, "advice": _ADVICE.get(a.signals[0], _ADVICE_DEFAULT),
            "count": len(a.related) + 1}


@bank_tool(
    name="bill.anomalies", domain="bill", base_risk=Risk.GREEN,
    description="找出异常或可疑的消费（深夜、境外、大额、新商户、重复扣款、明显偏离个人习惯），每条附可解释的理由，"
                "如「最近有没有异常消费」「我是不是被重复扣款了」。默认看近30天。",
    capability="查异常交易", examples=("最近有没有异常消费",),
    parameters={"type": "object", "properties": {"period": _PERIOD}},
)
def anomalies(ctx: ToolContext, period: str | None = None) -> ToolResult:
    p = _window(ctx, period, "近30天")
    found = _anomalies(ctx, p)
    if not found:
        reply = f"{p.label}没有发现异常消费。"
    else:
        parts = [f"{_short(a.txn)} {_where(a.txn.party)} {fmt_yuan(a.txn.amount_fen)} 元（{'、'.join(a.signals)}）" for a in found[:3]]
        reply = f"{p.label}发现 {len(found)} 笔需要留意的交易：" + "；".join(parts) + "。详情见卡片。"
    reply = _period_note(p) + reply
    data = {
        "period": p.label, "count": len(found),
        "items": [{"time": _when(a.txn), "party": a.txn.party, "category": a.txn.category,
                   "amount_yuan": fmt_yuan(a.txn.amount_fen), "level": a.level, "signals": a.signals,
                   "reason": a.reason, "advice": _ADVICE.get(a.signals[0], _ADVICE_DEFAULT)} for a in found],
        "summary": reply,
        "rule": "解释只引用实际命中的信号；不能说成「已确认被盗刷」",
    }
    if any(a.txn.party == quarantine.UNREGISTERED for a in found):
        data["note"] = UNTRUSTED_NOTE
    card = {"type": "bill_anomalies", "title": "异常交易", "period": p.label, "count": len(found),
            "items": [_anomaly_card_item(a) for a in found], "reply": reply, **_meta()}
    return ToolResult(data, card)


# ---------- F-BA-05 月度报告 ----------

@bank_tool(
    name="bill.report", domain="bill", base_risk=Risk.GREEN,
    description="生成账单报告：收入、支出、储蓄率、分类占比、Top 5 商户、订阅总额、异常事件，"
                "如「给我上个月的账单报告」「这个月的收支总结」。默认上个月。报告里的数字由系统计算，复述时只能用工具结果里的数字。",
    capability="生成月度账单报告", examples=("给我上个月的账单报告",),
    parameters={"type": "object", "properties": {"period": _PERIOD}},
)
def report(ctx: ToolContext, period: str | None = None) -> ToolResult:
    p = _window(ctx, period, "上个月")
    base_start = min(p.prev_start, p.start - timedelta(days=engine.BASELINE_DAYS))
    txns = _load(ctx, base_start, p.end)
    cur = [t for t in txns if p.start <= t.at < p.end]
    prev = [t for t in txns if p.prev_start <= t.at < p.prev_end]
    ok = engine.comparable(ctx.db, ctx.user_id, p)

    tot = engine.totals(cur)
    income, spend = tot["income_fen"], tot["spend_fen"]
    rate = engine.ratio_pct(max(0, income - spend), income) if income > 0 else "不适用"
    spend_change = engine.change(spend, engine.totals(prev)["spend_fen"], ok)
    rows = _category_rows(cur, prev, ok)
    tops = engine.top_merchants(cur)
    subs = [t for t in cur if t.kind == KIND_SPEND and t.category == "订阅"]
    sub_total = sum(t.amount_fen for t in subs)
    found = _anomalies(ctx, p, txns)
    insights = _insights(p, rows, spend_change, found, len(subs), sub_total)

    if not cur:
        summary = f"{p.label}没有交易记录。"
    else:
        summary = f"{p.label}收入 {fmt_yuan(income)} 元，消费 {fmt_yuan(spend)} 元，储蓄率 {rate}"
        summary += f"；消费最多的是{rows[0]['category']}（{engine.pct(rows[0]['permille'])}）。" if rows else "。"
        if found:
            summary += f"另有 {len(found)} 笔交易值得留意。"
    reply = _period_note(p) + summary

    audit.record(ctx.db, ctx.trace_id, "bill_snapshot", {
        "tool": "bill.report", "period": [p.start.isoformat(), p.end.isoformat()], "rules": RULES_VERSION,
        "txn_count": len(cur), "snapshot": engine.snapshot_id(cur)}, ctx.session.id)

    kpis = [{"label": "收入", "value": fmt_yuan(income), "unit": "元"},
            {"label": "消费", "value": fmt_yuan(spend), "unit": "元"},
            {"label": "转账转出", "value": fmt_yuan(tot["transfer_out_fen"]), "unit": "元"},
            {"label": "储蓄率", "value": rate, "unit": ""}]
    data = {
        "period": p.label, "compare_to": p.prev_label,
        "income_yuan": fmt_yuan(income), "spend_yuan": fmt_yuan(spend),
        "transfer_out_yuan": fmt_yuan(tot["transfer_out_fen"]), "net_yuan": fmt_yuan(tot["net_fen"]),
        "savings_rate": rate, "spend_change": spend_change.get("change"), "spend_change_yuan": spend_change.get("change_yuan"),
        "categories": [{"category": r["category"], "amount_yuan": fmt_yuan(r["amount_fen"]),
                        "share": engine.pct(r["permille"]), "change": r["change"]} for r in rows],
        "top_merchants": [{"party": m["party"], "amount_yuan": fmt_yuan(m["amount_fen"]), "count": m["count"]} for m in tops],
        "subscription_total_yuan": fmt_yuan(sub_total), "subscription_count": len(subs),
        "anomaly_count": len(found),
        "anomalies": [{"time": _when(a.txn), "party": a.txn.party, "amount_yuan": fmt_yuan(a.txn.amount_fen),
                       "level": a.level, "signals": a.signals} for a in found],
        "insights": insights, "summary": summary,
    }
    if any(m["party"] == quarantine.UNREGISTERED for m in tops) or any(a.txn.party == quarantine.UNREGISTERED for a in found):
        data["note"] = UNTRUSTED_NOTE
    card = {
        "type": "bill_report", "title": f"{p.label}账单报告", "period": p.label, "compare_to": p.prev_label,
        "kpis": kpis, "net": fmt_yuan(tot["net_fen"]),
        "spend_change": spend_change.get("change"), "spend_change_yuan": spend_change.get("change_yuan"),
        "slices": _slices(rows),
        "top_merchants": [{"name": m["display"], "amount": fmt_yuan(m["amount_fen"]), "count": m["count"]} for m in tops],
        "subscription": {"total": fmt_yuan(sub_total), "count": len(subs)},
        "anomalies": [_anomaly_card_item(a) for a in found],
        "insights": insights, "summary": summary, "note": METRIC_NOTE, "reply": reply, **_meta(),
    }
    return ToolResult(data, card)


def _insights(p: Period, rows: list[dict], spend_change: dict, found: list, n_subs: int, sub_total: int) -> list[str]:
    """规则挑出最多 3 条可验证的变化，句子由模板生成（技术文档 §7.4）。"""
    out = []
    if "change_fen" in spend_change:
        d = spend_change["change_fen"]
        out.append(f"消费比{p.prev_label}{'多' if d >= 0 else '少'} {fmt_yuan(abs(d))} 元（{spend_change['change']}）")
    ups = [r for r in rows if r.get("change_fen", 0) > 0 and r["amount_fen"] - r["change_fen"] > 0]
    if ups:
        r = max(ups, key=lambda r: r["change_fen"])
        out.append(f"{r['category']}比{p.prev_label}多花 {fmt_yuan(r['change_fen'])} 元（{r['change']}），增加最多")
    if found:
        out.append(f"有 {len(found)} 笔交易值得核实，见下方异常事件")
    if n_subs:
        out.append(f"订阅类自动扣费 {n_subs} 笔，合计 {fmt_yuan(sub_total)} 元")
    return out[:3]


# ---------- 规则兜底：模型不可用时按关键词组织参数 ----------

_AMT = r"([\d零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万萬wWkK.,]+)\s*(?:块钱|块|元)?"
_MIN = [re.compile(rf"(?:超过|大于|高于|多于|不低于|不少于|至少)\s*{_AMT}"), re.compile(rf"{_AMT}\s*(?:以上|起)")]
_MAX = [re.compile(rf"(?:低于|小于|少于|不超过|不到)\s*{_AMT}"), re.compile(rf"{_AMT}\s*以(?:下|内)")]
_PAYEE = re.compile(r"(?:给|向)(?P<who>[^\s，,。！!？?]{1,6}?)(?:转|打)")


def _rule_query_args(text: str) -> dict:
    args: dict = {"period": text} if has_period(text) else {}
    words = sorted({w for name, aliases in MERCHANTS.items() for w in (name.lower(), *aliases)}, key=len, reverse=True)
    low = text.lower()
    if hit := next((w for w in words if w in low), None):
        args["keyword"] = hit
    elif m := _PAYEE.search(text):
        args["keyword"], args["kind"] = m.group("who"), KIND_TRANSFER
    if cat := next((c for c in SPEND_CATEGORIES if c in text and c != "其他"), None):
        args["category"] = cat
    if re.search(r"收入|进账|入账|工资|收到|赚了", text):
        args["kind"] = KIND_INCOME
    elif "kind" not in args and re.search(r"转账|转出|转了", text):
        args["kind"] = KIND_TRANSFER
    for key, pats in (("min_amount", _MIN), ("max_amount", _MAX)):
        if m := next((m for pat in pats if (m := pat.search(text))), None):
            args[key] = m.group(1)
    if re.search(r"(最大|最贵|最多|金额最高)的?一笔", text):
        args["order"], args["limit"] = "金额最大", 1
    elif re.search(r"(最近|上|最后)一笔", text):
        args["limit"] = 1
    return args


RULE_TOOLS = {"bill_query": query, "bill_categories": categories, "bill_anomalies": anomalies, "bill_report": report}


def rule_turn(ctx: ToolContext, intent: str, text: str) -> ToolResult:
    fn = RULE_TOOLS[intent]
    if fn is query:
        return query(ctx, **_rule_query_args(text))
    return fn(ctx, period=text if has_period(text) else None)
