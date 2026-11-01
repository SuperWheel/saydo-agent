"""心愿罐管理与部分转出；内部划转不占对外转账额度。"""

import re
from dataclasses import replace
from datetime import datetime

from sqlalchemy import select

from .. import audit
from ..auth import actions
from ..cards import action_display, clarify_card, notice_card
from ..clock import now
from ..core_mock.ledger import LedgerError, post
from ..models import Account, PendingAction, TaskPlan, Transaction
from ..money import fmt_yuan, parse_amount
from ..nlu import pot_intent
from ..policy import Risk
from . import ToolContext, ToolResult, bank_tool
from .automation import _clean, _debit, _need_amount_from_user, _own_pot, active_pots, pot_lock_propose, pot_txns


def _notice(message: str) -> ToolResult:
    return ToolResult({"status": "rejected", "message": message}, notice_card(message, "warning"))


def _resolve(value, steps: dict):
    if isinstance(value, str) and (m := re.fullmatch(r"\{\{([\w-]+)\.(\w+)\}\}", value)):
        return (steps.get(m[1], {}).get("output") or {}).get(m[2])
    return value


def _plans(db, user_id: int, pot_id: str) -> tuple[list[dict], int]:
    items, reserved, counted = [], 0, set()
    for plan in db.scalars(select(TaskPlan).where(TaskPlan.user_id == user_id,
                                                TaskPlan.status.not_in(("done", "cancelled", "handoff")))):
        steps = {s["id"]: s for s in plan.steps}
        for step in plan.steps:
            if step.get("status") in ("done", "cancelled", "removed"):
                continue
            args = step.get("args") or {}
            if _resolve(args.get("pot_id"), steps) != pot_id:
                continue
            action = db.get(PendingAction, step["action_id"]) if step.get("action_id") else None
            if action and action.status == "succeeded":
                continue
            purchase = step.get("purchase") or {}
            at = step.get("run_at") or purchase.get("run_at") or (plan.due_at if step.get("at") == "due" else None)
            if isinstance(at, str):
                try:
                    at = datetime.fromisoformat(at)
                except ValueError:
                    at = None
            if hasattr(at, "strftime"):
                at = at.strftime("%Y-%m-%d %H:%M")
            items.append({"label": step.get("title") or plan.goal, "run_at": at})
            amount = (purchase.get("budget_fen") or args.get("amount_fen")
                      or parse_amount(str(args.get("amount_text", ""))))
            if not amount and isinstance(args.get("quote_id"), str):
                ref = re.fullmatch(r"\{\{([\w-]+)\.quote_id\}\}", args["quote_id"])
                if ref:
                    amount = (steps.get(ref[1], {}).get("output") or {}).get("price_fen")
            if action and action.user_id == user_id and action.tool == "order.pay":
                amount = action.args.get("amount_fen")
                counted.add(action.id)
            if type(amount) is int and amount > 0 and step.get("tool") in ("order.propose", "order.pay", "transfer.propose"):
                reserved += amount
    # 已出确认卡的订单即使未挂在计划里，也不能被另一张转出确认卡挤占。
    for action in db.scalars(select(PendingAction).where(PendingAction.user_id == user_id,
                                                        PendingAction.tool == "order.pay",
                                                        PendingAction.status.in_(("pending", "awaiting_auth", "executing", "unknown")))):
        if action.id not in counted and action.args.get("pot_id") == pot_id:
            if action.status in ("pending", "awaiting_auth") and action.expires_at < now():
                continue
            reserved += action.args.get("amount_fen", 0)
    return items, reserved


@bank_tool(name="pot.manage", domain="automation", base_risk=Risk.GREEN,
           description="查看与管理本人心愿罐、余额、历史付款及待执行计划。",
           capability="管理心愿罐", examples=("我的心愿罐", "心愿罐余额", "管理心愿罐"))
