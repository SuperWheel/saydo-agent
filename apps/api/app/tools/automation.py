"""跨场景联动（WS7）：心愿罐、模拟商户询价下单、定时转账、自动任务总览、转人工。

写操作都走 propose_action → 确认卡 →（🔴 强验证）→ 执行令牌 → 网关 → 审计。
到期执行（定时转账、心愿罐到期释放）用的是创建时用户确认过的授权：执行前重新做熔断检查和策略定级，
风险比授权时高就转为普通确认卡，被拦下就通知用户，不会静默执行（run_authorized）。

心愿罐是一个 kind = pot 的子账户：存入 / 释放都是活期和子账户之间的内部划转（channel = system，
不进账单分析），从心愿罐付款就是子账户的支出。对外不称「冻结」「保本」。
"""

import re
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..auth import actions
from ..auth.actions import PolicyEnv, base_context, create_action, decide, propose_action
from ..cards import action_display, confirm_card, notice_card
from ..clock import now
from ..core_mock import shops
from ..core_mock.ledger import LedgerError, post
from ..models import (Account, ChatMessage, Contact, MerchantOrder, PendingAction, ScheduledTransfer, TaskPlan,
                      Transaction, WishPot)
from ..money import fmt_yuan, parse_amount
from ..nlu import amount_from_user, resolve_payee
from ..nlu.when import next_monthly, parse_date, parse_when
from ..policy import PolicyContext, Risk
from ..security import breaker, mask_pii
from . import ToolContext, ToolResult, bank_tool

DOMAIN = "automation"
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_YELLOW_MAX_FEN = 100_000  # 与当日累计转出的 🟡 阈值一致


def _clean(text: str | None, limit: int) -> str:
    return mask_pii(_CTRL.sub("", (text or "").strip()))[:limit]


def _debit(db: Session, user_id: int) -> Account:
    return db.scalars(select(Account).where(Account.user_id == user_id, Account.kind == "debit")).one()


def active_pots(db: Session, user_id: int) -> list[WishPot]:
    return list(db.scalars(select(WishPot).where(WishPot.user_id == user_id, WishPot.status == "active")
                           .order_by(WishPot.created_at)))


def _own_pot(db: Session, user_id: int, pot_id: str | None) -> WishPot | None:
    pot = db.get(WishPot, pot_id) if pot_id else None
    return pot if pot is not None and pot.user_id == user_id else None


def _pot_view(db: Session, pot: WishPot) -> dict:
    acct = db.get(Account, pot.account_id)
    return {"pot_id": pot.id, "purpose": pot.purpose, "until": f"{pot.until}" if pot.until else None,
            "balance_fen": acct.balance_fen, "status": pot.status}


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return parse_date(value, now())


def _need_amount_from_user(ctx: ToolContext, amount_text: str) -> tuple[int | None, ToolResult | None]:
    """金额来源校验：必须能在用户最近说的话里找到（与转账同一规则）。"""
    fen = parse_amount(amount_text or "")
    if fen is None:
        return None, ToolResult({"status": "need_amount", "message": "请向用户确认金额"},
                                notice_card("要存多少钱？", "warning"))
    if not amount_from_user(fen, ctx.user_texts):
        audit.record(ctx.db, ctx.trace_id, "provenance_reject", {"field": "amount", "amount_text": amount_text},
                     ctx.session.id)
        return None, ToolResult({"status": "amount_not_from_user", "message": "金额必须来自用户原话，请向用户确认金额"},
                                notice_card("请再说一下金额。", "warning"))
    return fen, None


# ---------- 心愿罐（F-XS-02） ----------

@bank_tool(
    name="pot.lock_propose", domain=DOMAIN, base_risk=Risk.GREEN,
    description="把活期里的一笔钱存进心愿罐（专项子账户，到期前不参与日常支出），生成确认卡。同一用途的心愿罐会合并存入。"
                "只生成待确认动作，不会马上划转。",
    capability="存钱进心愿罐", examples=("存500进心愿罐，给妈妈过生日",),
    parameters={"type": "object", "properties": {
        "amount_text": {"type": "string", "description": "金额，照抄用户原话，如「500」「一千」"},
        "purpose": {"type": "string", "description": "用途，如「妈妈生日」；用户没说就写「心愿」"},
        "until": {"type": "string", "description": "到期日 YYYY-MM-DD（如生日当天），没有就不填"},
    }, "required": ["amount_text"]},
)
def pot_lock_propose(ctx: ToolContext, amount_text: str, purpose: str = "", until: str | None = None) -> ToolResult:
    db = ctx.db
    amount_fen, bad = _need_amount_from_user(ctx, amount_text)
    if bad:
        return bad
    purpose = _clean(purpose, 12) or "心愿"
    until_d = _parse_iso_date(until)
    if until_d is not None and until_d < now().date():
        until_d = None
    pot = next((p for p in active_pots(db, ctx.user_id) if p.purpose == purpose), None)
    acct = _debit(db, ctx.user_id)
    args = {"from_account_id": acct.id, "amount_fen": amount_fen, "purpose": purpose,
            "until": f"{until_d}" if until_d else None, "pot_id": pot.id if pot else None}
    rows = [["心愿罐", f"「{purpose}」" + ("（追加存入）" if pot else "（新建）")], ["转出账户", f"{acct.name} · 尾号 {acct.card_tail}"]]
    if until_d:
        rows.append(["到期日", f"{until_d}，到期后剩余资金自动退回活期"])
    rows.append(["说明", "钱仍在你名下，随时可以说「撤销锁定」退回活期"])
    display = action_display(title="存入心愿罐", verb="存入心愿罐", amount_fen=amount_fen, rows=rows,
                             summary=f"存入心愿罐「{purpose}」{fmt_yuan(amount_fen)}元",
                             fields={"purpose": purpose, "until": args["until"]})
    return propose_action(ctx, tool="pot.lock", args=args, display=display,
                          data={"purpose": purpose, "amount_yuan": fmt_yuan(amount_fen), "until": args["until"]})


