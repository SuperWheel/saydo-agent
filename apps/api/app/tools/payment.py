"""免密授权：表单只由用户提交额度，开通与变更仍走统一确认和强验证。"""

from datetime import datetime, timedelta

from sqlalchemy import select

from .. import audit, payments
from ..auth import actions
from ..cards import action_display, confirm_card, notice_card
from ..clock import now
from ..config import get_settings
from ..core_mock.ledger import LedgerError
from ..models import Card, PaymentMandate, PendingAction
from ..money import fmt_yuan
from ..nlu import mandate_intent
from ..policy import Risk
from . import ToolContext, ToolResult, bank_tool


def _notice(message: str) -> ToolResult:
    return ToolResult({"status": "rejected", "message": message}, notice_card(message, "warning"))


@bank_tool(name="mandate.form", domain="payment", base_risk=Risk.GREEN,
           description="用户本轮明确要求开通、设置或修改免密时，展示授权表单。额度只能由用户在表单填写，不接受模型预填。",
           capability="管理免密支付授权", examples=("开通免密支付", "设置免密额度"))
def mandate_form(ctx: ToolContext) -> ToolResult:
    intent = mandate_intent(ctx.user_texts[0] if ctx.user_texts else "")
    if intent is None or intent.slots.get("op") != "form":
        audit.record(ctx.db, ctx.trace_id, "provenance_reject", {"field": "mandate_intent"}, ctx.session.id)
        return _notice("请由您本人提出开通或修改免密支付，再填写授权表单。")
    return _form(ctx)


def _form(ctx: ToolContext) -> ToolResult:
    return ToolResult({"status": "form", "note": "请用户亲自选择付款卡并填写额度，尚未开通或修改授权。"},
                      payments.mandate_card(ctx.db, ctx.user_id))


@bank_tool(name="payment.overview", domain="payment", base_risk=Risk.GREEN,
           description="查看支付服务协议状态、免密授权额度与适用范围。", capability="查看支付授权",
           examples=("我的支付授权",))
def overview(ctx: ToolContext) -> ToolResult:
    agreement = payments.agreement_card(ctx.db, ctx.user_id)
    card = {"type": "payment_overview", "agreement": {
        "signed": agreement["status"] == "signed", "version": agreement["version"], "signed_at": agreement["signed_at"]},
        "mandate": payments.current_view(ctx.db, ctx.user_id), "scope": payments.scope(ctx.db, ctx.user_id)}
    return ToolResult({"status": "ok", **card}, card)


def _problem(db, user_id: int, args: dict, *, revoke: bool = False) -> str | None:
    current = payments.active_mandate(db, user_id)
    expected = args.get("mandate_id")
    if expected is None and current is not None:
        return "已存在免密授权，请重新打开表单修改"
    if expected is not None and (current is None or current.id != expected or current.version != args.get("version")):
        return "免密授权已变化，请重新发起"
    if revoke:
        return None if current else "当前没有有效免密授权"
    if type(args.get("valid_days")) is not int or args["valid_days"] not in payments.VALIDITY_OPTIONS:
        return "授权有效期只能选择30、90、180或365天"
    if not payments.agreement_valid(db, user_id):
        return "需先同意支付服务协议"
    for key, cap in payments.CAPS.items():
        value = args.get(key)
        if type(value) is not int or not 0 < value <= cap:
            return "单笔须大于0且不超过2,000元，每日须大于0且不超过5,000元"
    if args["single_max_fen"] > args["daily_max_fen"]:
        return "单笔免密额度不能超过每日额度"
    if type(args.get("card_id")) is not int or not payments.payable(db, db.get(Card, args["card_id"]), user_id):
        return "请选择本人可付款的银行卡；演示环境只支持用Ⅰ类借记卡主账户付款"
    return None


def mandate_context(db, args: dict, env):
    operation = args["operation"]
    block = _problem(db, env.user_id, args, revoke=operation == "revoke")
    current = payments.active_mandate(db, env.user_id)
    loosen = not block and (operation == "sign" or (operation == "update" and current is not None and (
        args["card_id"] != current.card_id or args["single_max_fen"] > current.single_max_fen
        or args["daily_max_fen"] > current.daily_max_fen
        or datetime.fromisoformat(args["valid_until"]) > current.valid_until)))
    return actions.base_context(db, env, tool=f"mandate.{operation}", block_reason=block,
                                facts={"mandate_loosen": loosen})


