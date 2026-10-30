"""订阅代扣（WS6）：识别 + 总览 🟢（F-SB-01/02）、临期扣费提醒 🟢（F-SB-03）、取消（F-SB-04）。

取消分三种情况，如实告知结果（技术文档 §7.3）：
- 只在本行：扣费只靠本行协议（如话费自动缴费），停止本行代扣授权 🟡 就不会再扣；
- 只在商户：本行没有这笔扣费的协议（如经第三方支付平台扣款），本行办不了，只给商户侧取消指引；
- 两者都要：停止本行代扣授权 🟡，同时给出商户侧取消指引。
停止本行代扣只是阻止付款方式，商户侧的订阅可能仍然有效，所以回复、确认卡、回执里都不能说「已解约」「已取消订阅」
（事实校验 agent/grounding.py 兜底）。停止代扣属于止损操作（stop_loss），熔断开着也能办。

数字全部由 analytics/recurring.py 算出；商户只用可信标签（登记商户规范名或「未登记商户」），原文只上卡片。
"""

import re
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics import recurring
from ..analytics.recurring import Detection, Recurring
from ..agent import quarantine
from ..auth.actions import PolicyEnv, base_context, propose_action
from ..cards import SOURCE_CORE, action_display, notice_card
from ..clock import now
from ..core_mock.ledger import LedgerError
from ..core_mock.merchants import MERCHANTS
from ..core_mock.subscriptions import AFTER_STOP, MERCHANT_SIDE, cancel_guide
from ..models import Account, DebitAgreement
from ..money import fmt_yuan
from ..policy import PolicyContext, Risk
from . import ToolContext, ToolResult, bank_tool

AGREEMENT_KIND = {"withhold": "本行代扣（自动缴费）", "nopwd": "本行免密支付"}
NOT_OURS = "非本行协议扣款（可能经第三方支付平台）"
# PRD F-SB-04 的原话：只停了代扣时要这样说，不能说成「已解约」
ONLY_BILLING = "付款方式已阻止，商户订阅可能仍然有效"
METHOD_NOTE = "识别口径：近 400 天同一商户、金额接近、间隔规律的扣费；置信度 = 0.4×间隔规律 + 0.3×金额一致 + 0.2×笔数 + 0.1×商户可信度"
MODEL_NOTE = ("停止本行代扣只是阻止付款方式，商户侧订阅可能仍然有效。不要说「已解约」「已取消订阅」「已退订」；"
              "商户侧要用户自己按指引取消。")
DEFAULT_WINDOW_DAYS = 7
_GENERIC = {"音乐": "音乐", "视频": "视频", "健身": "Keep", "运动": "Keep", "话费": "话费"}  # 没说商户名时的大类词


# ---------- 识别结果 + 协议状态 ----------

def detection(ctx_db: Session, user_id: int) -> Detection:
    return recurring.load_detection(ctx_db, user_id, now())


def _agreements(db: Session, user_id: int) -> dict[str, DebitAgreement]:
    """登记商户 → 代扣协议。同一商户有多份时，有效的优先、新签的优先。"""
    out: dict[str, DebitAgreement] = {}
    for a in db.scalars(select(DebitAgreement).where(DebitAgreement.user_id == user_id)
                        .order_by(DebitAgreement.signed_at, DebitAgreement.id)):
        if a.merchant not in out or a.status == "active" or out[a.merchant].status != "active":
            out[a.merchant] = a
    return out


def _state(r: Recurring, agr: DebitAgreement | None) -> str:
    if agr is not None and agr.status == "stopped":
        return "billing_stopped"
    return "overdue" if r.overdue else "active"


STATE_LABEL = {"active": "扣费中", "billing_stopped": "本行代扣已停止", "overdue": "最近一期未见扣款"}


def _md(d: date) -> str:
    return f"{d.month}月{d.day}日"


def _days_left(d: date) -> int:
    return (d - now().date()).days


def _when(days: int) -> str:
    return "今天" if days == 0 else "明天" if days == 1 else f"{days} 天后"


