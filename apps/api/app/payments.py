"""支付协议、免密资格与额度口径；事实只从服务端主数据和动作账本读取。"""

from datetime import datetime, time, timedelta

from sqlalchemy import select

from .clock import now
from .core_mock import shops
from .models import Account, Card, Contact, PaymentConsent, PaymentMandate, PendingAction
from .money import fmt_yuan

AGREEMENT_VERSION = "2026.11"
INTIMACY_OPTIONS = ("父母", "伴侣", "子女", "兄弟姐妹", "密友")
CAPS = {"single_max_fen": 200_000, "daily_max_fen": 500_000}
DEFAULTS = {"single_max_fen": 50_000, "daily_max_fen": 200_000}


def consent(db, user_id: int) -> PaymentConsent:
    row = db.get(PaymentConsent, user_id)
    if row is None:
        row = PaymentConsent(user_id=user_id)
        db.add(row)
        db.flush()
    return row


def agreement_valid(db, user_id: int) -> bool:
    row = db.get(PaymentConsent, user_id)
    return bool(row and row.agreement_version == AGREEMENT_VERSION and row.agreement_signed_at
                and row.agreement_revoked_at is None)


def active_mandate(db, user_id: int) -> PaymentMandate | None:
    return db.scalar(select(PaymentMandate).where(
        PaymentMandate.user_id == user_id, PaymentMandate.status == "active",
        PaymentMandate.revoked_at.is_(None), PaymentMandate.valid_until > now(),
    ).order_by(PaymentMandate.id.desc()))


def payable(db, card: Card | None, user_id: int) -> bool:
    account = db.get(Account, card.account_id) if card and card.account_id else None
    return bool(card and card.user_id == user_id and card.kind == "debit" and card.status == "active" and account
                and account.user_id == user_id and account.kind == "debit" and account.status == "active")


def used_today(db, user_id: int, exclude: PendingAction | None = None) -> int:
    start = datetime.combine(now().date(), time.min)
    rows = db.scalars(select(PendingAction).where(
        PendingAction.user_id == user_id,
        PendingAction.status.in_(("pending", "awaiting_auth", "executing", "succeeded", "unknown")),
    ))
    total = 0
    for action in rows:
        if not action.args.get("nopwd") or (exclude and action.id == exclude.id):
            continue
        if action.status in ("pending", "awaiting_auth") and action.expires_at < now():
            continue
        at = (datetime.fromisoformat((action.result or {}).get("payment_executed_at", action.created_at.isoformat()))
              if action.status == "succeeded" else now())
        if at and start <= at < start + timedelta(days=1):
            total += action.args.get("amount_fen", 0)
    return total


def nopwd_facts(db, user_id: int, target, source_account_id: int, amount_fen: int,
                *, exclude: PendingAction | None = None) -> tuple[dict, dict | None]:
    mandate = active_mandate(db, user_id)
    if not mandate or not agreement_valid(db, user_id):
        return {}, None
    eligible = (isinstance(target, Contact) and target.user_id == user_id and target.status == "active"
                and target.intimacy in INTIMACY_OPTIONS and target.intimacy_verified_at is not None)
    eligible = eligible or (isinstance(target, shops.Shop) and target == shops.SHOPS.get(target.id)
                            and target.whitelisted)
    if not eligible:
        return {}, None
    card = db.get(Card, mandate.card_id)
    used = used_today(db, user_id, exclude)
    reasons = []
    if not payable(db, card, user_id) or card.account_id != source_account_id:
        reasons.append("非默认授权卡或付款卡不可用")
    if amount_fen > mandate.single_max_fen:
        reasons.append("超过单笔免密额度")
    if used + amount_fen > mandate.daily_max_fen:
        reasons.append("超过当日免密额度（含待确认金额）")
    if reasons:
        return {"nopwd_over": True, "nopwd_reason": "；".join(reasons)}, None
    return {"nopwd_ok": True}, {
        "single_max": fmt_yuan(mandate.single_max_fen), "daily_max": fmt_yuan(mandate.daily_max_fen),
        "used_today": fmt_yuan(used), "after": fmt_yuan(used + amount_fen),
    }


def apply_nopwd(db, args: dict, env, ctx, target) -> None:
    facts, display = nopwd_facts(db, env.user_id, target, args["from_account_id"], args["amount_fen"],
                                exclude=env.exclude)
    ctx.facts |= facts
    if env.exclude is not None and not args.get("nopwd"):
        ctx.facts.pop("nopwd_ok", None)
        return
    mandate = active_mandate(db, env.user_id)
    binding = {"mandate_id": mandate.id, "version": mandate.version} if mandate else None
    if args.get("nopwd") and (not facts.get("nopwd_ok") or args["nopwd"] != binding):
        ctx.block_reason = "免密授权、亲密关系或可用额度已变化"
    elif facts.get("nopwd_ok"):
        if env.exclude is None:
            args["nopwd"] = binding
        ctx.facts["nopwd_display"] = display


def scope(db, user_id: int) -> dict:
    contacts = db.scalars(select(Contact).where(Contact.user_id == user_id, Contact.status == "active",
                                               Contact.intimacy.in_(INTIMACY_OPTIONS),
                                               Contact.intimacy_verified_at.is_not(None)))
    return {"contacts": [{"id": row.id, "label": f"{row.relation or row.intimacy} · {row.masked_name}",
                           "intimacy": row.intimacy} for row in contacts],
            "merchants": list(dict.fromkeys(shop.name for shop in shops.SHOPS.values() if shop.whitelisted))}


