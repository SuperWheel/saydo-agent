"""转账领域：查收款人、按称呼或手机号转账（含反诈识别）、AA 拆分收款。

写操作都走 propose_action → 确认卡 →（🔴 强验证）→ 执行令牌 → 网关 → 审计，这里只负责组织参数和展示。
策略上下文（transfer_context）在当日累计之外补充：滚动 24 小时累计、拆单识别、诈骗嫌疑。
"""

import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..auth.actions import PolicyEnv, base_context, propose_action
from ..cards import action_display, clarify_card, notice_card
from ..clock import now
from ..config import get_settings
from ..core_mock import phone_directory
from ..core_mock.ledger import LedgerError, post
from ..models import AABill, AAShare, Account, ChatMessage, Contact, PendingAction, Transaction
from ..money import fmt_yuan, parse_amount
from ..nlu import amount_from_user, fraud, resolve_payee
from ..nlu.transfer import ME_WORDS, is_phone_hint, mask_phone, resolve_phone, split_names
from ..policy import PolicyContext, Risk, load_rules
from ..security import mask_pii
from . import REGISTRY, ToolContext, ToolResult, bank_tool

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_COUNTED = ("pending", "awaiting_auth", "executing", "succeeded", "unknown")  # 占用额度的动作状态


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


def _rule(rule_id: str) -> dict | None:
    return next((r for r in load_rules()["rules"] if r["id"] == rule_id and r.get("enabled", True)), None)


def _saved_contact(db: Session, user_id: int, account_no: str) -> Contact | None:
    return db.scalars(select(Contact).where(Contact.user_id == user_id, Contact.account_no == account_no)).first()


def _debit_account(db: Session, user_id: int) -> Account:
    return db.scalars(select(Account).where(Account.user_id == user_id, Account.kind == "debit")).one()


# ---------- 查收款人 ----------

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


# ---------- 转账提议 ----------

