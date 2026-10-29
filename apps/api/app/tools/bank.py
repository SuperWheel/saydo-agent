"""M0 银行工具：查余额、找收款人、提议转账（只读 / 提议），以及只能由网关执行的转账。"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..auth.actions import create_transfer_action, expire_stale, transfer_policy_context
from ..cards import balance_card, clarify_card, confirm_card, notice_card
from ..core_mock.ledger import LedgerError, post
from ..models import Account, Contact, Transaction
from ..money import fmt_yuan, parse_amount
from ..nlu import amount_from_user, resolve_payee
from ..policy import Risk, evaluate
from ..security import mask_pii
from . import ToolContext, ToolResult, bank_tool

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _contact_view(c: Contact) -> dict:
    return {"name": c.masked_name, "bank": c.bank, "tail": c.tail, "note": c.note or c.relation or "",
            "known_payee": c.first_transfer_at is not None}


def _set_question(ctx: ToolContext, question: dict | None) -> None:
    state = dict(ctx.session.state or {})
    if question:
        state["pending_question"] = question
    else:
        state.pop("pending_question", None)
    ctx.session.state = state  # 整体赋值，JSON 列才会被识别为已修改


@bank_tool(
    name="account.balance", domain="account", base_risk=Risk.GREEN,
    description="查询用户名下所有账户的余额。",
)
def get_balance(ctx: ToolContext) -> ToolResult:
    accts = ctx.db.scalars(select(Account).where(Account.user_id == ctx.user_id, Account.status == "active")).all()
    rows = [{"name": a.name, "card_tail": a.card_tail, "balance_fen": a.balance_fen} for a in accts]
    card = balance_card(rows)
    data = {
        "accounts": [{"name": r["name"], "card_tail": r["card_tail"], "balance_yuan": fmt_yuan(r["balance_fen"])} for r in rows],
        "total_yuan": card["total"], "as_of": card["as_of"],
    }
    return ToolResult(data, card)


@bank_tool(
    name="contact.search", domain="transfer", base_risk=Risk.GREEN,
    description="在用户已保存的收款人里查找，返回脱敏信息。",
    parameters={"type": "object", "properties": {
        "hint": {"type": "string", "description": "收款人称呼或姓名，照抄用户原话，如「我妈」「张伟」"},
    }, "required": ["hint"]},
)
def search_contacts(ctx: ToolContext, hint: str) -> ToolResult:
    return ToolResult({"candidates": [_contact_view(c) for c in resolve_payee(ctx.db, ctx.user_id, hint)]})


@bank_tool(
    name="transfer.propose", domain="transfer", base_risk=Risk.GREEN,
    description="为一笔转账生成确认卡交给用户。只生成待确认动作，不会转账；风险等级和是否需要验证由系统决定。",
    parameters={"type": "object", "properties": {
        "payee_hint": {"type": "string", "description": "收款人称呼，照抄用户原话，如「我妈」「张伟」「房东」"},
        "amount_text": {"type": "string", "description": "金额，照抄用户原话，如「500」「两千五」「1.5w」，不要换算"},
        "memo": {"type": "string", "description": "转账备注，用户说了才填"},
    }, "required": ["payee_hint", "amount_text"]},
)
def propose_transfer(ctx: ToolContext, payee_hint: str, amount_text: str, memo: str | None = None) -> ToolResult:
    return propose(ctx, payee_hint=payee_hint, amount_text=amount_text, memo=memo)


def propose(ctx: ToolContext, *, payee_hint: str = "", amount_text: str = "", memo: str | None = None,
            contact_id: int | None = None, amount_fen: int | None = None) -> ToolResult:
    """模型、规则兜底、澄清选择三个入口共用的转账提议逻辑。"""
    db = ctx.db
    memo = mask_pii(_CTRL.sub("", (memo or "").strip()))[:20]
    if amount_fen is None and amount_text:
        amount_fen = parse_amount(amount_text)
        if amount_fen is not None and not amount_from_user(amount_fen, ctx.user_texts):
            audit.record(db, ctx.trace_id, "provenance_reject", {"field": "amount", "amount_text": amount_text},
                         ctx.session.id)
            return ToolResult({"status": "amount_not_from_user", "message": "金额必须来自用户原话，请向用户确认金额"},
                              notice_card("请再说一下要转多少钱。", "warning"))

    if contact_id is not None:
        c = db.get(Contact, contact_id)
        candidates = [c] if c and c.user_id == ctx.user_id else []
    else:
        candidates = resolve_payee(db, ctx.user_id, payee_hint)
    if not candidates:
        return ToolResult({"status": "payee_not_found", "hint": payee_hint},
                          notice_card(f"没有在你的收款人里找到「{payee_hint}」。目前只支持向已保存的收款人转账。", "warning"))
    if amount_fen is None:
        _set_question(ctx, {"kind": "amount", "contact_ids": [c.id for c in candidates], "memo": memo})
        return ToolResult({"status": "need_amount", "candidates": [_contact_view(c) for c in candidates]})
    if len(candidates) > 1:
        _set_question(ctx, {"kind": "payee", "contact_ids": [c.id for c in candidates], "amount_fen": amount_fen, "memo": memo})
        options = [{"label": c.name, "detail": f"{c.bank} 尾号 {c.tail}" + (f" · {c.note or c.relation}" if c.note or c.relation else ""),
                    "selection": {"type": "contact", "contact_id": c.id}} for c in candidates]
        return ToolResult(
            {"status": "need_clarification", "candidates": [_contact_view(c) for c in candidates]},
            clarify_card(f"你有 {len(candidates)} 位收款人符合「{payee_hint or candidates[0].name}」，要转给哪一位？", options),
        )

    contact = candidates[0]
    _set_question(ctx, None)
    expire_stale(db, ctx.user_id)
    decision = evaluate(transfer_policy_context(db, user_id=ctx.user_id, contact=contact, amount_fen=amount_fen,
                                                channel=ctx.channel, injection_flag=ctx.injection_flag))
    audit.record(db, ctx.trace_id, "policy_decision", {"tool": "transfer.create", "amount_fen": amount_fen,
                                                       "payee": contact.masked_name, **decision.as_dict()}, ctx.session.id)
    if decision.block:
        return ToolResult({"status": "blocked", "reason": decision.block_reason},
                          notice_card(decision.block_reason or "该操作被拒绝", "error"))
    action = create_transfer_action(db, user_id=ctx.user_id, session_id=ctx.session.id, trace_id=ctx.trace_id,
                                    contact=contact, amount_fen=amount_fen, memo=memo, decision=decision,
                                    injection_flag=ctx.injection_flag)
    return ToolResult({
        "status": "pending_confirmation", **_contact_view(contact), "amount_yuan": fmt_yuan(amount_fen),
        "level": decision.level.name, "level_label": decision.level.label, "reasons": decision.reasons,
        "note": "确认卡已展示给用户。用户点击确认并完成验证之前不会转账，不要说已经转账。",
    }, confirm_card(action))


@bank_tool(
    name="transfer.create", domain="transfer", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    description="执行转账。只能由工具网关凭执行令牌调用。",
)
def transfer_create(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    acct = db.get(Account, args["from_account_id"])
    existing = db.scalars(select(Transaction).where(Transaction.idempotency_key == idempotency_key)).first()
    if existing:  # 幂等：同一个动作只会记一笔账
        return {"status": "succeeded", "txn_no": existing.txn_no, "at": f"{existing.created_at:%Y-%m-%d %H:%M:%S}",
                "amount_fen": existing.amount_fen, "balance_after_fen": acct.balance_fen}
    contact = db.get(Contact, args["contact_id"])
    if acct is None or acct.user_id != user_id or contact is None or contact.account_no != args["account_no"]:
        raise LedgerError("账户或收款人校验失败")
    txn = post(db, account=acct, direction="out", amount_fen=args["amount_fen"], counterparty=contact.name,
               contra_code=f"CLEARING:{contact.bank}", contact_id=contact.id, memo=args.get("memo") or None,
               category="转账", channel="agent", idempotency_key=idempotency_key)
    if contact.first_transfer_at is None:
        contact.first_transfer_at = txn.created_at
    return {"status": "succeeded", "txn_no": txn.txn_no, "at": f"{txn.created_at:%Y-%m-%d %H:%M:%S}",
            "amount_fen": txn.amount_fen, "balance_after_fen": acct.balance_fen}