def _name(r: Recurring) -> str:
    return "一家未登记商户（名称见卡片）" if r.party == quarantine.UNREGISTERED else r.party


def _view(r: Recurring, agr: DebitAgreement | None) -> dict:
    """给模型看的一条订阅：只有可信标签和算好的数字。"""
    state = _state(r, agr)
    v = {"merchant": r.party, "cycle": r.cycle.label, "amount_yuan": fmt_yuan(r.amount_fen), "amount_fen": r.amount_fen,
         "yearly_yuan": fmt_yuan(r.yearly_fen), "confidence": r.confidence, "confidence_pct": f"{round(r.confidence * 100)}%",
         "last_charge": f"{r.last_at:%Y-%m-%d}", "count": len(r.txns), "status": STATE_LABEL[state],
         "pay_via": AGREEMENT_KIND[agr.kind] if agr else NOT_OURS}
    if state == "billing_stopped":
        v["note"] = ONLY_BILLING
    else:
        v |= {"next_due": f"{r.next_due:%Y-%m-%d}", "days_left": _days_left(r.next_due)}
    if r.price_up_from_fen:
        v |= {"price_up": True, "previous_yuan": fmt_yuan(r.price_up_from_fen)}
    return v


def _tags(r: Recurring, agr: DebitAgreement | None) -> list[str]:
    tags = []
    if r.price_up_from_fen:
        tags.append(f"涨价（上期 ¥{fmt_yuan(r.price_up_from_fen)}）")
    if _state(r, agr) == "active" and _days_left(r.next_due) <= max(r.cycle.lead_days):
        tags.append("即将扣费")
    return tags


def _card_item(r: Recurring, agr: DebitAgreement | None) -> dict:
    state = _state(r, agr)
    item = {"name": r.display, "cycle": r.cycle.label, "amount": fmt_yuan(r.amount_fen), "state": state,
            "state_label": STATE_LABEL[state], "pay_via": AGREEMENT_KIND[agr.kind] if agr else NOT_OURS,
            "confidence": f"{round(r.confidence * 100)}%", "tags": _tags(r, agr)}
    if state == "billing_stopped":
        item["next"] = ONLY_BILLING
    else:
        item["next"] = f"{_md(r.next_due)}（{_when(_days_left(r.next_due))}）"
    return item


def _meta() -> dict:
    return {"as_of": now().strftime("%Y-%m-%d %H:%M"), "source": SOURCE_CORE}


# ---------- F-SB-01/02 识别 + 总览 🟢 ----------