def manage(ctx: ToolContext) -> ToolResult:
    debit = _debit(ctx.db, ctx.user_id)
    pots = []
    for pot in active_pots(ctx.db, ctx.user_id):
        view = pot_txns(ctx.db, ctx.user_id, pot.id)
        spent = -sum(item["amount_fen"] for item in view["items"] if item["kind"] in ("pay", "refund"))
        plans, _ = _plans(ctx.db, ctx.user_id, pot.id)
        pots.append({**view["pot"], "until": str(pot.until) if pot.until else None,
                     "spent": fmt_yuan(spent), "spent_fen": spent, "plans": plans})
    card = {"type": "pots", "title": "我的心愿罐", "pots": pots,
            "debit": {"account_id": debit.id, "label": f"{debit.name} · 尾号 {debit.card_tail}",
                      "balance": fmt_yuan(debit.balance_fen), "balance_fen": debit.balance_fen},
            "verify_note": "转入心愿罐需人脸或短信验证（任选其一）；转出到活期只需确认",
            "empty_text": "还没有心愿罐，可以说「存 500 到心愿罐，给妈妈买生日礼物」"}
    return ToolResult({"status": "ok", **card}, card)


def _problem(db, user_id: int, args: dict) -> str | None:
    amount = args.get("amount_fen")
    if type(amount) is not int or amount <= 0:
        return "转出金额必须是大于0的整数分"
    pot = _own_pot(db, user_id, args.get("pot_id"))
    account = db.get(Account, pot.account_id) if pot else None
    debit = db.get(Account, args.get("to_account_id"))
    if pot is None or pot.status != "active" or account is None or account.user_id != user_id or account.status != "active":
        return "心愿罐不存在或不可用"
    if debit is None or debit.user_id != user_id or debit.kind != "debit" or debit.status != "active":
        return "转入活期账户不可用"
    if account.balance_fen != args.get("balance_fen"):
        return "心愿罐余额已变化，请重新发起"
    _, reserved = _plans(db, user_id, pot.id)
    if amount > max(0, account.balance_fen - reserved):
        return f"可转出余额不足（当前余额 {fmt_yuan(account.balance_fen)} 元，待执行计划预占 {fmt_yuan(reserved)} 元）"
    return None


def withdraw_context(db, args: dict, env):
    return actions.base_context(db, env, tool="pot.withdraw", block_reason=_problem(db, env.user_id, args),
                                facts={"pot_withdraw": True})


def _propose_withdraw(ctx: ToolContext, pot_id: str, amount_fen: int) -> ToolResult:
    pot = _own_pot(ctx.db, ctx.user_id, pot_id)
    if pot is None:
        return _notice("心愿罐不存在")
    account = ctx.db.get(Account, pot.account_id)
    debit = _debit(ctx.db, ctx.user_id)
    args = {"pot_id": pot.id, "amount_fen": amount_fen, "balance_fen": account.balance_fen,
            "to_account_id": debit.id}
    if problem := _problem(ctx.db, ctx.user_id, args):
        return _notice(problem)
    display = action_display(title="心愿罐转出确认", verb="转出到活期", amount_fen=amount_fen,
                             summary=f"从心愿罐「{pot.purpose}」转出{fmt_yuan(amount_fen)}元到活期",
                             rows=[["心愿罐", pot.purpose], ["转入账户", f"{debit.name} · 尾号 {debit.card_tail}"],
                                   ["转出后余额", fmt_yuan(account.balance_fen - amount_fen) + " 元"]],
                             fields={"pot_id": pot.id, "purpose": pot.purpose})
    return actions.propose_action(ctx, tool="pot.withdraw", args=args, display=display)


@bank_tool(name="pot.withdraw_propose", domain="automation", base_risk=Risk.GREEN,
           description="用户本轮明确要求从心愿罐取出指定金额到活期时，生成部分转出确认卡。金额必须来自本轮原话。",
           parameters={"type": "object", "properties": {"amount_text": {"type": "string"}, "pot_hint": {"type": "string"}},
                       "required": ["amount_text"]})
def withdraw_propose(ctx: ToolContext, amount_text: str, pot_hint: str = "") -> ToolResult:
    intent = pot_intent(ctx.user_texts[0] if ctx.user_texts else "")
    if intent is None or intent.slots.get("op") != "withdraw":
        audit.record(ctx.db, ctx.trace_id, "provenance_reject", {"field": "pot_withdraw_intent"}, ctx.session.id)
        return _notice("请由您本人提出从心愿罐转出，并说明金额。")
    if parse_amount(amount_text or "") is None:
        return _notice("要从心愿罐转出多少元？")
    amount, bad = _need_amount_from_user(replace(ctx, user_texts=ctx.user_texts[:1]), amount_text)
    if bad:
        return bad
    pots = active_pots(ctx.db, ctx.user_id)
    hint = pot_hint or ctx.user_texts[0]
    pots = [p for p in pots if p.id == hint or p.purpose in hint] or pots
    if not pots:
        return _notice("您还没有在用的心愿罐。")
    if len(pots) > 1:
        return ToolResult({"status": "need_pot"}, clarify_card("从哪个心愿罐转出？", [
            {"label": p.purpose, "selection": {"type": "pot_manage", "op": "withdraw", "pot_id": p.id,
                                               "amount_fen": amount}} for p in pots]))
    return _propose_withdraw(ctx, pots[0].id, amount)


