"""M0 银行工具：查余额、找收款人、提议转账（只读 / 提议），以及只能由网关执行的转账。"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..auth.actions import PolicyEnv, base_context, propose_action
from ..cards import action_display, balance_card, clarify_card, notice_card
from ..core_mock.ledger import LedgerError, post
from ..models import Account, Contact, Transaction
from ..money import fmt_yuan, parse_amount
from ..nlu import amount_from_user, resolve_payee
from ..policy import PolicyContext, Risk
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
    description="查询用户名下所有账户的余额。", capability="查余额", examples=("我还有多少钱",),
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
    description="在用户已保存的收款人里查找，返回脱敏信息。只用于查看收款人；要转账请直接调用 transfer_propose。",
    capability="查看已保存的收款人",
    parameters={"type": "object", "properties": {
        "hint": {"type": "string", "description": "收款人称呼或姓名，照抄用户原话，如「我妈」「张伟」"},
    }, "required": ["hint"]},
)
def search_contacts(ctx: ToolContext, hint: str) -> ToolResult:
    return ToolResult({"candidates": [_contact_view(c) for c in resolve_payee(ctx.db, ctx.user_id, hint)]})


@bank_tool(
    name="transfer.propose", domain="transfer", base_risk=Risk.GREEN,
    description="为一笔转账生成确认卡交给用户；查找收款人、同名时出选择卡都由它完成，不需要先调 contact_search。"
                "只生成待确认动作，不会转账；风险等级和是否需要验证由系统决定。",
    capability="给已保存的收款人转账", examples=("给我妈转500",),
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
    acct = db.scalars(select(Account).where(Account.user_id == ctx.user_id, Account.kind == "debit")).one()
    args = {"from_account_id": acct.id, "contact_id": contact.id, "account_no": contact.account_no,
            "amount_fen": amount_fen, "memo": memo}
    rows = [["收款人", contact.masked_name], ["收款银行", f"{contact.bank} · 尾号 {contact.tail}"]]
    if memo:
        rows.append(["备注", memo])
    display = action_display(
        title="转账确认", verb="转账", amount_fen=amount_fen, rows=rows,
        summary=f"向{contact.masked_name}（尾号{contact.tail}）转账{fmt_yuan(amount_fen)}元",
        fields={"payee": contact.masked_name, "bank": contact.bank, "tail": contact.tail, "memo": memo},
    )
    return propose_action(ctx, tool="transfer.create", args=args, display=display,
                          data={**_contact_view(contact), "amount_yuan": fmt_yuan(amount_fen)})


def transfer_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    contact = db.get(Contact, args["contact_id"])
    changed = contact is None or contact.account_no != args["account_no"]
    return base_context(db, env, tool="transfer.create", amount_fen=args["amount_fen"],
                        payee_known=not changed and contact.first_transfer_at is not None,
                        block_reason="收款人信息已变化" if changed else None)


@bank_tool(
    name="transfer.create", domain="transfer", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    moves_money=True, policy_context=transfer_context, description="执行转账。只能由工具网关凭执行令牌调用。",
)
def transfer_create(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    acct = db.get(Account, args["from_account_id"])
    existing = db.scalars(select(Transaction).where(Transaction.idempotency_key == idempotency_key)).first()
    if existing:  # 幂等：同一个动作只会记一笔账
        return _transfer_result(existing, acct)
    contact = db.get(Contact, args["contact_id"])
    if acct is None or acct.user_id != user_id or contact is None or contact.account_no != args["account_no"]:
        raise LedgerError("账户或收款人校验失败")
    txn = post(db, account=acct, direction="out", amount_fen=args["amount_fen"], counterparty=contact.name,
               contra_code=f"CLEARING:{contact.bank}", contact_id=contact.id, memo=args.get("memo") or None,
               category="转账", channel="agent", idempotency_key=idempotency_key)
    if contact.first_transfer_at is None:
        contact.first_transfer_at = txn.created_at
    return _transfer_result(txn, acct)


def _transfer_result(txn: Transaction, acct: Account) -> dict:
    at = f"{txn.created_at:%Y-%m-%d %H:%M:%S}"
    return {"status": "succeeded", "txn_no": txn.txn_no, "at": at, "amount_fen": txn.amount_fen,
            "balance_after_fen": acct.balance_fen,
            "receipt_rows": [["流水号", txn.txn_no], ["时间", at], ["转账后余额", f"¥{fmt_yuan(acct.balance_fen)}"]]}