def pot_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    acct = db.get(Account, args["from_account_id"])
    block = None
    if acct is None or acct.user_id != env.user_id:
        block = "转出账户校验失败"
    elif args["amount_fen"] > acct.balance_fen:
        block = f"可用余额不足（当前 {fmt_yuan(acct.balance_fen)} 元）"
    elif args.get("pot_id") and _own_pot(db, env.user_id, args["pot_id"]) is None:
        block = "心愿罐不存在"
    elif args.get("pot_id") and _own_pot(db, env.user_id, args["pot_id"]).status != "active":
        block = "心愿罐已经释放"
    return base_context(db, env, tool="pot.lock", block_reason=block)


@bank_tool(
    name="pot.lock", domain=DOMAIN, base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    policy_context=pot_context, description="从活期划入心愿罐。只能由工具网关凭执行令牌调用。",
)
def pot_lock(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    debit = db.get(Account, args["from_account_id"])
    if debit is None or debit.user_id != user_id:
        raise LedgerError("转出账户校验失败")
    existing = db.scalars(select(WishPot).where(WishPot.idempotency_key == idempotency_key)).first()
    pot = existing or _own_pot(db, user_id, args.get("pot_id"))
    if pot is None:
        acct = Account(user_id=user_id, kind="pot", name=f"心愿罐·{args['purpose']}", card_tail="", balance_fen=0)
        db.add(acct)
        db.flush()
        pot = WishPot(id="P" + secrets.token_hex(6), user_id=user_id, account_id=acct.id, purpose=args["purpose"],
                      until=_parse_iso_date(args.get("until")), idempotency_key=idempotency_key)
        db.add(pot)
        db.flush()
    elif pot.status != "active":
        raise LedgerError("心愿罐已经释放")
    acct = db.get(Account, pot.account_id)
    if db.scalars(select(Transaction).where(Transaction.idempotency_key == idempotency_key)).first() is None:
        post(db, account=debit, direction="out", amount_fen=args["amount_fen"], counterparty=acct.name,
             contra_code=f"DEP:{acct.id}", category="心愿罐", channel="system", idempotency_key=idempotency_key)
        acct.balance_fen += args["amount_fen"]
        pot.locked_fen += args["amount_fen"]
    return {"status": "succeeded", "pot_id": pot.id, "amount_fen": args["amount_fen"], "balance_fen": acct.balance_fen,
            "receipt_rows": [["心愿罐余额", f"¥{fmt_yuan(acct.balance_fen)}"], ["活期余额", f"¥{fmt_yuan(debit.balance_fen)}"]]}


def _pick_pot(db: Session, user_id: int, hint: str) -> tuple[WishPot | None, list[WishPot]]:
    pots = active_pots(db, user_id)
    hint = (hint or "").strip()
    hits = [p for p in pots if hint and (p.purpose in hint or hint in p.purpose or p.id == hint)] or pots
    return (hits[0] if len(hits) == 1 else None), hits


@bank_tool(
    name="pot.release_propose", domain=DOMAIN, base_risk=Risk.GREEN,
    description="释放心愿罐（撤销锁定）：把心愿罐里剩余的钱全部退回活期，生成确认卡。只生成待确认动作。",
    capability="释放心愿罐、撤销锁定",
    parameters={"type": "object", "properties": {
        "pot_hint": {"type": "string", "description": "哪个心愿罐，照抄用户说的用途，如「妈妈生日」；只有一个时可不填"},
    }},
)
def pot_release_propose(ctx: ToolContext, pot_hint: str = "") -> ToolResult:
    db = ctx.db
    pot, hits = _pick_pot(db, ctx.user_id, pot_hint)
    if not hits:
        return ToolResult({"status": "no_pot"}, notice_card("你现在没有在用的心愿罐，不需要撤销。"))
    if pot is None:
        names = "、".join(f"「{p.purpose}」" for p in hits)
        return ToolResult({"status": "need_pot", "pots": [p.purpose for p in hits]},
                          notice_card(f"你有 {len(hits)} 个心愿罐：{names}，要释放哪一个？", "warning"))
    return propose_release(ctx, pot)


def propose_release(ctx: ToolContext, pot: WishPot, *, title: str = "释放心愿罐") -> ToolResult:
    acct = ctx.db.get(Account, pot.account_id)
    debit = _debit(ctx.db, ctx.user_id)
    args = {"pot_id": pot.id, "amount_fen": acct.balance_fen, "to_account_id": debit.id}
    rows = [["心愿罐", f"「{pot.purpose}」"], ["退回账户", f"{debit.name} · 尾号 {debit.card_tail}"],
            ["说明", "释放后心愿罐关闭，依赖它的待办订单不会再下单"]]
    display = action_display(title=title, verb="释放心愿罐", amount_fen=acct.balance_fen or None, rows=rows,
                             summary=f"释放心愿罐「{pot.purpose}」，{fmt_yuan(acct.balance_fen)}元退回活期",
                             fields={"pot_id": pot.id, "purpose": pot.purpose})
    return propose_action(ctx, tool="pot.release", args=args, display=display,
                          data={"purpose": pot.purpose, "amount_yuan": fmt_yuan(acct.balance_fen)})


def release_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    pot = _own_pot(db, env.user_id, args["pot_id"])
    block = None
    if pot is None or pot.status != "active":
        block = "心愿罐已经释放"
    elif db.get(Account, pot.account_id).balance_fen != args["amount_fen"]:
        block = "心愿罐余额已变化"
    return base_context(db, env, tool="pot.release", block_reason=block)


@bank_tool(
    name="pot.release", domain=DOMAIN, base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    policy_context=release_context, description="心愿罐余额退回活期并关闭。只能由工具网关凭执行令牌调用。",
)
def pot_release(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    pot = _own_pot(db, user_id, args["pot_id"])
    debit = db.get(Account, args["to_account_id"])
    if pot is None or debit is None or debit.user_id != user_id:
        raise LedgerError("心愿罐校验失败")
    acct = db.get(Account, pot.account_id)
    if pot.status == "active":
        if acct.balance_fen != args["amount_fen"]:
            raise LedgerError("心愿罐余额已变化")
        if acct.balance_fen:
            post(db, account=debit, direction="in", amount_fen=acct.balance_fen, counterparty=acct.name,
                 contra_code=f"DEP:{acct.id}", category="心愿罐", channel="system", idempotency_key=idempotency_key)
            acct.balance_fen = 0
        pot.status, pot.released_at, acct.status = "released", now(), "closed"
    return {"status": "succeeded", "pot_id": pot.id, "amount_fen": args["amount_fen"],
            "receipt_rows": [["退回金额", f"¥{fmt_yuan(args['amount_fen'])}"], ["活期余额", f"¥{fmt_yuan(debit.balance_fen)}"]]}


# ---------- 模拟商户：询价、下单、取消（F-XS-03） ----------

_CATEGORY_WORDS = {"flower": ("flower", "花", "鲜花", "花束"), "cake": ("cake", "蛋糕")}


def category_of(word: str) -> str | None:
    w = (word or "").strip().lower()
    return next((c for c, words in _CATEGORY_WORDS.items() if any(x in w for x in words)), None)


@bank_tool(
    name="merchant.quote", domain=DOMAIN, base_risk=Risk.GREEN,
    description="向合作花店或蛋糕店询价：按收礼人挑一件商品，返回价格、配送时段、退款条件和报价单号 quote_id"
                "（2 小时内有效）。只读，不会下单。",
    capability="给花店、蛋糕店询价",
    parameters={"type": "object", "properties": {
        "category": {"type": "string", "enum": ["flower", "cake"], "description": "flower 鲜花 / cake 蛋糕"},
        "for_whom": {"type": "string", "description": "送给谁，照抄用户原话，如「我妈」「爱人」"},
        "date": {"type": "string", "description": "送达日期 YYYY-MM-DD，没说就不填（默认明天）"},
    }, "required": ["category"]},
)
def merchant_quote(ctx: ToolContext, category: str, for_whom: str = "", date: str | None = None) -> ToolResult:
    cat = category_of(category)
    if cat is None:
        return ToolResult({"status": "unsupported", "message": "目前只合作了花店和蛋糕店"})
    at = now()
    d = _parse_iso_date(date)
    if d is None or d <= at.date():
        d = at.date() + timedelta(days=1)
    q = shops.quote(cat, for_whom=_clean(for_whom, 10), delivery_date=d, at=at)
    if q is None:
        return ToolResult({"status": "out_of_stock", "message": f"{shops.CATEGORY_LABELS[cat]}暂时没有可售商品"})
    return ToolResult({"status": "ok", **q.view()})


def quote_problem(q: shops.Quote | None, at: datetime) -> str | None:
    if q is None:
        return "报价单不存在，请重新询价"
    if q.quoted_at > at + timedelta(minutes=1) or at > q.valid_until:
        return "报价已过期，请重新询价"
    return None


@bank_tool(
    name="order.propose", domain=DOMAIN, base_risk=Risk.GREEN,
    description="按询价结果（quote_id 来自 merchant_quote）生成订单确认卡；有在用的心愿罐时默认从心愿罐付款。"
                "价格以商户报价为准，只生成待确认动作，不会马上付款。",
    capability="在花店、蛋糕店下单",
    parameters={"type": "object", "properties": {
        "quote_id": {"type": "string", "description": "merchant_quote 返回的报价单号，原样填写"},
        "pot_id": {"type": "string", "description": "从哪个心愿罐付款，不填时自动选用唯一的心愿罐"},
    }, "required": ["quote_id"]},
)
def order_propose(ctx: ToolContext, quote_id: str, pot_id: str | None = None) -> ToolResult:
    db, at = ctx.db, now()
    q = shops.lookup_quote(quote_id or "")
    if problem := quote_problem(q, at):
        return ToolResult({"status": "quote_invalid", "message": problem}, notice_card(problem, "warning"))
    price = q.item.price_fen
    pot = _own_pot(db, ctx.user_id, pot_id) if pot_id else None
    if pot_id and (pot is None or pot.status != "active"):
        return ToolResult({"status": "pot_unavailable"}, notice_card("这个心愿罐已经释放，订单可以改从活期付款，请重新说一下。", "warning"))
    if pot is None and not pot_id:
        usable = [p for p in active_pots(db, ctx.user_id) if db.get(Account, p.account_id).balance_fen >= price]
        pot = usable[0] if len(usable) == 1 else None
    debit = _debit(db, ctx.user_id)
    pay_from = f"心愿罐「{pot.purpose}」" if pot else f"{debit.name} · 尾号 {debit.card_tail}"
    args = {"quote_id": q.quote_id, "shop_id": q.shop.id, "sku": q.item.sku, "amount_fen": price,
            "pot_id": pot.id if pot else None, "from_account_id": debit.id, "delivery_date": f"{q.delivery_date}"}
    rows = [["商户", q.shop.name + ("（合作商户）" if q.shop.whitelisted else "")], ["商品", q.item.name],
            ["配送", q.delivery], ["付款方式", pay_from], ["退款条件", q.shop.refund], ["报价有效期", f"至 {q.valid_until:%m-%d %H:%M}"]]
    display = action_display(title="订单确认", verb="下单", amount_fen=price, rows=rows,
                             summary=f"在{q.shop.name}购买{q.item.name}，{fmt_yuan(price)}元",
                             fields={"shop": q.shop.name, "item": q.item.name, "category": q.shop.category,
                                     "pay_from": "pot" if pot else "debit"})
    return propose_action(ctx, tool="order.pay", args=args, display=display,
                          data={"shop": q.shop.name, "item": q.item.name, "price_yuan": fmt_yuan(price),
                                "pay_from": pay_from})


def order_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    q = shops.lookup_quote(args["quote_id"])
    block = quote_problem(q, now())
    if block is None and (q.item.price_fen != args["amount_fen"] or q.shop.id != args["shop_id"]):
        block = "商户价格或商品有变化，请重新确认"
    pot = _own_pot(db, env.user_id, args.get("pot_id")) if args.get("pot_id") else None
    if block is None and args.get("pot_id") and (pot is None or pot.status != "active"):
        block = "心愿罐已经释放，请重新下单"
    ctx = base_context(db, env, tool="order.pay", amount_fen=args["amount_fen"], block_reason=block)
    if pot is not None:  # 从心愿罐付款：可用余额看心愿罐
        ctx.balance_fen = db.get(Account, pot.account_id).balance_fen
    after = ctx.outflow_today_fen + args["amount_fen"]
    ctx.facts["order_fast_path"] = bool(q and q.shop.whitelisted and pot is not None and after <= _YELLOW_MAX_FEN)
    return ctx


@bank_tool(
    name="order.pay", domain=DOMAIN, base_risk=Risk.YELLOW, side_effect=True, llm_visible=False, moves_money=True,
    policy_context=order_context, description="向模拟商户下单并付款。只能由工具网关凭执行令牌调用。",
)
def order_pay(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    existing = db.scalars(select(MerchantOrder).where(MerchantOrder.idempotency_key == idempotency_key)).first()
    if existing:  # 幂等：同一个动作只下一单
        return _order_result(db, existing)
    q = shops.lookup_quote(args["quote_id"])
    if quote_problem(q, now()) or q.item.price_fen != args["amount_fen"]:
        raise LedgerError("报价已失效")
    pot = _own_pot(db, user_id, args.get("pot_id")) if args.get("pot_id") else None
    if args.get("pot_id") and (pot is None or pot.status != "active"):
        raise LedgerError("心愿罐已经释放")
    acct = db.get(Account, pot.account_id) if pot else db.get(Account, args["from_account_id"])
    if acct is None or acct.user_id != user_id:
        raise LedgerError("付款账户校验失败")
    txn = post(db, account=acct, direction="out", amount_fen=args["amount_fen"], counterparty=q.shop.name,
               contra_code="CLEARING:UNIONPAY", merchant=q.shop.name, mcc=q.shop.mcc, category="购物",
               channel="agent", idempotency_key=idempotency_key)
    order = MerchantOrder(id="O" + secrets.token_hex(6), user_id=user_id, shop_id=q.shop.id, sku=q.item.sku,
                          item=q.item.name, amount_fen=args["amount_fen"], pot_id=pot.id if pot else None,
                          delivery=q.delivery, txn_no=txn.txn_no, idempotency_key=idempotency_key)
    db.add(order)
    db.flush()
    return _order_result(db, order)


def _order_result(db: Session, order: MerchantOrder) -> dict:
    rows = [["订单号", order.id], ["配送", order.delivery]]
    if order.pot_id:
        rows.append(["心愿罐余额", f"¥{fmt_yuan(db.get(Account, db.get(WishPot, order.pot_id).account_id).balance_fen)}"])
    return {"status": "succeeded", "order_id": order.id, "txn_no": order.txn_no, "amount_fen": order.amount_fen,
            "receipt_rows": rows}


def paid_orders(db: Session, user_id: int) -> list[MerchantOrder]:
    return list(db.scalars(select(MerchantOrder).where(MerchantOrder.user_id == user_id, MerchantOrder.status == "paid")
                           .order_by(MerchantOrder.created_at)))


@bank_tool(
    name="order.cancel_propose", domain=DOMAIN, base_risk=Risk.GREEN,
    description="取消在花店、蛋糕店下的订单并退款（退回原付款的心愿罐或活期），生成确认卡。只生成待确认动作。",
    capability="取消花店、蛋糕店订单",
    parameters={"type": "object", "properties": {
        "order_hint": {"type": "string", "description": "哪个订单：订单号，或照抄用户说的商品，如「蛋糕」「花」"},
    }},
)
def order_cancel_propose(ctx: ToolContext, order_hint: str = "") -> ToolResult:
    orders = paid_orders(ctx.db, ctx.user_id)
    cat = category_of(order_hint)
    hits = [o for o in orders if o.id == order_hint or (cat and shops.SHOPS[o.shop_id].category == cat)] or \
        (orders if not order_hint else [])
    if not hits:
        return ToolResult({"status": "no_order"}, notice_card("没有找到可以取消的订单。"))
    if len(hits) > 1:
        return ToolResult({"status": "need_order", "orders": [o.item for o in hits]},
                          notice_card("你有几笔订单：" + "、".join(o.item for o in hits) + "，要取消哪一笔？", "warning"))
    return propose_cancel(ctx, hits[0])


def propose_cancel(ctx: ToolContext, order: MerchantOrder) -> ToolResult:
    db = ctx.db
    pot = _own_pot(db, ctx.user_id, order.pot_id) if order.pot_id else None
    to_pot = pot is not None and pot.status == "active"
    debit = _debit(db, ctx.user_id)
    args = {"order_id": order.id, "amount_fen": order.amount_fen, "refund_to_pot": pot.id if to_pot else None,
            "to_account_id": debit.id}
    shop = shops.SHOPS[order.shop_id]
    rows = [["商户", shop.name], ["商品", order.item], ["订单号", order.id],
            ["退款去向", f"心愿罐「{pot.purpose}」" if to_pot else f"{debit.name} · 尾号 {debit.card_tail}"],
            ["退款条件", shop.refund]]
    display = action_display(title="取消订单", verb="取消订单", amount_fen=order.amount_fen, rows=rows,
                             summary=f"取消{shop.name}的{order.item}订单，退款{fmt_yuan(order.amount_fen)}元",
                             fields={"order_id": order.id, "item": order.item})
    return propose_action(ctx, tool="order.cancel", args=args, display=display,
                          data={"item": order.item, "refund_yuan": fmt_yuan(order.amount_fen)})


def cancel_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    order = db.get(MerchantOrder, args["order_id"])
    block = None
    if order is None or order.user_id != env.user_id or order.status != "paid":
        block = "订单已经取消或不存在"
    elif args.get("refund_to_pot") and _own_pot(db, env.user_id, args["refund_to_pot"]).status != "active":
        block = "心愿罐已经释放，请重新发起取消"
    return base_context(db, env, tool="order.cancel", block_reason=block)


@bank_tool(
    name="order.cancel", domain=DOMAIN, base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    policy_context=cancel_context, description="取消模拟商户订单并退款。只能由工具网关凭执行令牌调用。",
)
def order_cancel(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    order = db.get(MerchantOrder, args["order_id"])
    if order is None or order.user_id != user_id:
        raise LedgerError("订单校验失败")
    if order.status == "paid":
        pot = _own_pot(db, user_id, args.get("refund_to_pot")) if args.get("refund_to_pot") else None
        acct = db.get(Account, pot.account_id) if pot and pot.status == "active" else db.get(Account, args["to_account_id"])
        shop = shops.SHOPS[order.shop_id]
        txn = post(db, account=acct, direction="in", amount_fen=order.amount_fen, counterparty=shop.name,
                   contra_code="CLEARING:UNIONPAY", merchant=shop.name, mcc=shop.mcc, category="退款",
                   channel="agent", idempotency_key=idempotency_key)
        order.status, order.refund_txn_no = "cancelled", txn.txn_no
    return {"status": "succeeded", "order_id": order.id, "amount_fen": order.amount_fen,
            "receipt_rows": [["退款流水号", order.refund_txn_no or ""]]}


# ---------- 定时 / 周期转账（F-TR-05） ----------

@bank_tool(
    name="schedule.transfer_propose", domain=DOMAIN, base_risk=Risk.GREEN,
    description="设置定时或每月转账（如「每月15号给我妈转2000」「下周五给房东转3500」），生成确认卡。"
                "确认后到时间自动执行，每次执行前重新校验余额、限额和安全状态。只生成待确认动作。",
    capability="设置定时 / 每月转账", examples=("每月15号给我妈转2000",),
    parameters={"type": "object", "properties": {
        "payee_hint": {"type": "string", "description": "收款人称呼或姓名，照抄用户原话，如「我妈」"},
        "amount_text": {"type": "string", "description": "单次金额，照抄用户原话，如「2000」"},
        "when_text": {"type": "string", "description": "执行时间，照抄用户原话，如「每月15号」「下周五」「11月20号」"},
        "memo": {"type": "string", "description": "转账备注，用户说了才填"},
    }, "required": ["payee_hint", "amount_text", "when_text"]},
)
def schedule_transfer_propose(ctx: ToolContext, payee_hint: str, amount_text: str, when_text: str,
                              memo: str | None = None) -> ToolResult:
    db = ctx.db
    fen = parse_amount(amount_text or "")
    if fen is None or not amount_from_user(fen, ctx.user_texts):
        audit.record(db, ctx.trace_id, "provenance_reject", {"field": "amount", "amount_text": amount_text}, ctx.session.id)
        return ToolResult({"status": "amount_not_from_user", "message": "金额必须来自用户原话，请向用户确认金额"},
                          notice_card("每次转多少钱？", "warning"))
    when = parse_when(when_text or "", now())
    if when is None:
        return ToolResult({"status": "need_time", "message": "请向用户确认执行时间"},
                          notice_card("什么时候转？可以说「每月15号」或「下周五」。", "warning"))
    candidates = resolve_payee(db, ctx.user_id, payee_hint)
    if len(candidates) != 1:
        msg = (f"没有在你的收款人里找到「{payee_hint}」。" if not candidates
               else f"你有 {len(candidates)} 位收款人符合「{payee_hint}」，定时转账请说全名或备注（如「同事张伟」）。")
        return ToolResult({"status": "payee_not_found" if not candidates else "need_clarification"},
                          notice_card(msg, "warning"))
    c = candidates[0]
    memo = _clean(memo, 20)
    args = {"contact_id": c.id, "account_no": c.account_no, "amount_fen": fen, "memo": memo,
            "recurrence": when.recurrence, "day_of_month": when.day_of_month, "first_run_at": when.first.isoformat()}
    rows = [["收款人", c.masked_name], ["收款银行", f"{c.bank} · 尾号 {c.tail}"], ["执行时间", when.label],
            ["首次执行", f"{when.first:%Y-%m-%d %H:%M}"]]
    if memo:
        rows.append(["备注", memo])
    rows.append(["说明", "每次执行前重新校验余额、限额和安全状态；风险变高时会请你重新确认。可随时说「取消定时转账」"])
    display = action_display(title="定时转账确认", verb="设置定时转账", amount_fen=fen, rows=rows,
                             summary=f"设置定时转账：{when.label}向{c.masked_name}（尾号{c.tail}）转账{fmt_yuan(fen)}元",
                             fields={"payee": c.masked_name, "tail": c.tail, "recurrence": when.recurrence})
    return propose_action(ctx, tool="schedule.transfer_create", args=args, display=display,
                          data={"payee": c.masked_name, "amount_yuan": fmt_yuan(fen), "when": when.label})


def schedule_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    c = db.get(Contact, args["contact_id"])
    changed = c is None or c.user_id != env.user_id or c.account_no != args["account_no"]
    ctx = base_context(db, env, tool="schedule.transfer_create", block_reason="收款人信息已变化" if changed else None)
    ctx.facts |= {"amount_over": args["amount_fen"] > _YELLOW_MAX_FEN,
                  "payee_new": not changed and c.first_transfer_at is None}
    return ctx


@bank_tool(
    name="schedule.transfer_create", domain=DOMAIN, base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    policy_context=schedule_context, description="创建定时转账授权。只能由工具网关凭执行令牌调用。",
)
def schedule_transfer_create(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    st = db.scalars(select(ScheduledTransfer).where(ScheduledTransfer.idempotency_key == idempotency_key)).first()
    if st is None:
        action = db.get(PendingAction, idempotency_key)
        st = ScheduledTransfer(
            id="ST" + secrets.token_hex(5), user_id=user_id, session_id=action.session_id if action else None,
            contact_id=args["contact_id"], account_no=args["account_no"], amount_fen=args["amount_fen"],
            memo=args.get("memo") or None, recurrence=args["recurrence"], day_of_month=args.get("day_of_month"),
            next_run_at=datetime.fromisoformat(args["first_run_at"]), level=action.level if action else "RED",
            idempotency_key=idempotency_key, runs=[])
        db.add(st)
        db.flush()
    return {"status": "succeeded", "schedule_id": st.id,
            "receipt_rows": [["任务编号", st.id], ["下次执行", f"{st.next_run_at:%Y-%m-%d %H:%M}"]]}


def active_schedules(db: Session, user_id: int) -> list[ScheduledTransfer]:
    return list(db.scalars(select(ScheduledTransfer).where(ScheduledTransfer.user_id == user_id,
                                                           ScheduledTransfer.status == "active")
                           .order_by(ScheduledTransfer.created_at)))


def _schedule_label(db: Session, st: ScheduledTransfer) -> str:
    c = db.get(Contact, st.contact_id)
    if st.recurrence == "monthly":
        when = f"每月 {st.day_of_month} 日"
    else:
        when = f"{st.next_run_at:%m-%d}" if st.next_run_at else "单次"
    return f"{when}给{c.masked_name}转 {fmt_yuan(st.amount_fen)} 元"


@bank_tool(
    name="schedule.cancel_propose", domain=DOMAIN, base_risk=Risk.GREEN,
    description="取消还没执行的定时 / 每月转账，生成确认卡。只生成待确认动作。",
    capability="取消定时转账",
    parameters={"type": "object", "properties": {
        "payee_hint": {"type": "string", "description": "给谁的定时转账，照抄用户原话；只有一个时可不填"},
    }},
)
def schedule_cancel_propose(ctx: ToolContext, payee_hint: str = "") -> ToolResult:
    db = ctx.db
    items = active_schedules(db, ctx.user_id)
    if payee_hint:
        ids = {c.id for c in resolve_payee(db, ctx.user_id, payee_hint)}
        items = [s for s in items if s.contact_id in ids] or items
    if not items:
        return ToolResult({"status": "none"}, notice_card("你现在没有生效中的定时转账。"))
    if len(items) > 1:
        return ToolResult({"status": "need_choice"}, notice_card(
            "你有几笔定时转账：" + "；".join(_schedule_label(db, s) for s in items) + "。要取消哪一笔？", "warning"))
    st = items[0]
    display = action_display(title="取消定时转账", verb="取消定时转账", rows=[["任务", _schedule_label(db, st)],
                             ["说明", "取消后不再执行，已经执行过的转账不受影响"]],
                             summary=f"取消定时转账：{_schedule_label(db, st)}", fields={"schedule_id": st.id})
    return propose_action(ctx, tool="schedule.cancel", args={"schedule_id": st.id}, display=display,
                          data={"schedule": _schedule_label(db, st)})


def schedule_cancel_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    st = db.get(ScheduledTransfer, args["schedule_id"])
    ok = st is not None and st.user_id == env.user_id and st.status == "active"
    return base_context(db, env, tool="schedule.cancel", block_reason=None if ok else "定时转账已经取消或执行完毕")


@bank_tool(
    name="schedule.cancel", domain=DOMAIN, base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    policy_context=schedule_cancel_context, description="取消定时转账。只能由工具网关凭执行令牌调用。",
)
def schedule_cancel(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    st = db.get(ScheduledTransfer, args["schedule_id"])
    if st is None or st.user_id != user_id:
        raise LedgerError("定时转账不存在")
    st.status, st.next_run_at = "cancelled", None
    return {"status": "succeeded", "schedule_id": st.id, "receipt_rows": []}


# ---------- 自动任务总览（F-XS-06） ----------

PLAN_STATUS_LABELS = {"draft": "待确认", "running": "进行中", "waiting": "等待临期执行", "rolling_back": "回退中",
                      "done": "已完成", "cancelled": "已取消", "handoff": "已转人工"}


@bank_tool(
    name="automation.list", domain=DOMAIN, base_risk=Risk.GREEN,
    description="查看用户设置的自动任务：心愿罐余额、定时转账、进行中的计划和花店蛋糕店订单。只读。",
    capability="查看心愿罐和自动任务", examples=("我设了哪些自动任务",),
)
def automation_list(ctx: ToolContext) -> ToolResult:
    db = ctx.db
    pots = [_pot_view(db, p) for p in active_pots(db, ctx.user_id)]
    schedules = [{"schedule_id": s.id, "label": _schedule_label(db, s), "next_run": f"{s.next_run_at:%Y-%m-%d %H:%M}",
                  "level": s.level} for s in active_schedules(db, ctx.user_id)]
    plans = [{"plan_id": p.id, "goal": p.goal, "status": PLAN_STATUS_LABELS.get(p.status, p.status),
              "due_at": f"{p.due_at:%m-%d %H:%M}" if p.due_at else None}
             for p in db.scalars(select(TaskPlan).where(TaskPlan.user_id == ctx.user_id,
                                                        TaskPlan.status.in_(("running", "waiting", "rolling_back"))))]
    orders = [{"order_id": o.id, "item": o.item, "amount_fen": o.amount_fen, "delivery": o.delivery}
              for o in paid_orders(db, ctx.user_id)]
    n = len(pots) + len(schedules) + len(plans) + len(orders)
    reply = f"你现在有 {len(pots)} 个心愿罐、{len(schedules)} 笔定时转账、{len(plans)} 个进行中的计划，详情见卡片。" if n \
        else "你现在没有设置心愿罐、定时转账或进行中的计划。"
    data = {"pots": pots, "schedules": schedules, "plans": plans, "orders": orders}
    return ToolResult(data, {"type": "automation", **data, "reply": reply,
                             "pots": [{**p, "balance": fmt_yuan(p["balance_fen"])} for p in pots],
                             "orders": [{**o, "amount": fmt_yuan(o["amount_fen"])} for o in orders]})


# ---------- 人工接管 ----------

def handoff(db: Session, *, user_id: int, session_id: str, trace_id: str, reason: str, package: dict) -> dict:
    """转人工：生成接管包写进审计，会话进入「已转人工」处置（熔断器的 handoff 等级，写操作全部暂停，查询照常），
    坐席在运营后台解除后交还。返回给用户的提示卡。"""
    from ..models import BreakerTrip

    trip = next((t for t in breaker.active_trips(db, user_id, session_id) if t.level == "handoff"), None)
    if trip is None:
        trip = BreakerTrip(id="B" + secrets.token_hex(8), user_id=user_id, session_id=session_id, trigger="user_handoff",
                           level="handoff", tools=["*"], reason=reason, trace_id=trace_id, created_at=now())
        db.add(trip)
        db.flush()
        audit.record(db, trace_id, "breaker_tripped", {"trip_id": trip.id, "trigger": trip.trigger, "level": "handoff",
                                                       "scope": "session", "tools": trip.tools, "reason": reason},
                     session_id)
    texts = db.scalars(select(ChatMessage.content).where(ChatMessage.session_id == session_id, ChatMessage.role == "user")
                       .order_by(ChatMessage.id.desc()).limit(5)).all()
    pending = db.scalars(select(PendingAction).where(PendingAction.session_id == session_id,
                                                     PendingAction.status.in_(actions.ACTIVE))).all()
    ticket = "H" + trip.id[1:9].upper()
    audit.record(db, trace_id, "handoff_requested", {
        "ticket": ticket, "trip_id": trip.id, "reason": reason, "recent_user_texts": [mask_pii(t) for t in reversed(texts)],
        "pending_actions": [{"action_id": a.id, "summary": a.display.get("summary"), "level": a.level} for a in pending],
        **package}, session_id)
    text = (f"已为你转接人工客服（工单 {ticket}），坐席会看到这次对话、计划进度和待确认事项。"
            "接入前转账、下单等办理暂停，查询照常可用。")
    return {**notice_card(text, "warning"), "handoff": {"ticket": ticket, "trip_id": trip.id}}


@bank_tool(
    name="handoff.request", domain=DOMAIN, base_risk=Risk.GREEN,
    description="用户要求转人工客服，或连续几次都没能理解用户时调用：生成接管工单，接入前写操作暂停。",
    capability="转人工客服",
    parameters={"type": "object", "properties": {
        "reason": {"type": "string", "description": "一句话原因，如「用户要求人工」"},
    }},
)
def handoff_request(ctx: ToolContext, reason: str = "") -> ToolResult:
    card = handoff(ctx.db, user_id=ctx.user_id, session_id=ctx.session.id, trace_id=ctx.trace_id,
                   reason=_clean(reason, 30) or "你申请了人工服务", package={})
    return ToolResult({"status": "handoff", "ticket": card["handoff"]["ticket"],
                       "note": "已转人工，接入前不要再发起办理。"}, card)


# ---------- 到期执行：按创建时的授权执行，执行前重新校验 ----------

def run_authorized(db: Session, *, user_id: int, session_id: str | None, trace_id: str, tool: str, args: dict,
                   display: dict, authorized: Risk, label: str) -> tuple[str, dict]:
    """返回 (结果, 卡片)。结果：succeeded / needs_confirm（风险变高，转为普通确认卡）/ blocked（熔断）/
    needs_user（余额不足等策略拒绝）/ failed。执行走和用户确认后同一条路径：令牌 → 网关 → 回执。"""
    block = breaker.check(db, user_id=user_id, tool=tool, session_id=session_id, args=args)
    if block is not None:
        audit.record(db, trace_id, "breaker_blocked", {"tool": tool, **block.as_dict()}, session_id)
        return "blocked", {**block.card, "text": f"{label}这次没有执行：{block.text}"}
    decision = decide(db, tool, args, PolicyEnv(user_id, "schedule"))
    audit.record(db, trace_id, "policy_decision", {"tool": tool, "summary": display["summary"], "mandate": True,
                                                   **decision.as_dict()}, session_id)
    if decision.block:
        return "needs_user", notice_card(f"{label}这次没有执行：{decision.block_reason}。", "warning")
    action = create_action(db, user_id=user_id, session_id=session_id, trace_id=trace_id, tool=tool, args=args,
                           display=display, decision=decision)
    if decision.level > authorized or decision.cooling_off_s:
        audit.record(db, trace_id, "mandate_escalated", {"action_id": action.id, "authorized": authorized.name,
                                                         "level": decision.level.name}, session_id)
        return "needs_confirm", confirm_card(action)
    out = actions._execute(db, action)  # 授权内：沿用安全外壳的执行步骤（签发令牌 → 网关核验 → 回执）
    return out.status, out.card


def _advance_schedule(st: ScheduledTransfer, ran_at: datetime) -> None:
    if st.recurrence == "monthly":
        st.next_run_at = next_monthly(st.day_of_month, max(ran_at, st.next_run_at))
    else:
        st.status, st.next_run_at = "completed", None


def run_due_transfers(db: Session, user_id: int, trace_id: str) -> list[tuple[str | None, str, dict]]:
    """到期的定时转账：返回 [(会话, 说明, 卡片)]。先推进下次执行时间再执行，重复扫描不会重复转账。"""
    out = []
    at = now()
    for st in db.scalars(select(ScheduledTransfer).where(ScheduledTransfer.user_id == user_id,
                                                         ScheduledTransfer.status == "active",
                                                         ScheduledTransfer.next_run_at <= at)).all():
        occurrence, label = st.next_run_at, _schedule_label(db, st)
        _advance_schedule(st, at)
        c = db.get(Contact, st.contact_id)
        debit = _debit(db, user_id)
        args = {"from_account_id": debit.id, "contact_id": c.id, "account_no": st.account_no,
                "amount_fen": st.amount_fen, "memo": st.memo or "", "schedule_id": st.id,
                "occurrence": occurrence.isoformat()}
        rows = [["收款人", c.masked_name], ["收款银行", f"{c.bank} · 尾号 {c.tail}"], ["定时任务", f"{st.id}（{label}）"]]
        if st.memo:
            rows.append(["备注", st.memo])
        display = action_display(title="定时转账", verb="定时转账", amount_fen=st.amount_fen, rows=rows,
                                 summary=f"定时转账：向{c.masked_name}（尾号{c.tail}）转账{fmt_yuan(st.amount_fen)}元",
                                 fields={"payee": c.masked_name, "tail": c.tail, "schedule_id": st.id})
        status, card = run_authorized(db, user_id=user_id, session_id=st.session_id, trace_id=trace_id,
                                      tool="transfer.create", args=args, display=display, authorized=Risk[st.level],
                                      label="定时转账")
        st.runs = [*st.runs, {"occurrence": occurrence.isoformat(), "at": at.isoformat(), "status": status}]
        note = {"succeeded": f"定时转账已按授权执行：{label}。",
                "needs_confirm": "定时转账到期了，但这次风险比设置时高，需要你重新确认。",
                }.get(status, card.get("text", "定时转账这次没有执行。"))
        out.append((st.session_id, note, card))
    return out


def release_expired_pots(db: Session, user_id: int, trace_id: str) -> list[tuple[str | None, str, dict]]:
    """过了到期日的心愿罐：剩余资金按存入时的约定自动退回活期（同样重新校验、走网关）。"""
    out = []
    for pot in active_pots(db, user_id):
        if pot.until is None or pot.until >= now().date():
            continue
        acct = db.get(Account, pot.account_id)
        debit = _debit(db, user_id)
        args = {"pot_id": pot.id, "amount_fen": acct.balance_fen, "to_account_id": debit.id}
        display = action_display(title="心愿罐到期", verb="释放心愿罐", amount_fen=acct.balance_fen or None,
                                 rows=[["心愿罐", f"「{pot.purpose}」"], ["到期日", f"{pot.until}"]],
                                 summary=f"心愿罐「{pot.purpose}」到期，{fmt_yuan(acct.balance_fen)}元退回活期",
                                 fields={"pot_id": pot.id})
        session_id = db.scalar(select(PendingAction.session_id).where(PendingAction.id == pot.idempotency_key))
        status, card = run_authorized(db, user_id=user_id, session_id=session_id, trace_id=trace_id, tool="pot.release",
                                      args=args, display=display, authorized=Risk.YELLOW, label="心愿罐到期释放")
        note = f"心愿罐「{pot.purpose}」已到期，剩余 {fmt_yuan(args['amount_fen'])} 元已退回活期。" if status == "succeeded" \
            else card.get("text", "心愿罐到期了，请确认释放。")
        out.append((session_id, note, card))
    return out


# ---------- 规则兜底：单步的心愿罐、订花订蛋糕、定时转账、总览 ----------

_POT_LOCK = re.compile(r"(存|放|锁|攒|留|转|划|预留|锁定)[^，,。；;]{0,12}心愿罐|心愿罐[^，,。；;]{0,4}(存|放|锁|攒|留|转|划)")
_LIST = re.compile(r"自动任务|定时转账有哪些|有哪些定时|心愿罐.{0,6}(多少|余额|情况|还有)|我设了哪些|我的心愿罐|我的计划")
_SCHEDULE_CANCEL = re.compile(r"(取消|停掉|关掉|不要了).{0,8}(定时|每月|每个月|自动)转账|(定时|每月|每个月|自动)转账.{0,6}(取消|停掉|不要了)")
_SCHEDULE = re.compile(r"(?P<when>每个?月的?[\d一二三四五六七八九十]{1,3}[日号]|下下?个?月[\d一二三四五六七八九十]{1,3}[日号]"
                       r"|[\d一二三四五六七八九十]{1,2}月[\d一二三四五六七八九十]{1,3}[日号]|下下?(?:个)?(?:周|星期|礼拜)[一二三四五六日天]"
                       r"|明天|后天|大后天)[^，,。]{0,6}?(?:给|向)(?P<payee>[^\s，,。！!？?]{1,10}?)(?:转账|转)(?!了)"
                       r"(?P<amount>[¥￥]?[\d零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万萬wWkK.]+(?:块|元)?)")
_ORDER = re.compile(r"(订|买|预订|定|帮我订|下单)(一|1)?(束|个|份|只)?(?P<what>鲜花|花束|花|蛋糕)")
_ORDER_CANCEL = re.compile(r"(取消|退掉|退了|不要).{0,6}(订单|蛋糕|花)|(订单|蛋糕|花)[^，,。]{0,6}(取消|退掉|不要了)")
_FOR_WHOM = re.compile(r"(给|送)(?P<who>我妈|妈妈|老妈|母亲|我爸|爸爸|老爸|爱人|老婆|媳妇|妻子|女朋友)")


def schedule_slots(text: str) -> dict | None:
    if m := _SCHEDULE.search(text):
        memo = (x := re.search(r"(?:备注|用途)[是为:：]?\s*(?P<memo>[^\s，,。！!？?]{1,20})", text)) and x.group("memo")
        return {"when_text": m.group("when"), "payee_hint": m.group("payee"), "amount_text": m.group("amount"),
                "memo": memo}
    return None


def pot_slots(text: str) -> dict | None:
    if not _POT_LOCK.search(text):
        return None
    clause = next((c for c in re.split(r"[，,。；;]|再|然后", text) if "心愿罐" in c), text)
    found = [m.group() for m in re.finditer(r"[¥￥]?[\d零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万萬wWkK.]+(?:块|元)?", clause)
             if parse_amount(m.group())]
    purpose = (x := re.search(r"(?:给|为)(?P<who>[^\s，,。]{1,6}?)(?:过)?(?P<what>生日|纪念日|旅行|旅游)", text)) and \
        (x.group("who").removeprefix("我") + x.group("what"))
    return {"amount_text": found[0] if found else "", "purpose": purpose or "", "until": None}


def rule_turn(ctx: ToolContext, text: str) -> tuple[str | None, list[tuple[str, dict, ToolResult]]] | None:
    """规则模式下的单步跨场景意图。返回 (回复, [(工具, 参数, 结果), ...])，回复为空时按最后一张卡片说；
    不是这类话返回 None。"""
    if _LIST.search(text):
        r = automation_list(ctx)
        return r.card["reply"], [("automation.list", {}, r)]
    if _SCHEDULE_CANCEL.search(text):
        args = {"payee_hint": (m := re.search(r"给(?P<p>[^\s，,。的]{1,6})的", text)) and m.group("p") or ""}
        return None, [("schedule.cancel_propose", args, schedule_cancel_propose(ctx, **args))]
    if slots := pot_slots(text):
        until = parse_date(text, now())
        slots["until"] = f"{until}" if until and until >= now().date() else None
        return None, [("pot.lock_propose", slots, pot_lock_propose(ctx, **slots))]
    if _ORDER_CANCEL.search(text) and paid_orders(ctx.db, ctx.user_id):
        args = {"order_hint": (m := re.search(r"蛋糕|花", text)) and m.group() or ""}
        return None, [("order.cancel_propose", args, order_cancel_propose(ctx, **args))]
    if m := _ORDER.search(text):
        who = (w := _FOR_WHOM.search(text)) and w.group("who") or ""
        d = parse_date(text, now())
        qargs = {"category": category_of(m.group("what")), "for_whom": who, "date": f"{d}" if d else None}
        q = merchant_quote(ctx, **qargs)
        calls = [("merchant.quote", qargs, q)]
        if q.data.get("status") != "ok":
            return q.data.get("message", "暂时没法询价。"), calls
        oargs = {"quote_id": q.data["quote_id"]}
        calls.append(("order.propose", oargs, order_propose(ctx, **oargs)))
        return f"已询价：{q.data['shop']} {q.data['item']}，请核对订单确认卡。", calls
    return None