@bank_tool(
    name="sub.list", domain="subscription", base_risk=Risk.GREEN,
    description="从流水里识别订阅和周期性自动扣费（会员、话费等），列出每个订阅的金额、周期、下次扣费日、扣费方式、"
                "识别置信度，以及每月 / 每年合计，如「我每个月订阅花多少」「我有哪些自动扣费」。",
    capability="查订阅和自动扣费（每月花多少、下次扣费日）", examples=("我每个月订阅花多少",),
)
def overview(ctx: ToolContext) -> ToolResult:
    det = detection(ctx.db, ctx.user_id)
    agrs = _agreements(ctx.db, ctx.user_id)
    subs = det.subscriptions
    active = [r for r in subs if _state(r, agrs.get(r.party)) == "active"]
    yearly = sum(r.yearly_fen for r in active)
    monthly = (yearly + 6) // 12  # 年化合计 ÷ 12，四舍五入到分
    nxt = min(active, key=lambda r: (r.next_due, r.party), default=None)
    if not subs:
        reply = "没有在你的流水里识别到订阅或周期性自动扣费。"
    else:
        reply = f"识别到 {len(subs)} 个订阅，预计每月扣费 {fmt_yuan(monthly)} 元，一年约 {fmt_yuan(yearly)} 元。"
        if nxt:
            reply += f"最近一笔是{_md(nxt.next_due)}{_name(nxt)} {fmt_yuan(nxt.amount_fen)} 元。"
        stopped = len(subs) - len(active)
        if stopped:
            reply += f"另有 {stopped} 个已停止本行代扣或最近一期未扣款，不计入合计。"
    data = {
        "subscriptions": [_view(r, agrs.get(r.party)) for r in subs], "count": len(subs),
        "monthly_total_yuan": fmt_yuan(monthly), "monthly_total_fen": monthly,
        "yearly_total_yuan": fmt_yuan(yearly), "yearly_total_fen": yearly, "counted": len(active),
        "to_confirm": [{"merchant": r.party, "cycle": r.cycle.label, "amount_yuan": fmt_yuan(r.amount_fen),
                        "confidence": r.confidence} for r in det.ask],
        "excluded": [{"merchant": e["party"], "reason": e["reason"]} for e in det.excluded],
        "summary": reply, "note": MODEL_NOTE,
    }
    if det.ask:
        data["to_confirm_note"] = "这几笔像周期扣费但把握不大，没有计入合计；可以问用户是不是周期付款"
    card = {
        "type": "sub_overview", "title": "我的订阅", "count": len(subs), "monthly_total": fmt_yuan(monthly),
        "yearly_total": fmt_yuan(yearly), "items": [_card_item(r, agrs.get(r.party)) for r in subs],
        "to_confirm": [{"name": r.display, "cycle": r.cycle.label, "amount": fmt_yuan(r.amount_fen),
                        "confidence": f"{round(r.confidence * 100)}%"} for r in det.ask],
        "excluded": [{"name": e["display"], "reason": e["reason"]} for e in det.excluded],
        "note": METHOD_NOTE, "reply": reply, **_meta(),
    }
    return ToolResult(data, card)


# ---------- F-SB-03 临期扣费提醒 🟢 ----------

def _upcoming(det: Detection, agrs: dict, within: int) -> list[Recurring]:
    return [r for r in det.subscriptions
            if _state(r, agrs.get(r.party)) == "active" and 0 <= _days_left(r.next_due) <= within]


@bank_tool(
    name="sub.upcoming", domain="subscription", base_risk=Risk.GREEN,
    description="查未来几天内要扣费的订阅（续费提醒）：扣费日、金额、涨价提示，以及借记卡余额够不够扣，"
                "如「最近有哪些订阅要扣费」「这周要续费的有哪些」。",
    capability="查最近要扣费的订阅（续费提醒）", examples=("最近有哪些订阅要扣费",),
    parameters={"type": "object", "properties": {
        "within_days": {"type": "integer", "description": "未来几天，1–31，默认 7；用户说「这周」填 7，「这个月」填 31"},
    }},
)
def upcoming(ctx: ToolContext, within_days: int | str | None = None) -> ToolResult:
    try:
        within = min(31, max(1, int(within_days))) if within_days not in (None, "") else DEFAULT_WINDOW_DAYS
    except (TypeError, ValueError):
        within = DEFAULT_WINDOW_DAYS
    det = detection(ctx.db, ctx.user_id)
    agrs = _agreements(ctx.db, ctx.user_id)
    due = _upcoming(det, agrs, within)
    total = sum(r.amount_fen for r in due)
    balance = ctx.db.scalar(select(Account.balance_fen).where(Account.user_id == ctx.user_id, Account.kind == "debit")) or 0
    short = total > balance
    if not due:
        reply = f"未来 {within} 天没有订阅要扣费。"
    else:
        first = due[0]
        reply = (f"未来 {within} 天有 {len(due)} 笔订阅扣费，合计 {fmt_yuan(total)} 元；"
                 f"最近一笔是{_md(first.next_due)}{_name(first)} {fmt_yuan(first.amount_fen)} 元。")
        reply += "借记卡余额可能不够扣，记得留足余额。" if short else ""
        if ups := [r for r in due if r.price_up_from_fen]:
            reply += f"其中{_name(ups[0])}比上期涨价了。"
    data = {"within_days": within, "count": len(due), "total_yuan": fmt_yuan(total), "total_fen": total,
            "balance_enough": not short,
            "items": [_view(r, agrs.get(r.party)) | {"remind": _days_left(r.next_due) <= max(r.cycle.lead_days)}
                      for r in due],
            "summary": reply, "note": MODEL_NOTE}
    card = {"type": "sub_upcoming", "title": f"未来 {within} 天的订阅扣费", "within_days": within, "count": len(due),
            "total": fmt_yuan(total), "balance_enough": not short,
            "items": [_card_item(r, agrs.get(r.party)) | {"date": _md(r.next_due)} for r in due],
            "reply": reply, **_meta()}
    return ToolResult(data, card)


