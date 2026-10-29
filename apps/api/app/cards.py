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


def confirm_card(action) -> dict:
    d = action.display
    return {
        "type": "confirm",
        "action_id": action.id,
        "digest": action.digest,
        "title": "转账确认",
        "payee": d["payee"],
        "bank": d["bank"],
        "tail": d["tail"],
        "amount": fmt_yuan(d["amount_fen"]),
        "amount_capital": to_capital(d["amount_fen"]),
        "memo": d.get("memo") or "",
        "today_after": fmt_yuan(d["cumulative_after_fen"]),
        "level": action.level,
        "level_label": {"YELLOW": "需要确认", "RED": "需要强验证"}[action.level],
        "reasons": action.reasons,
        "auth_hint": "确认后需完成人脸识别 + 短信验证码（模拟）" if action.level == "RED" else "",
        "expires_at": action.expires_at.isoformat(),
        "status": action.status,
    }


FACTOR_LABELS = {"face": "人脸识别（模拟）", "sms": "短信验证码（模拟）"}


def auth_card(challenge, action, *, sms_preview: str | None, attempts_left: int) -> dict:
    return {
        "type": "auth",
        "challenge_id": challenge.id,
        "action_id": action.id,
        "digest": action.digest,
        "summary": f"向 {action.display['payee']}（{action.display['bank']} 尾号 {action.display['tail']}）"
                   f"转账 {fmt_yuan(action.display['amount_fen'])} 元",
        "factors": [
            {"factor": f, "label": FACTOR_LABELS[f], "passed": f in challenge.factors_passed}
            for f in challenge.factors_required
        ],
        "sms_preview": sms_preview,
        "attempts_left": attempts_left,
        "status": challenge.status,
    }


def receipt_card(action, result: dict) -> dict:
    d = action.display
    return {
        "type": "receipt",
        "status": result["status"],
        "status_label": {"succeeded": "转账成功", "failed": "转账失败", "unknown": "结果待确认"}[result["status"]],
        "txn_no": result.get("txn_no"),
        "payee": d["payee"],
        "bank": d["bank"],
        "tail": d["tail"],
        "amount": fmt_yuan(d["amount_fen"]),
        "memo": d.get("memo") or "",
        "time": result.get("at"),
        "balance_after": fmt_yuan(result["balance_after_fen"]) if "balance_after_fen" in result else None,
        "watermark": "模拟回执 · 非真实交易",
        "source": SOURCE_CORE,
    }