def submit_setup(ctx: ToolContext, selection: dict) -> ToolResult:
    current = payments.active_mandate(ctx.db, ctx.user_id)
    operation = "update" if current else "sign"
    args = {key: selection.get(key) for key in ("card_id", "single_max_fen", "daily_max_fen")}
    valid_days = selection.get("valid_days", current.valid_days if current else 365)
    args["valid_days"] = valid_days
    if current and "valid_days" not in selection:
        args["valid_until"] = current.valid_until.isoformat()
    else:
        args["valid_until"] = ((now() + timedelta(days=valid_days)).isoformat()
                               if type(valid_days) is int and valid_days in payments.VALIDITY_OPTIONS else None)
    args |= {"operation": operation, "mandate_id": current.id if current else None,
             "version": current.version if current else None}
    if problem := _problem(ctx.db, ctx.user_id, args):
        if not payments.agreement_valid(ctx.db, ctx.user_id):
            return ToolResult({"status": "payment_agreement_required", "note": "需先同意支付服务协议"},
                              payments.agreement_card(ctx.db, ctx.user_id))
        return _notice(problem)
    verb = "修改免密额度" if current else "开通免密支付"
    card = ctx.db.get(Card, args["card_id"])
    summary = f"{verb}：单笔{fmt_yuan(args['single_max_fen'])}元，每日{fmt_yuan(args['daily_max_fen'])}元"
    display = action_display(title=verb + "确认", verb=verb, summary=summary,
                             rows=[["付款卡", payments.card_label(card)],
                                   ["单笔额度", fmt_yuan(args["single_max_fen"]) + " 元"],
                                   ["每日额度", fmt_yuan(args["daily_max_fen"]) + " 元"],
                                   ["有效期", f"{valid_days}天（至 {datetime.fromisoformat(args['valid_until']):%Y-%m-%d}）"], ["规则", "免验证不免确认，新收款人与风险交易仍需强验证"]])
    return actions.propose_action(ctx, tool=f"mandate.{operation}", args=args, display=display)


@bank_tool(name="mandate.sign_propose", domain="payment", base_risk=Risk.GREEN,
           description="开通免密须用户填写表单，工具不接受金额或卡号参数。")
def sign_propose(ctx: ToolContext) -> ToolResult:
    return mandate_form(ctx)


@bank_tool(name="mandate.update_propose", domain="payment", base_risk=Risk.GREEN,
           description="修改免密须用户填写表单，工具不接受金额或卡号参数。")
def update_propose(ctx: ToolContext) -> ToolResult:
    return mandate_form(ctx)


@bank_tool(name="mandate.revoke_propose", domain="payment", base_risk=Risk.GREEN, stop_loss=True,
           description="用户明确要求关闭免密支付时生成撤销确认卡，确认前不生效。")
def revoke_propose(ctx: ToolContext) -> ToolResult:
    intent = mandate_intent(ctx.user_texts[0] if ctx.user_texts else "")
    if intent is None or intent.slots.get("op") != "revoke":
        return _notice("请由您本人提出关闭免密支付。")
    return _revoke(ctx)


def _revoke(ctx: ToolContext) -> ToolResult:
    row = payments.active_mandate(ctx.db, ctx.user_id)
    if row is None:
        return _notice("当前没有有效免密授权。")
    args = {"operation": "revoke", "mandate_id": row.id, "version": row.version}
    display = action_display(title="关闭免密支付确认", verb="关闭免密支付", summary="关闭免密支付授权",
                             rows=[["说明", "确认后立即失效，已生成的免密确认卡须重新发起"]])
    return actions.propose_action(ctx, tool="mandate.revoke", args=args, display=display)


def _result(db, row: PaymentMandate, rows: list[list[str]]) -> dict:
    return {"status": "succeeded", "mandate_id": row.id, "version": row.version,
            "single_max_fen": row.single_max_fen, "daily_max_fen": row.daily_max_fen,
            "valid_days": row.valid_days, "valid_until": f"{row.valid_until:%Y-%m-%d}",
            "card_label": payments.card_label(db.get(Card, row.card_id)), "receipt_rows": rows}


@bank_tool(name="mandate.sign", domain="payment", base_risk=Risk.RED, side_effect=True, llm_visible=False,
           policy_context=mandate_context)