@bank_tool(
    name="transfer.propose", domain="transfer", base_risk=Risk.GREEN,
    description="为一笔转账生成确认卡交给用户；收款人可以是称呼、姓名或手机号。查找收款人、同名时出选择卡都由它完成，"
                "不需要先调 contact_search。只生成待确认动作，不会转账；风险等级和是否需要验证由系统决定。",
    capability="给收款人或手机号转账", examples=("给我妈转500",),
    parameters={"type": "object", "properties": {
        "payee_hint": {"type": "string", "description": "收款人称呼、姓名或手机号，照抄用户原话，如「我妈」「张伟」「房东」"
                                                        "「139****5678」（手机号已脱敏，原样照抄即可）"},
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

    if contact_id is None and is_phone_hint(payee_hint):
        return _propose_by_phone(ctx, payee_hint, amount_fen, memo)
    if contact_id is not None:
        c = db.get(Contact, contact_id)
        candidates = [c] if c and c.user_id == ctx.user_id else []
    else:
        candidates = resolve_payee(db, ctx.user_id, payee_hint)
    if not candidates:
        return ToolResult({"status": "payee_not_found", "hint": payee_hint},
                          notice_card(f"没有在你的收款人里找到「{payee_hint}」。可以换个称呼，或者直接说对方的手机号。", "warning"))
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
    _set_question(ctx, None)
    return _propose_to(ctx, amount_fen=amount_fen, memo=memo, contact=candidates[0])


def _propose_by_phone(ctx: ToolContext, hint: str, amount_fen: int | None, memo: str) -> ToolResult:
    """F-TR-02：按手机号查绑定账户。模型和审计只见脱敏号码，完整号码从用户原话里还原（来源校验）。"""
    phones = resolve_phone(hint, ctx.user_texts)
    if len(phones) > 1:
        return ToolResult({"status": "phone_ambiguous", "message": "用户提到了多个尾号相同的手机号，请让用户说完整号码"},
                          notice_card("你提到的几个手机号尾号相同，请说一下要转给哪个完整号码。", "warning"))
    if not phones:
        audit.record(ctx.db, ctx.trace_id, "provenance_reject", {"field": "phone", "hint": mask_pii(hint)}, ctx.session.id)
        return ToolResult({"status": "phone_not_from_user", "message": "手机号必须来自用户原话，请让用户再说一遍完整号码"},
                          notice_card("请再说一遍收款人的完整手机号。", "warning"))
    phone = phones[0]
    b = phone_directory.lookup(phone)
    if b is None:
        return ToolResult({"status": "phone_not_registered", "phone": mask_phone(phone)},
                          notice_card(f"手机号 {mask_phone(phone)} 没有绑定可收款的账户，请核对号码。", "warning"))
    contact = _saved_contact(ctx.db, ctx.user_id, b.account_no)
    if amount_fen is None:
        _set_question(ctx, {"kind": "amount", "contact_ids": [], "phone": mask_phone(phone), "memo": memo})
        return ToolResult({"status": "need_amount", "phone": mask_phone(phone), "payee": b.masked_name})
    _set_question(ctx, None)
    return _propose_to(ctx, amount_fen=amount_fen, memo=memo, contact=contact, binding=b, phone=phone)


def _propose_to(ctx: ToolContext, *, amount_fen: int, memo: str, contact: Contact | None = None,
                binding: phone_directory.PhoneBinding | None = None, phone: str | None = None) -> ToolResult:
    """收款人已确定：组织参数、识别诈骗话术、生成确认卡。contact 为空表示按手机号查到的新收款人。"""
    db = ctx.db
    acct = _debit_account(db, ctx.user_id)
    if contact is not None:
        name, bank, account_no = contact.masked_name, contact.bank, contact.account_no
        args = {"from_account_id": acct.id, "contact_id": contact.id, "account_no": account_no,
                "amount_fen": amount_fen, "memo": memo}
        view = _contact_view(contact)
    else:
        name, bank, account_no = binding.masked_name, binding.bank, binding.account_no
        args = {"from_account_id": acct.id, "contact_id": None, "account_no": account_no, "payee_name": binding.name,
                "bank": bank, "amount_fen": amount_fen, "memo": memo}
        view = {"name": name, "bank": bank, "tail": account_no[-4:], "note": "", "known_payee": False}
    tail = account_no[-4:]

    hits = _fraud_hits(ctx, amount_fen=amount_fen, payee_known=view["known_payee"])
    if hits:
        args["fraud_hits"] = hits  # 进 digest：执行前重新校验时诈骗嫌疑还在

    rows = [["收款人", name]]
    if phone:
        rows.append(["手机号", mask_phone(phone)])
    rows.append(["收款银行", f"{bank} · 尾号 {tail}"])
    if contact is None:
        rows.append(["说明", "按手机号查到的新收款人，请核对户名；转账成功后会保存到收款人"])
    if memo:
        rows.append(["备注", memo])
    fields = {"payee": name, "bank": bank, "tail": tail, "memo": memo}
    if phone:
        fields["phone"] = mask_phone(phone)
    display = action_display(title="转账确认", verb="转账", amount_fen=amount_fen, rows=rows,
                             summary=f"向{name}（尾号{tail}）转账{fmt_yuan(amount_fen)}元", fields=fields)
    data = {**view, "amount_yuan": fmt_yuan(amount_fen)}
    if phone:
        data["phone"] = mask_phone(phone)
    if hits:
        display["warning"] = data["fraud_warning"] = fraud.warning(hits)
        data["cooling_off_s"] = (_rule("anti_fraud") or {}).get("cooling_off_s", 0)
        data["note_fraud"] = "请用一句话提醒用户这笔转账疑似诈骗、先核实对方身份，不要催用户确认。"
    return propose_action(ctx, tool="transfer.create", args=args, display=display, data=data)


def _fraud_hits(ctx: ToolContext, *, amount_fen: int, payee_known: bool) -> list[str]:
    """看本会话里用户说过的所有话（不只最近几句）：诈骗话术，或「新收款人 + 大额 + 催促」的组合。"""
    texts = ctx.db.scalars(select(ChatMessage.content).where(
        ChatMessage.session_id == ctx.session.id, ChatMessage.role == "user").order_by(ChatMessage.id.desc()).limit(30)).all()
    found = fraud.scan(list(texts))
    hits = list(found.hits)
    rule = _rule("anti_fraud")
    if rule and found.urgent and not payee_known and amount_fen > rule.get("combo_min_fen", 0):
        hits.append(fraud.COMBO)
    return hits


# ---------- 转账执行 ----------

def _recent_actions(db: Session, env: PolicyEnv, tools: list[str], window_s: int) -> list[PendingAction]:
    rows = db.scalars(select(PendingAction).where(
        PendingAction.user_id == env.user_id, PendingAction.tool.in_(tools), PendingAction.status.in_(_COUNTED),
        PendingAction.created_at >= now() - timedelta(seconds=window_s),
    )).all()
    return [a for a in rows if env.exclude is None or a.id != env.exclude.id]


def _window_facts(db: Session, args: dict, env: PolicyEnv, ctx: PolicyContext) -> dict:
    """滚动 24 小时累计与拆单识别。口径和当日累计一致：经本系统发起的转出，含待确认预占。"""
    facts = {}
    amount = args["amount_fen"]
    if rule := _rule("rolling_24h_outflow"):
        money_tools = [s.name for s in REGISTRY.values() if s.moves_money]
        rolling = sum(a.args["amount_fen"] for a in _recent_actions(db, env, money_tools, rule["window_s"])) + amount
        # 滚动窗口总是覆盖今天，当日累计已超限时交给 cumulative_outflow，这里只补跨零点的情况
        facts["rolling_24h_over"] = rolling > rule["yellow_max_fen"] >= ctx.outflow_today_fen + amount
    if rule := _rule("split_evasion"):
        small = rule["small_max_fen"]
        prior = [a.args["amount_fen"] for a in _recent_actions(db, env, ["transfer.create"], rule["window_s"])
                 if a.args["account_no"] == args["account_no"] and a.args["amount_fen"] <= small]
        facts["split_suspected"] = (amount <= small and len(prior) >= rule["min_prior"]
                                    and sum(prior) + amount > rule["sum_over_fen"])
    return facts


def transfer_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    if args.get("contact_id") is not None:
        contact = db.get(Contact, args["contact_id"])
        changed = contact is None or contact.account_no != args["account_no"]
        known = not changed and contact.first_transfer_at is not None
        block = "收款人信息已变化" if changed else None
    else:  # 按手机号查到的收款人：再核一次目录，户名或账号对不上就拒绝
        b = phone_directory.by_account(args["account_no"])
        block = None if b and b.name == args.get("payee_name") else "收款账户校验失败"
        saved = _saved_contact(db, env.user_id, args["account_no"])
        known = saved is not None and saved.first_transfer_at is not None
    ctx = base_context(db, env, tool="transfer.create", amount_fen=args["amount_fen"], payee_known=known,
                       block_reason=block)
    ctx.facts |= _window_facts(db, args, env, ctx)
    ctx.facts["fraud_suspected"] = bool(args.get("fraud_hits"))
    return ctx


@bank_tool(
    name="transfer.create", domain="transfer", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    moves_money=True, policy_context=transfer_context, description="执行转账。只能由工具网关凭执行令牌调用。",
)
def transfer_create(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    acct = db.get(Account, args["from_account_id"])
    existing = db.scalars(select(Transaction).where(Transaction.idempotency_key == idempotency_key)).first()
    if existing:  # 幂等：同一个动作只会记一笔账
        return _transfer_result(existing, acct)
    if args.get("contact_id") is not None:
        contact = db.get(Contact, args["contact_id"])
        ok = contact is not None and contact.account_no == args["account_no"]
        name, bank = (contact.name, contact.bank) if ok else ("", "")
    else:
        b = phone_directory.by_account(args["account_no"])
        ok = b is not None and b.name == args["payee_name"]
        contact, name, bank = _saved_contact(db, user_id, args["account_no"]), args["payee_name"], args["bank"]
    if acct is None or acct.user_id != user_id or not ok:
        raise LedgerError("账户或收款人校验失败")
    txn = post(db, account=acct, direction="out", amount_fen=args["amount_fen"], counterparty=name,
               contra_code=f"CLEARING:{bank}", contact_id=contact.id if contact else None, memo=args.get("memo") or None,
               category="转账", channel="agent", idempotency_key=idempotency_key)
    if contact is None:  # 按手机号首次转账成功：保存为收款人，下次按姓名也能找到
        contact = Contact(user_id=user_id, name=name, account_no=args["account_no"], bank=bank, aliases=[],
                          note="手机号转账")
        db.add(contact)
        db.flush()
        txn.contact_id = contact.id
    if contact.first_transfer_at is None:
        contact.first_transfer_at = txn.created_at
    return _transfer_result(txn, acct)


def _transfer_result(txn: Transaction, acct: Account) -> dict:
    at = f"{txn.created_at:%Y-%m-%d %H:%M:%S}"
    return {"status": "succeeded", "txn_no": txn.txn_no, "at": at, "amount_fen": txn.amount_fen,
            "balance_after_fen": acct.balance_fen,
            "receipt_rows": [["流水号", txn.txn_no], ["时间", at], ["转账后余额", f"¥{fmt_yuan(acct.balance_fen)}"]]}


# ---------- AA 拆分收款（F-TR-06） ----------

def split_aa(total_fen: int, n: int, *, absorb_last: bool = False) -> list[int]:
    """按分精确拆分：q = 总额 div n，r = 总额 mod n，合计恒等于总额。

    默认名单前 r 位各多 1 分（100 元 3 人 → 33.34 / 33.33 / 33.33）；absorb_last 时尾差全部算在最后一位。
    """
    q, r = divmod(total_fen, n)
    if absorb_last:
        return [q] * (n - 1) + [q + r]
    return [q + 1 if i < r else q for i in range(n)]


def plan_aa(total_fen: int, names: list[str], *, include_me: bool, me_absorbs: bool) -> tuple[list[list], str, str]:
    """返回 (份额 [[称呼, 分, 是否本人], ...], 尾差说明, 尾差规则)。名单按用户说的顺序，用户本人排最后。"""
    people = [*names, *(["你"] if include_me else [])]
    n = len(people)
    q, r = divmod(total_fen, n)
    if me_absorbs and not include_me:  # 本人不分摊但愿意承担尾差：每人只收 q，零头不收
        amounts = [q] * n
    else:
        amounts = split_aa(total_fen, n, absorb_last=me_absorbs)
    shares = [[p, a, include_me and i == n - 1] for i, (p, a) in enumerate(zip(people, amounts))]
    base = f"{fmt_yuan(total_fen)} 元 ÷ {n} 人 = 每人 {fmt_yuan(q)} 元"
    if r == 0:
        return shares, base + "，正好除尽，没有尾差", "front"
    if me_absorbs:
        return shares, base + f"，余 {fmt_yuan(r)} 元由你承担", "initiator"
    each = "各" if r > 1 else ""
    return shares, base + f"，余 {fmt_yuan(r)} 元按名单顺序由{'、'.join(people[:r])}{each}多付 0.01 元", "front"


def _clean_name(raw: str) -> str:
    return mask_pii(_CTRL.sub("", raw.strip()))[:10]


@bank_tool(
    name="aa.propose", domain="transfer", base_risk=Risk.GREEN,
    description="AA 拆分收款：按「分」精确拆分总额并生成确认卡，用户确认后向其他参与人发送收款请求。"
                "只生成待确认动作；拆分金额和尾差由系统计算，不要自己算。",
    capability="AA 拆分收款", examples=("昨晚火锅856，我和小王、小李、小张AA",),
    parameters={"type": "object", "properties": {
        "total_text": {"type": "string", "description": "总金额，照抄用户原话，如「856」「两百」，不要换算"},
        "participants": {"type": "string", "description": "除用户本人以外的参与人，照抄用户原话，用顿号分隔，如「小王、小李、小张」"},
        "include_me": {"type": "boolean", "description": "用户本人是否也分摊一份，默认 true；用户说自己不参与时填 false"},
        "me_absorb_remainder": {"type": "boolean", "description": "用户说「尾差我来出」「零头算我的」时填 true"},
        "title": {"type": "string", "description": "事由，如「火锅」「打车」，用户没说就不填"},
    }, "required": ["total_text", "participants"]},
)
def propose_aa(ctx: ToolContext, total_text: str, participants: str, include_me: bool = True,
               me_absorb_remainder: bool = False, title: str | None = None) -> ToolResult:
    total = parse_amount(total_text) if total_text else None
    if total is None:
        return ToolResult({"status": "need_amount"}, notice_card("这次 AA 一共多少钱？"))
    if not amount_from_user(total, ctx.user_texts):
        audit.record(ctx.db, ctx.trace_id, "provenance_reject", {"field": "amount", "amount_text": total_text}, ctx.session.id)
        return ToolResult({"status": "amount_not_from_user", "message": "金额必须来自用户原话，请向用户确认金额"},
                          notice_card("请再说一下这次 AA 一共多少钱。", "warning"))
    names = [n for n in (_clean_name(x) for x in split_names(participants)) if n and n not in ME_WORDS]
    if not names:
        return ToolResult({"status": "need_participants"}, notice_card("要和哪几位 AA？请说一下他们的称呼。"))
    if missing := [n for n in names if not any(n in t for t in ctx.user_texts)]:
        audit.record(ctx.db, ctx.trace_id, "provenance_reject", {"field": "participants", "names": missing}, ctx.session.id)
        return ToolResult({"status": "participant_not_from_user", "message": "参与人必须来自用户原话"},
                          notice_card("请再说一下要和哪几位 AA。", "warning"))
    if len(set(names)) != len(names):
        return ToolResult({"status": "duplicate_participants"}, notice_card("参与人里有重名，请用不同的称呼区分。", "warning"))
    n = len(names) + include_me
    if n < 2 or len(names) > 20:
        return ToolResult({"status": "bad_participants"}, notice_card("AA 需要 2 到 21 人分摊。", "warning"))
    if total < n:
        return ToolResult({"status": "amount_too_small"}, notice_card("金额太小，不够每人分到 1 分钱。", "warning"))

    title = _clean_name(title or "")
    shares, rule_text, rule = plan_aa(total, names, include_me=include_me, me_absorbs=me_absorb_remainder)
    collect = sum(a for _, a, me in shares if not me)
    rows = ([["事由", title]] if title else []) + [["总额", f"¥{fmt_yuan(total)}"]]
    rows += [["你自己", f"¥{fmt_yuan(a)}（自付，不发起收款）"] if me else [p, f"¥{fmt_yuan(a)}"] for p, a, me in shares]
    rows += [["尾差", rule_text], ["收款", f"确认后向 {len(names)} 人发送收款请求，共 ¥{fmt_yuan(collect)}"]]
    display = action_display(
        title="AA 收款确认", verb="发起 AA 收款", rows=rows,
        summary=f"向{'、'.join(names)}发起{f'「{title}」' if title else ''}AA 收款，共{fmt_yuan(collect)}元",
        fields={"total_fen": total, "shares": shares, "rule": rule, "rule_text": rule_text},
    )
    args = {"title": title, "total_fen": total, "remainder_rule": rule, "shares": shares}
    data = {"title": title, "total_yuan": fmt_yuan(total), "collect_yuan": fmt_yuan(collect), "rule": rule_text,
            "shares": [{"name": p, "amount_yuan": fmt_yuan(a), "is_me": me} for p, a, me in shares]}
    return propose_action(ctx, tool="aa.create", args=args, display=display, data=data)


@bank_tool(
    name="aa.create", domain="transfer", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    description="创建 AA 收款单并向参与人发送收款请求。只能由工具网关凭执行令牌调用。",
)
def aa_create(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    bill_id = "AA" + idempotency_key.removeprefix("A")
    bill = db.get(AABill, bill_id)
    if bill is None:  # 幂等：同一个动作只建一张收款单
        bill = AABill(id=bill_id, user_id=user_id, title=args["title"], total_fen=args["total_fen"],
                      remainder_rule=args["remainder_rule"], status="sent", created_at=now())
        db.add(bill)
        db.add_all([AAShare(bill_id=bill_id, seq=i, name=p, is_initiator=me, amount_fen=a, status="self" if me else "sent")
                    for i, (p, a, me) in enumerate(args["shares"])])
        db.flush()
    others = sum(1 for *_, me in args["shares"] if not me)
    return {"status": "succeeded", "bill_id": bill_id,
            "receipt_rows": [["收款单号", bill_id], ["状态", f"已向 {others} 人发送收款请求"]]}


_SHARE_LABELS = {"self": "本人", "sent": "已发送", "paid": "已付清"}
_BILL_LABELS = {"sent": "已发送", "paid": "已付清"}


def _shares(db: Session, bill: AABill) -> list[AAShare]:
    return list(db.scalars(select(AAShare).where(AAShare.bill_id == bill.id).order_by(AAShare.seq)))


def aa_card(db: Session, bill: AABill) -> dict:
    shares = _shares(db, bill)
    others = [s for s in shares if not s.is_initiator]
    return {
        "type": "aa", "bill_id": bill.id, "title": bill.title, "total": fmt_yuan(bill.total_fen),
        "status": bill.status, "status_label": _BILL_LABELS[bill.status],
        "received": fmt_yuan(sum(s.amount_fen for s in others if s.status == "paid")),
        "expected": fmt_yuan(sum(s.amount_fen for s in others)),
        "shares": [{"id": s.id, "name": "你自己" if s.is_initiator else s.name, "amount": fmt_yuan(s.amount_fen),
                    "status": s.status, "status_label": _SHARE_LABELS[s.status]} for s in shares],
        "created_at": f"{bill.created_at:%Y-%m-%d %H:%M}", "demo": get_settings().demo_mode,
    }


@bank_tool(
    name="aa.status", domain="transfer", base_risk=Risk.GREEN,
    description="查询 AA 收款进度：每位参与人是「已发送」还是「已付清」。",
    capability="查 AA 收款进度", examples=("AA收款收齐了吗",),
    parameters={"type": "object", "properties": {
        "title": {"type": "string", "description": "事由关键词，如「火锅」；不填返回最近一笔"},
    }},
)
def aa_status(ctx: ToolContext, title: str | None = None) -> ToolResult:
    q = select(AABill).where(AABill.user_id == ctx.user_id).order_by(AABill.created_at.desc())
    bills = list(ctx.db.scalars(q))
    bill = next((b for b in bills if title and title in b.title), bills[0] if bills else None)
    if bill is None:
        return ToolResult({"status": "none", "summary": "你还没有发起过 AA 收款。"}, notice_card("你还没有发起过 AA 收款。"))
    card = aa_card(ctx.db, bill)
    others = [s for s in card["shares"] if s["status"] != "self"]
    paid = sum(1 for s in others if s["status"] == "paid")
    summary = f"{f'「{bill.title}」' if bill.title else ''}AA 收款：{paid}/{len(others)} 人已付清。"
    data = {"title": bill.title, "total_yuan": card["total"], "status_label": card["status_label"],
            "received_yuan": card["received"], "expected_yuan": card["expected"], "summary": summary,
            "shares": [{"name": s["name"], "amount_yuan": s["amount"], "status": s["status_label"]} for s in card["shares"]]}
    return ToolResult(data, card)


def aa_mark_paid(db: Session, *, user_id: int, share_id: int) -> dict:
    """模拟参与人付款（演示用）：对方银行入账一笔，份额改为已付清；其他人都付清时整单改为已付清。"""
    share = db.get(AAShare, share_id)
    bill = db.get(AABill, share.bill_id) if share else None
    if bill is None or bill.user_id != user_id or share.is_initiator:
        raise LookupError("收款记录不存在")
    if share.status == "sent":
        post(db, account=_debit_account(db, user_id), direction="in", amount_fen=share.amount_fen,
             counterparty=share.name, contra_code="CLEARING:INTERBANK", category="AA收款", memo=f"{bill.title} AA",
             channel="system")
        share.status, share.paid_at = "paid", now()
        if all(s.status != "sent" for s in _shares(db, bill)):
            bill.status = "paid"
        audit.record(db, "aa", "aa_share_paid", {"bill_id": bill.id, "share_id": share.id, "amount_fen": share.amount_fen})
    return aa_card(db, bill)
