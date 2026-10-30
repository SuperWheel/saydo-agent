"""自由分析（F-BA-06）：固定的账单工具答不了的开放问题，如「工作日和周末的外卖支出比一比」「每月咖啡花费的趋势」。
只读（🟢），不出确认卡。

模型只拿到数据结构说明（SCHEMA_DOC），拿不到数据；它写的分析代码交给 app/sandbox：AST 白名单 → 子进程运行 →
输出契约。结果里的金额由系统格式化成元，卡片按模板渲染，模型复述时由事实校验兜底。
每次执行都记审计（sandbox_exec）：代码哈希和全文、校验结果、退出原因、耗时、峰值内存、快照摘要；
有逃逸意图的代码（双下划线、动态执行、系统模块、运行时被拦下）计入熔断器的越权次数。
"""

from .. import audit, sandbox
from ..analytics import engine
from ..analytics.periods import has_period, parse_period
from ..cards import notice_card
from ..clock import now
from ..money import fmt_yuan
from ..policy import Risk
from ..security import breaker, mask_pii
from . import ToolContext, ToolResult, bank_tool
from .bill import _PERIOD, _load, _meta, _period_note

TOOL = "bill.adhoc_analysis"
DEFAULT_PERIOD = "近90天"
SANDBOX_NOTE = "分析代码由 AI 生成，在隔离沙箱中运行：只读脱敏数据、无网络、限时限内存；统计口径：已入账交易，北京时间"
UNAVAILABLE = ("这个问题需要 AI 现写分析代码、在沙箱里运行，当前模型不可用，暂时答不了。"
               "可以先试试「这个月钱都花哪了」或「上个月在美团花了多少」。")


@bank_tool(
    name=TOOL, domain="bill", base_risk=Risk.GREEN,
    description="自由分析账单：其他账单工具答不了的开放问题，如「工作日和周末的外卖支出比一比」「每月咖啡花费的趋势」"
                "「一天里哪个时段点外卖最多」。你写一段 Python 分析代码（code），系统在沙箱里对只读、脱敏的交易快照运行，"
                "返回结构化结果。能用 bill_query、bill_categories、bill_anomalies、bill_report 回答的问题不要用它。"
                + sandbox.SCHEMA_DOC + sandbox.CONTRACT_DOC + "运行失败时会返回原因，可以改好代码再调用一次。",
    capability="自由分析账单（如工作日和周末对比、月度趋势）", examples=("工作日和周末的外卖支出比一比",),
    parameters={"type": "object", "properties": {
        "question": {"type": "string", "description": "用户的问题，照抄原话"},
        "period": _PERIOD,
        "code": {"type": "string", "description": "分析代码（Python），数据和结果格式见工具说明"},
    }, "required": ["code"]},
)
def adhoc_analysis(ctx: ToolContext, code: str, question: str | None = None, period: str | None = None) -> ToolResult:
    code, q = str(code or ""), str(question or "")  # 网关只保证是标量，可能是数字
    p = parse_period(period or (q if has_period(q) else None), at=now(), default=DEFAULT_PERIOD)
    txns = _load(ctx, p.start, p.end)
    an = sandbox.analyze(code, sandbox.snapshot(txns))
    run = an.run
    audit.record(ctx.db, ctx.trace_id, "sandbox_exec", {
        "tool": TOOL, "question": mask_pii(q)[:200], "period": [p.start.isoformat(), p.end.isoformat()],
        "code_sha256": an.code_sha256, "code": mask_pii(code[:sandbox.checker.MAX_CODE_CHARS]),
        "validation": "pass" if an.verdict.ok else "reject", "reasons": an.verdict.reasons, "escape": an.escape,
        "status": an.status, "detail": an.detail, "elapsed_ms": run.elapsed_ms if run else 0,
        "peak_kb": run.peak_bytes // 1024 if run else 0, "exit_code": run.exit_code if run else None,
        "txn_count": len(txns), "snapshot": engine.snapshot_id(txns), "rows": len(an.rows),
    }, ctx.session.id)
    if an.escape:
        breaker.on_tool_violation(ctx.db, user_id=ctx.user_id, session_id=ctx.session.id, trace_id=ctx.trace_id,
                                  tool=TOOL, error=f"沙箱拦截：{an.reason}（{an.detail}）")
    if not an.ok:
        return ToolResult({"status": "error", "reason": an.reason, "detail": an.detail,
                           "hint": "这次没有得到结果，回复里不要给出任何数字；可以按原因改好代码再调用一次"})

    rows = [_row(r) for r in an.rows]
    shown = "，".join(_say(r) for r in rows[:4]) + ("等" if len(rows) > 4 else "")
    reply = _period_note(p) + f"{p.label}{an.title}：{shown}。"
    data = {
        "status": "ok", "period": p.label, "txn_count": len(txns), "title": an.title,
        "rows": [{k: v for k, v in r.items() if k != "bar"} for r in rows],
        "summary": reply, "rule": "回复里的数字只能用 rows 里的；金额单位是元",
    }
    card = {
        "type": "bill_analysis", "title": an.title, "question": mask_pii(q)[:60], "period": p.label,
        "txn_count": len(txns), "rows": rows, "note": SANDBOX_NOTE, "reply": reply, **_meta(),
    }
    _bars(card["rows"])
    return ToolResult(data, card)


def _row(r: dict) -> dict:
    row: dict = {"label": r["label"]}
    if "amount_fen" in r:
        row["amount_yuan"] = fmt_yuan(r["amount_fen"])
        row["bar"] = abs(r["amount_fen"])
    if "count" in r:
        row["count"] = r["count"]
    if "value" in r:
        row["value"] = r["value"]
        if r.get("unit"):
            row["unit"] = r["unit"]
    return row


def _say(r: dict) -> str:
    parts = []
    if "amount_yuan" in r:
        parts.append(f"{r['amount_yuan']} 元")
    if "count" in r:
        parts.append(f"{r['count']} 笔")
    if "value" in r:
        parts.append(f"{r['value']}{r.get('unit', '')}")
    return f"{r['label']} {'、'.join(parts)}"


def _bars(rows: list[dict]) -> None:
    """有金额的行给一个相对最大值的千分比，卡片上画横条；没有金额的行不画。"""
    top = max((r.get("bar", 0) for r in rows), default=0)
    for r in rows:
        b = r.pop("bar", None)
        if b is not None and top:
            r["bar_permille"] = b * 1000 // top


# ---------- 规则兜底：没有模型就写不出分析代码，只回模板 ----------

def rule_turn(ctx: ToolContext, text: str) -> ToolResult:
    audit.record(ctx.db, ctx.trace_id, "sandbox_exec", {"tool": TOOL, "question": mask_pii(text)[:200],
                                                        "status": "unavailable", "reason": "规则模式没有模型生成代码"},
                 ctx.session.id)
    return ToolResult({"status": "unavailable", "summary": UNAVAILABLE}, notice_card(UNAVAILABLE))