def sign(db, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    if problem := _problem(db, user_id, args):
        raise LedgerError(problem)
    for old in db.scalars(select(PaymentMandate).where(PaymentMandate.user_id == user_id,
                                                      PaymentMandate.status == "active")):
        old.status, old.revoked_at = "revoked", now()
    row = PaymentMandate(user_id=user_id, card_id=args["card_id"], single_max_fen=args["single_max_fen"],
                         daily_max_fen=args["daily_max_fen"], signed_at=now(), valid_days=args["valid_days"],
                         valid_until=datetime.fromisoformat(args["valid_until"]))
    db.add(row)
    db.flush()
    return _result(db, row, [["状态", "免密支付已开通"], ["有效期", f"{row.valid_days}天（至 {row.valid_until:%Y-%m-%d}）"]])


@bank_tool(name="mandate.update", domain="payment", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
           policy_context=mandate_context)
def update(db, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    if problem := _problem(db, user_id, args):
        raise LedgerError(problem)
    row = payments.active_mandate(db, user_id)
    row.card_id, row.single_max_fen, row.daily_max_fen = args["card_id"], args["single_max_fen"], args["daily_max_fen"]
    row.valid_days, row.valid_until = args["valid_days"], datetime.fromisoformat(args["valid_until"])
    row.version += 1
    return _result(db, row, [["有效期", f"{row.valid_days}天（至 {row.valid_until:%Y-%m-%d}）"], ["单笔额度", fmt_yuan(row.single_max_fen) + " 元"],
                             ["每日额度", fmt_yuan(row.daily_max_fen) + " 元"]])


@bank_tool(name="mandate.revoke", domain="payment", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
           policy_context=mandate_context, stop_loss=True)
def revoke(db, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    if problem := _problem(db, user_id, args, revoke=True):
        raise LedgerError(problem)
    row = payments.active_mandate(db, user_id)
    row.status, row.revoked_at = "revoked", now()
    row.version += 1
    return _result(db, row, [["状态", "免密支付已关闭"]])


def agreement_selection(ctx: ToolContext, selection: dict) -> ToolResult:
    operation = selection.get("action")
    if operation == "view":
        return ToolResult({"status": "ok"}, payments.agreement_card(ctx.db, ctx.user_id))
    actions.expire_stale(ctx.db, ctx.user_id)
    action = None
    if selection.get("action_id"):
        if not isinstance(selection["action_id"], str):
            return _notice("动作不存在或已失效。")
        action = ctx.db.get(PendingAction, selection["action_id"])
        if (action is None or action.user_id != ctx.user_id or action.session_id != ctx.session.id
                or action.status != "pending" or not actions.REGISTRY[action.tool].needs_payment_agreement):
            return _notice("动作不存在或已失效，请重新发起。")
    if operation == "decline":
        if action:
            actions._release(ctx.db, action)
            action.status = "cancelled"
        audit.record(ctx.db, ctx.trace_id, "payment_agreement_declined",
                     {"version": payments.AGREEMENT_VERSION, "action_id": action.id if action else None}, ctx.session.id)
        message = "您暂未同意支付服务协议，本次操作已取消。" if action else "您暂未同意支付服务协议，未办理任何支付。"
        return ToolResult({"status": "cancelled"}, notice_card(message))
    if operation != "sign" or selection.get("version") != payments.AGREEMENT_VERSION:
        return _notice("协议版本或操作无效，请重新查看支付服务协议。")
    row = payments.consent(ctx.db, ctx.user_id)
    row.agreement_version, row.agreement_signed_at, row.agreement_revoked_at = payments.AGREEMENT_VERSION, now(), None
    audit.record(ctx.db, ctx.trace_id, "payment_agreement_signed",
                 {"version": row.agreement_version, "action_id": action.id if action else None}, ctx.session.id)
    if action:
        action.expires_at = now() + timedelta(seconds=get_settings().action_ttl_s)
        if bad := actions._revalidate(ctx.db, action):
            return ToolResult({"status": bad.status}, bad.card)
        return ToolResult({"status": "pending_confirmation"}, confirm_card(action))
    return ToolResult({"status": "signed"}, payments.agreement_card(ctx.db, ctx.user_id))


def selection_turn(ctx: ToolContext, selection: dict) -> ToolResult:
    kind = selection.get("type")
    if kind == "payment_agreement":
        return agreement_selection(ctx, selection)
    if kind == "mandate_setup":
        return submit_setup(ctx, selection)
    if kind == "mandate_change" and selection.get("action") == "revoke":
        return _revoke(ctx)
    if kind == "mandate_offer":
        if selection.get("action") == "open":
            return _form(ctx)
        if selection.get("action") == "dismiss":
            payments.consent(ctx.db, ctx.user_id).mandate_offer_dismissed_at = now()
            audit.record(ctx.db, ctx.trace_id, "mandate_offer_dismissed", {}, ctx.session.id)
            return ToolResult({"status": "dismissed"}, notice_card("已记录，30天内不再主动邀请开通免密。"))
    return _notice("这个选项已经失效，请重新打开支付授权。")