def reminders_due(db: Session, user_id: int) -> list[dict]:
    """主动提醒（给定时任务 / 时间快进用）：今天正好到提前量的订阅。月付提前 3 天，年付提前 14 天和 3 天各一次。"""
    det = recurring.load_detection(db, user_id, now())
    agrs = _agreements(db, user_id)
    out = []
    for r in det.subscriptions:
        days = _days_left(r.next_due)
        if _state(r, agrs.get(r.party)) == "active" and days in r.cycle.lead_days:
            text = f"{_name(r)}将在{_md(r.next_due)}扣费 {fmt_yuan(r.amount_fen)} 元"
            if r.price_up_from_fen:
                text += f"（比上期的 {fmt_yuan(r.price_up_from_fen)} 元涨价了）"
            out.append({"merchant": r.party, "due": f"{r.next_due:%Y-%m-%d}", "days_left": days,
                        "amount_fen": r.amount_fen, "text": text + "。不想续费可以说「取消" + _name(r) + "」。"})
    return out


# ---------- F-SB-04 取消 ----------

def _aliases(name: str) -> tuple[str, ...]:
    return (name.lower(), *MERCHANTS.get(name, ()))


def match_merchants(hint: str | None, names: list[str]) -> list[str]:
    """按用户原话找订阅：规范名或别名出现在话里 → 大类词（「音乐会员」→ 名字里带「音乐」的）。没找到返回空。"""
    h = quarantine.normalize(hint).lower()
    if not h:
        return []
    hits = [n for n in names if any(len(w) >= 2 and w in h for w in _aliases(n))]
    if hits:
        return hits
    for word, part in _GENERIC.items():
        if word in h:
            return [n for n in names if part.lower() in n.lower()]
    return []


def _set_question(ctx: ToolContext, question: dict | None) -> None:
    state = dict(ctx.session.state or {})
    if question:
        state["pending_question"] = question
    else:
        state.pop("pending_question", None)
    ctx.session.state = state  # 整体赋值，JSON 列才会被识别为已修改


def _guide_card(name: str, case: str, guide, bank_part: str) -> dict:
    card = {"type": "sub_guide", "title": "商户侧取消指引", "merchant": name, "case": case, "bank_part": bank_part,
            "warning": "本行只能停止代扣授权（阻止付款方式）；商户那边的订阅要你自己取消，取消前仍可能续费。"}
    if guide:
        card |= guide.as_dict()
    else:
        card |= {"where": "商户客服", "steps": ["暂未收录这家商户的取消路径，请联系商户客服取消自动续费，或转人工协助"],
                 "source": "", "tips": []}
    return card