@bank_tool(name="pot.withdraw", domain="automation", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
           policy_context=withdraw_context, description="心愿罐部分余额转回本人活期，保留心愿罐。")
def withdraw(db, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    existing = db.scalar(select(Transaction).where(Transaction.idempotency_key == idempotency_key))
    if existing is not None:
        pot = _own_pot(db, user_id, args.get("pot_id"))
        if pot is None or existing.account_id != args.get("to_account_id") or existing.amount_fen != args.get("amount_fen"):
            raise LedgerError("心愿罐转出校验失败")
    elif problem := _problem(db, user_id, args):
        raise LedgerError(problem)
    pot = _own_pot(db, user_id, args["pot_id"])
    account, debit = db.get(Account, pot.account_id), db.get(Account, args["to_account_id"])
    if existing is None:
        post(db, account=debit, direction="in", amount_fen=args["amount_fen"], counterparty=account.name,
             contra_code=f"DEP:{account.id}", category="心愿罐", channel="system", idempotency_key=idempotency_key)
        account.balance_fen -= args["amount_fen"]
    return {"status": "succeeded", "pot_id": pot.id, "purpose": pot.purpose, "amount_fen": args["amount_fen"], "balance_fen": account.balance_fen,
            "receipt_rows": [["转出金额", fmt_yuan(args["amount_fen"]) + " 元"],
                             ["心愿罐余额", fmt_yuan(account.balance_fen) + " 元"], ["活期余额", fmt_yuan(debit.balance_fen) + " 元"]]}


def selection_turn(ctx: ToolContext, selection: dict) -> ToolResult:
    amount, pot_id = selection.get("amount_fen"), selection.get("pot_id")
    if type(amount) is not int or not 0 < amount <= 9_000_000_000_000_000:
        return _notice("金额必须是大于0且在有效范围内的整数分")
    if pot_id is not None and not isinstance(pot_id, str):
        return _notice("心愿罐不存在")
    pot = _own_pot(ctx.db, ctx.user_id, pot_id)
    if pot_id is not None and (pot is None or pot.status != "active"):
        return _notice("心愿罐不存在或不可用")
    if selection.get("op") == "withdraw":
        return _propose_withdraw(ctx, pot_id, amount)
    if selection.get("op") == "deposit":
        purpose = pot.purpose if pot else selection.get("purpose")
        if not isinstance(purpose, str) or not _clean(purpose, 12):
            return _notice("请填写新心愿罐的用途")
        # 表单金额是用户的明确选择，转换为同一提议工具的金额来源。
        value = f"{amount // 100}.{amount % 100:02d}"
        if pot:
            # 指定编号时沿用存入工具的参数和确认逻辑，避免同用途的另一个罐被误选。
            debit = _debit(ctx.db, ctx.user_id)
            args = {"from_account_id": debit.id, "amount_fen": amount, "purpose": pot.purpose,
                    "until": str(pot.until) if pot.until else None, "pot_id": pot.id}
            display = action_display(title="存入心愿罐确认", verb="存入心愿罐", amount_fen=amount,
                                     summary=f"存{fmt_yuan(amount)}元进心愿罐「{pot.purpose}」",
                                     rows=[["心愿罐", f"「{pot.purpose}」（追加存入）"],
                                           ["转出账户", f"{debit.name} · 尾号 {debit.card_tail}"]],
                                     fields={"pot_id": pot.id, "purpose": pot.purpose})
            return actions.propose_action(ctx, tool="pot.lock", args=args, display=display)
        return pot_lock_propose(replace(ctx, user_texts=[value]), value, purpose,
                                until=str(pot.until) if pot and pot.until else None)
    return _notice("心愿罐操作无效")