def current_view(db, user_id: int) -> dict | None:
    row = active_mandate(db, user_id)
    if row is None:
        return None
    card = db.get(Card, row.card_id)
    used = used_today(db, user_id)
    return {"status": row.status, "card_id": row.card_id, "card_label": card_label(card),
            "single_max_fen": row.single_max_fen, "daily_max_fen": row.daily_max_fen,
            "single_max": fmt_yuan(row.single_max_fen), "daily_max": fmt_yuan(row.daily_max_fen),
            "used_today_fen": used, "used_today": fmt_yuan(used), "signed_at": f"{row.signed_at:%Y-%m-%d %H:%M}",
            "valid_until": f"{row.valid_until:%Y-%m-%d}"}


def card_label(card: Card) -> str:
    return f"{card.name} · 尾号 {card.card_no[-4:]}"


def mandate_card(db, user_id: int, mode: str = "form") -> dict:
    cards = []
    for card in db.scalars(select(Card).where(Card.user_id == user_id, Card.status != "unbound")):
        ok = payable(db, card, user_id)
        item = {"id": card.id, "label": card_label(card), "tail": card.card_no[-4:], "payable": ok}
        if not ok:
            item["reason"] = ("演示环境只支持用Ⅰ类借记卡主账户付款" if not card.account_id
                              else "虚拟卡不支持作为免密授权卡" if card.kind == "virtual" else "付款卡当前不可用")
        cards.append(item)
    current = current_view(db, user_id)
    defaults = {key: current[key] for key in DEFAULTS} if current else dict(DEFAULTS)
    card = {"type": "mandate", "mode": mode, "title": "免密支付授权", "cards": cards,
            "default_card_id": current["card_id"] if current else next((c["id"] for c in cards if c["payable"]), None),
            "caps": dict(CAPS), "defaults": defaults, "current": current, "scope": scope(db, user_id),
            "rules": ["仅对已验证亲密关系的联系人和合作商户生效；新收款人仍需强验证",
                      "免验证不免确认：每一笔仍需您核对并点击确认",
                      "超过单笔或每日额度、或不用默认卡付款，自动升级为强验证",
                      "开通、调高额度、换卡需人脸与短信验证；调低或关闭只需确认",
                      "授权有效期365天；可在我的支付授权中修改或关闭，反诈和注入检测不予豁免"]}
    if mode == "invite":
        card["invite_text"] = "以后给家人转账、在合作商户付款，额度内只需点确认，不用刷脸和输验证码。要开通吗？"
    return card


def agreement_card(db, user_id: int, action: PendingAction | None = None) -> dict:
    row = db.get(PaymentConsent, user_id)
    signed = agreement_valid(db, user_id)
    return {"type": "payment_agreement", "version": AGREEMENT_VERSION, "title": "支付服务协议",
            "status": "signed" if signed else "unsigned",
            "signed_at": f"{row.agreement_signed_at:%Y-%m-%d %H:%M}" if signed else None,
            "action_id": action.id if action else None, "pending_summary": action.display["summary"] if action else None,
            "intro": "首次付款前，请先了解言行助手的交易规则。同意后才会继续办理这笔业务。",
            "points": [
                {"icon": "🟢", "title": "查询类", "text": "查余额、查账单等只读操作直接办理"},
                {"icon": "🟡", "title": "需要确认", "text": "每笔支付均展示金额与收款对象，由您核对后点击确认"},
                {"icon": "🔴", "title": "需要强验证", "text": "超额、新收款人或风险交易需人脸或短信；高风险需两者均通过"},
                {"icon": "🔐", "title": "免密另行授权", "text": "本协议不自动开通免密。额度由您设置，单笔最高2,000元、每日最高5,000元"},
                {"icon": "↩", "title": "随时管理", "text": "在我的支付授权中可调低额度或关闭免密；撤回服务协议可联系人工客服"},
            ], "sections": [
                {"title": "一、适用范围", "body": "适用于转账、商户付款、定时转账授权和存入心愿罐。理财按其独立协议办理。"},
                {"title": "二、确认与验证", "body": "模型只能提议，不能代替您同意或完成验证。确认内容绑定交易摘要，过期或风险变化须重新办理。"},
                {"title": "三、额度与免密", "body": "免密仅对授权卡及已验证亲密联系人、合作商户生效，有效期365天；每日额度包含已成功与待确认金额。超过单笔、每日额度或改用其他卡须强验证。新收款人、拆单、频繁交易、反诈和注入风险不豁免。"},
                {"title": "四、授权变更与撤回", "body": "开通、提额、换卡需人脸加短信；调低或关闭只需确认。可在我的支付授权管理免密，联系人工客服申请撤回服务协议；撤回后须重新同意方可付款，已完成交易不受影响。"},
                {"title": "五、演示说明", "body": "本系统为比赛模拟环境，金额及验证均为演示数据，支付操作与授权变更保留审计记录。"},
            ]}


def invite_after(db, action: PendingAction) -> list[dict]:
    from .tools import REGISTRY

    if action.status != "succeeded" or not REGISTRY[action.tool].needs_payment_agreement:
        return []
    if active_mandate(db, action.user_id) or not agreement_valid(db, action.user_id) or action.args.get("pot_id"):
        return []
    row = consent(db, action.user_id)
    if row.mandate_offer_dismissed_at and row.mandate_offer_dismissed_at > now() - timedelta(days=30):
        return []
    account_id = action.args.get("from_account_id")
    if action.tool == "schedule.transfer_create":
        account_id = db.scalar(select(Account.id).where(Account.user_id == action.user_id, Account.kind == "debit"))
    card = db.scalar(select(Card).where(Card.user_id == action.user_id, Card.account_id == account_id)) if account_id else None
    return [mandate_card(db, action.user_id, "invite")] if payable(db, card, action.user_id) else []