@bank_tool(
    name="sub.cancel_propose", domain="subscription", base_risk=Risk.GREEN,
    description="取消订阅 / 停止代扣，如「帮我取消爱奇艺会员」「停掉网易云的自动扣费」。系统按情况处理："
                "本行有代扣或免密协议的，生成「停止本行代扣授权」确认卡；商户侧还有订阅（会员自动续费）的，同时给出商户侧取消指引；"
                "本行没有协议的只给指引。停止本行代扣只是阻止付款方式，商户侧订阅可能仍然有效，绝不能说成「已解约」「已取消订阅」。",
    capability="取消订阅 / 停止代扣", examples=("帮我取消爱奇艺会员",),
    parameters={"type": "object", "properties": {
        "merchant": {"type": "string", "description": "要取消的订阅，照抄用户原话里的商户名，如「爱奇艺」「网易云」；用户没说就不填"},
    }},
)
def propose_cancel(ctx: ToolContext, merchant: str | None = None) -> ToolResult:
    det = detection(ctx.db, ctx.user_id)
    agrs = _agreements(ctx.db, ctx.user_id)
    subs = {r.party: r for r in det.subscriptions if r.registered}
    names = list(subs) + [m for m, a in agrs.items() if m not in subs and a.status == "active"]
    found = match_merchants(merchant, names)
    if len(found) != 1:
        options = found or names
        if not options:
            msg = "没有在你的流水里识别到订阅或代扣。"
            return ToolResult({"status": "not_found", "message": msg}, notice_card(msg))
        unmatched = bool(merchant) and not found and bool(match_merchants(merchant, list(MERCHANTS)))
        _set_question(ctx, {"kind": "sub", "op": "cancel", "names": options})
        ask = ("没有找到这家商户的订阅扣费。" if unmatched else "") + "要取消哪一个？" + "、".join(options) + "。"
        return ToolResult({"status": "need_merchant", "candidates": options, "ask": ask,
                           "note": "直接问用户要取消哪一个，不要替用户选。"})
    _set_question(ctx, None)
    name = found[0]
    r, agr = subs.get(name), agrs.get(name)
    merchant_side = MERCHANT_SIDE.get(name, True)  # 没收录的商户按「商户侧可能有订阅」处理，宁可多提醒
    guide = cancel_guide(name) if merchant_side else None
    billing_active = agr is not None and agr.status == "active"
    case = ("both" if merchant_side else "bank_only") if billing_active else "merchant_only"
    data = {"merchant": name, "case": case, "merchant_side_subscription": merchant_side, "note": MODEL_NOTE}
    if r:
        data |= {"amount_yuan": fmt_yuan(r.amount_fen), "cycle": r.cycle.label}
    if guide:
        data["merchant_guide"] = guide.as_dict()

    if not billing_active:
        if agr is not None:
            bank_part = f"本行代扣授权已于 {agr.stopped_at:%Y-%m-%d} 停止，商户不能再从你的本行账户扣款"
        else:
            bank_part = "本行没有这笔扣费的代扣协议（可能是经第三方支付平台扣款），本行无法直接停止"
        if not merchant_side:
            msg = f"{name}{bank_part}。" if agr else f"{name}的扣费不是经本行协议扣的，请联系商户处理。"
            return ToolResult({**data, "status": "no_action", "message": msg}, notice_card(msg))
        reply = f"{bank_part}。{name}的订阅需要你到商户那边自己取消，步骤见卡片。"
        return ToolResult({**data, "status": "merchant_only", "bank_part": bank_part, "reply": reply},
                          _guide_card(name, case, guide, bank_part) | {"reply": reply})

    rows = [["商户", name], ["扣费方式", f"{AGREEMENT_KIND[agr.kind]}（协议尾号 {agr.agreement_no[-4:]}）"]]
    if r:
        rows.append(["扣费", f"{r.cycle.label} ¥{fmt_yuan(r.amount_fen)}，最近一期 {_md(r.last_at.date())}"])
    rows += [["本次办理", "停止本行代扣授权"], ["停止后", "该商户不能再从你的本行账户扣款"]]
    if merchant_side:
        where = guide.where if guide else "商户"
        rows.append(["商户侧订阅", f"不受影响，可能仍然有效，需到{where}自行取消自动续费"])
        reply = (f"已为你生成停止{name}本行代扣授权的确认卡。注意：这只是阻止付款方式，商户订阅可能仍然有效，"
                 f"还要按指引到{where}取消自动续费。")
        ctx.cards.append(_guide_card(name, case, guide, "可以停止（见确认卡）"))  # 指引卡排在确认卡前面
    else:
        rows.append(["提醒", AFTER_STOP.get(name, "停止后需要自己按时缴费")])
        reply = f"已为你生成停止{name}本行代扣的确认卡，确认后立即生效。{AFTER_STOP.get(name, '')}。".replace("。。", "。")
    display = action_display(title="停止代扣确认", verb="停止本行代扣", summary=f"停止{name}的本行代扣授权", rows=rows,
                             fields={"op": "stop_billing", "case": case, "merchant": name})
    args = {"agreement_id": agr.id, "merchant": name, "agreement_no": agr.agreement_no, "merchant_side": merchant_side}
    return propose_action(ctx, tool="sub.cancel_billing", args=args, display=display, data={**data, "reply": reply})


