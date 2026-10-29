"""卡片模板。涉及资金的信息一律由服务端按真实参数渲染，不经过模型。"""

from .clock import now
from .money import fmt_yuan, to_capital

SOURCE_CORE = "核心系统（模拟）"


def balance_card(accounts: list[dict]) -> dict:
    total = sum(a["balance_fen"] for a in accounts)
    return {
        "type": "balance",
        "accounts": [{"name": a["name"], "tail": a["card_tail"], "balance": fmt_yuan(a["balance_fen"])} for a in accounts],
        "total": fmt_yuan(total),
        "as_of": now().strftime("%Y-%m-%d %H:%M"),
        "source": SOURCE_CORE,
    }


def clarify_card(question: str, options: list[dict]) -> dict:
    return {"type": "clarify", "question": question, "options": options}


def notice_card(text: str, level: str = "info") -> dict:
    return {"type": "notice", "level": level, "text": text}


def action_display(*, title: str, verb: str, summary: str, rows: list[list[str]], amount_fen: int | None = None,
                   fields: dict | None = None) -> dict:
    """待确认动作的展示内容，确认卡、验证卡、模拟短信和回执都按它渲染。

    title    卡片标题，如「转账确认」「锁卡确认」
    verb     动作名，用在「转账成功」「本次锁卡已取消」这类提示里
    summary  一句话摘要，如「向王*英（尾号1234）转账500.00元」，用于验证卡和短信
    rows     [[标签, 值], ...]，按顺序展示
    amount_fen  有金额时卡片顶部大字显示金额和大写
    fields   机器可读的字段（测试、前端特殊渲染用），不展示
    """
    d = {"title": title, "verb": verb, "summary": summary, "rows": rows, "fields": fields or {}}
    if amount_fen is not None:
        d["amount_fen"] = amount_fen
    return d


def confirm_card(action) -> dict:
    d = action.display
    card = {
        "type": "confirm",
        "action_id": action.id,
        "digest": action.digest,
        "title": d["title"],
        "rows": d["rows"],
        "fields": d.get("fields", {}),
        "level": action.level,
        "level_label": {"YELLOW": "需要确认", "RED": "需要强验证"}[action.level],
        "reasons": action.reasons,
        "auth_hint": "确认后需完成人脸识别 + 短信验证码（模拟）" if action.level == "RED" else "",
        "expires_at": action.expires_at.isoformat(),
        "status": action.status,
    }
    if "amount_fen" in d:
        card |= {"amount": fmt_yuan(d["amount_fen"]), "amount_capital": to_capital(d["amount_fen"])}
    if "cumulative_after_fen" in d:
        card["today_after"] = fmt_yuan(d["cumulative_after_fen"])
    return card


FACTOR_LABELS = {"face": "人脸识别（模拟）", "sms": "短信验证码（模拟）"}


def auth_card(challenge, action, *, sms_preview: str | None, attempts_left: int) -> dict:
    return {
        "type": "auth",
        "challenge_id": challenge.id,
        "action_id": action.id,
        "digest": action.digest,
        "summary": action.display["summary"],
        "factors": [
            {"factor": f, "label": FACTOR_LABELS[f], "passed": f in challenge.factors_passed}
            for f in challenge.factors_required
        ],
        "sms_preview": sms_preview,
        "attempts_left": attempts_left,
        "status": challenge.status,
    }


def receipt_card(action, result: dict) -> dict:
    """回执：动作的展示行 + 写工具返回的结果行（result["receipt_rows"]，如流水号、时间）。"""
    d = action.display
    verb = d["verb"]
    card = {
        "type": "receipt",
        "status": result["status"],
        "status_label": {"succeeded": f"{verb}成功", "failed": f"{verb}失败", "unknown": "结果待确认"}[result["status"]],
        "title": d["title"],
        "rows": [*d["rows"], *result.get("receipt_rows", [])],
        "txn_no": result.get("txn_no"),
        "watermark": "模拟回执 · 非真实交易",
        "source": SOURCE_CORE,
    }
    if "amount_fen" in d:
        card["amount"] = fmt_yuan(d["amount_fen"])
    return card