# ---------- 写工具：只能由网关凭执行令牌调用 ----------

def _billing_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    """执行前用同一个构造器重新校验：协议还在、属于本人、仍然有效。"""
    a = db.get(DebitAgreement, args["agreement_id"])
    if a is None or a.user_id != env.user_id or a.merchant != args["merchant"] or a.agreement_no != args["agreement_no"]:
        problem = "代扣协议不存在"
    elif a.status != "active":
        problem = "该代扣协议已经停止"
    else:
        problem = None
    return base_context(db, env, tool="sub.cancel_billing", block_reason=problem, facts={"op": "stop_billing"})


@bank_tool(
    name="sub.cancel_billing", domain="subscription", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    stop_loss=True, policy_context=_billing_context,
    description="停止本行代扣 / 免密支付授权。只能由工具网关凭执行令牌调用。",
)
def stop_billing(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    a = db.get(DebitAgreement, args["agreement_id"])
    if a is None or a.user_id != user_id:
        raise LedgerError("代扣协议校验失败")
    if a.status != "stopped":  # 已经停止：幂等返回
        a.status, a.stopped_at = "stopped", now()
    # 确认卡的行（含「商户侧订阅」「提醒」）会原样带进回执，这里只补结果和时间
    result = f"本行代扣授权已停止；{ONLY_BILLING}" if args.get("merchant_side", True) else "本行代扣授权已停止，商户不能再从本行账户扣款"
    return {"status": "succeeded", "merchant": a.merchant,
            "receipt_rows": [["结果", result], ["生效时间", f"{a.stopped_at:%Y-%m-%d %H:%M:%S}"]]}


# ---------- 规则兜底（模型不可用时）----------

_DAYS = re.compile(r"(\d{1,2})\s*天")
_SPANS = [(re.compile(r"今天|明天"), 1), (re.compile(r"两周|半个月"), 14), (re.compile(r"这个?月|本月|一个月|30天"), 31),
          (re.compile(r"这周|本周|一周|下周|七天"), 7), (re.compile(r"三天|这几天"), 3)]


def _window_from(text: str) -> int | None:
    if m := _DAYS.search(text):
        return int(m.group(1))
    return next((n for pat, n in _SPANS if pat.search(text)), None)


def rule_turn(ctx: ToolContext, intent, text: str) -> str | None:
    """订阅意图，以及「要取消哪一个」追问的回答。不是订阅相关的话返回 None。"""
    pq = (ctx.session.state or {}).get("pending_question") or {}
    if intent.intent == "sub":
        op = intent.slots["op"]
        r = (overview(ctx) if op == "list" else upcoming(ctx, _window_from(text)) if op == "upcoming"
             else propose_cancel(ctx, text))
        return _rule_text(ctx, r)
    if intent.intent != "unknown" or pq.get("kind") != "sub" or len(match_merchants(text, pq.get("names", []))) != 1:
        return None
    return _rule_text(ctx, propose_cancel(ctx, text))


def _rule_text(ctx: ToolContext, r: ToolResult) -> str:
    if r.card:
        ctx.cards.append(r.card)
    status = r.data.get("status")
    if status == "need_merchant":
        return r.data["ask"]
    if status == "pending_confirmation":
        return r.data["reply"].replace("已为你生成", "请核对") if r.data.get("reply") else "请核对确认卡，无误后点击确认。"
    if r.card and r.card["type"] == "notice":
        return r.card["text"]
    return r.data.get("reply") or r.data.get("summary") or r.data.get("message") or "好的。"
