"""卡片管理（WS3）：查卡 🟢、锁卡 🟡、解锁 / 解除挂失 🔴、挂失 🔴、交易限制（收紧 🟡 / 放宽 🔴）、限额虚拟卡 🟡。

设计要点是不对称：收紧保护的操作门槛低，放松保护的操作门槛高。锁卡、挂失是止损操作（stop_loss），收紧交易限制也算（stop_loss_if），熔断开着也能办。
收紧还是放宽由系统比对卡片当前设置算出（规则 card_loosen），模型只传「改哪一项、改成什么」。
给模型、前端和审计的卡片信息一律只露尾号。
"""

import re
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.actions import PolicyEnv, base_context, propose_action
from ..cards import SOURCE_CORE, action_display, notice_card
from ..clock import now
from ..core_mock.ledger import LedgerError
from ..models import Card
from ..money import fmt_yuan, parse_amount
from ..nlu import amount_from_user, card_amount_text
from ..nlu import card_hint as hint_in
from ..policy import PolicyContext, Risk
from ..security import mask_pii
from . import ToolContext, ToolResult, bank_tool

KIND_LABEL = {"debit": "借记卡", "credit": "信用卡", "virtual": "虚拟卡"}
STATUS_LABEL = {"active": "正常", "locked": "已临时锁定", "lost": "已挂失"}
SWITCHES = {"overseas": "境外交易", "online": "无卡支付（线上）", "atm": "ATM 取现"}
AMOUNTS = {"per_txn": "单笔限额", "daily": "单日限额"}
SETTING_LABEL = SWITCHES | AMOUNTS
LIMIT_KEY = {"overseas": "overseas", "online": "online", "atm": "atm", "per_txn": "per_txn_fen", "daily": "daily_fen"}
MAX_LIMIT_FEN = {"debit": 5_000_000, "credit": 5_000_000, "virtual": 500_000}  # 限额上限：实体卡 5 万元，虚拟卡 5000 元

# 每种操作：写工具、动作名、哪些状态 / 卡种可以办
OPS = {
    "lock": {"tool": "card.lock", "verb": "锁卡", "ask": "要锁哪张卡？", "status": ("active",),
             "kinds": ("debit", "credit", "virtual")},
    "unlock": {"tool": "card.unlock", "verb": "解锁", "ask": "要解锁哪张卡？", "status": ("locked", "lost"),
               "kinds": ("debit", "credit", "virtual")},
    "report_loss": {"tool": "card.report_loss", "verb": "挂失", "ask": "要挂失哪张卡？", "status": ("active", "locked"),
                    "kinds": ("debit", "credit")},
    "limit": {"tool": "card.limit_set", "verb": "修改交易限制", "ask": "要修改哪张卡的交易限制？", "status": ("active", "locked"),
              "kinds": ("debit", "credit", "virtual")},
}
OP_OF_TOOL = {v["tool"]: k for k, v in OPS.items()} | {"card.virtual_create": "virtual_apply"}

LOST_TIP = "别着急，建议先临时锁卡止损：立即生效，找到了可以随时解锁；确定找不到了再办理挂失。"

_CARD_HINT_PARAM = {"type": "string", "description": "哪张卡，照抄用户原话，如「借记卡」「信用卡」「尾号6688」；用户没说就不填"}
_KIND_WORDS = {"debit": ("借记卡", "储蓄卡", "工资卡", "一类卡", "Ⅰ类卡"), "credit": ("信用卡",), "virtual": ("虚拟卡",)}
_TAIL = re.compile(r"(?<!\d)\d{4}(?!\d)")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


# ---------- 查找与展示 ----------

def _user_cards(db: Session, user_id: int) -> list[Card]:
    return list(db.scalars(select(Card).where(Card.user_id == user_id).order_by(Card.id)))


def resolve_cards(cards: list[Card], hint: str | None) -> list[Card]:
    """按「尾号 → 卡种」在用户的卡里查找；没给线索就返回全部，由上层追问，不替用户选。"""
    h = hint or ""
    if tails := _TAIL.findall(h):
        return [c for c in cards if c.tail in tails]
    kinds = {k for k, words in _KIND_WORDS.items() if any(w in h for w in words)}
    return [c for c in cards if c.kind in kinds] if kinds else cards


def _label(c: Card) -> str:
    return f"{c.name}（尾号{c.tail}）"


def _setting_value(setting: str, value) -> str:
    if setting in SWITCHES:
        return "开通" if value else "关闭"
    return f"¥{fmt_yuan(value)}"


def _limit_rows(c: Card) -> list[list[str]]:
    rows = [[label, "已开通" if c.limits.get(k) else "已关闭"] for k, label in SWITCHES.items()
            if not (c.kind == "virtual" and k == "atm")]
    return rows + [[label, _setting_value(k, c.limits[LIMIT_KEY[k]])] for k, label in AMOUNTS.items()]


def _extra_rows(c: Card) -> list[list[str]]:
    if c.kind == "credit":
        return [["信用额度", f"¥{fmt_yuan(c.credit_limit_fen)}"], ["可用额度", f"¥{fmt_yuan(c.available_fen)}"]]
    if c.kind == "virtual":
        return [["限定商户", c.merchant or "不限"], ["使用次数", "一次性" if c.one_time else "可多次使用"],
                ["有效期至", f"{c.expires_on:%Y-%m-%d}"]]
    return []


def _view(c: Card) -> dict:
    """给模型看的卡片信息：只有尾号，没有卡号。"""
    v = {"card": c.name, "kind": KIND_LABEL[c.kind], "tail": c.tail, "card_status": STATUS_LABEL[c.status]}
    v |= {k: val for k, val in _limit_rows(c)} | {k: val for k, val in _extra_rows(c)}
    return v


def card_list_card(cards: list[Card], *, hint: str | None = None) -> dict:
    return {
        "type": "card_list",
        "hint": hint,
        "cards": [{"name": c.name, "kind": c.kind, "kind_label": KIND_LABEL[c.kind], "tail": c.tail, "status": c.status,
                   "status_label": STATUS_LABEL[c.status], "rows": _limit_rows(c) + _extra_rows(c)} for c in cards],
        "as_of": now().strftime("%Y-%m-%d %H:%M"),
        "source": SOURCE_CORE,
    }


def _set_question(ctx: ToolContext, question: dict | None) -> None:
    state = dict(ctx.session.state or {})
    if question:
        state["pending_question"] = question
    else:
        state.pop("pending_question", None)
    ctx.session.state = state  # 整体赋值，JSON 列才会被识别为已修改


def _why_not(op: str, c: Card) -> str:
    if c.status == "lost" and op != "unlock":
        return f"{_label(c)}已挂失，原卡已停用。" + ("" if op == "report_loss" else "如需恢复，可以办理解除挂失。")
    if op == "lock" and c.status == "locked":
        return f"{_label(c)}已经是临时锁定状态，不用再锁。"
    if op == "unlock":
        return f"{_label(c)}状态正常，不需要解锁。"
    if op == "report_loss" and c.kind == "virtual":
        return "虚拟卡不需要挂失，可以直接锁卡。"
    return f"{_label(c)}当前{STATUS_LABEL[c.status]}，不能{OPS[op]['verb']}。"


def _pick(ctx: ToolContext, op: str, card_hint: str | None, *, card_ids: list[int] | None = None,
          params: dict | None = None) -> Card | ToolResult:
    """找出要操作的那张卡。找不到、状态不对时返回提示；有多张候选时展示卡片列表并记下追问，由用户选。"""
    cards = _user_cards(ctx.db, ctx.user_id)
    if card_ids is not None:
        cards = [c for c in cards if c.id in card_ids]
    elif card_hint and not any(hint_in(t) or any(x in t for x in _TAIL.findall(card_hint)) for t in ctx.user_texts):
        card_hint = None  # 用户没说是哪张卡，模型自己填的不算：展示卡片列表让用户选（用户说了「6688」这样的尾号就算说了）
    found = resolve_cards(cards, card_hint)
    if not found:
        msg = f"没有找到「{mask_pii(card_hint or '')}」对应的卡。"
        return ToolResult({"status": "card_not_found", "message": msg}, notice_card(msg, "warning"))
    rule = OPS[op]
    ok = [c for c in found if c.status in rule["status"] and c.kind in rule["kinds"]]
    if not ok:
        msg = _why_not(op, found[0]) if len(found) == 1 else f"你名下没有可以{rule['verb']}的卡。"
        return ToolResult({"status": "not_applicable", "message": msg, "cards": [_view(c) for c in found]},
                          notice_card(msg))
    if len(ok) > 1:
        _set_question(ctx, {"kind": "card", "op": op, "card_ids": [c.id for c in ok], "params": params or {}})
        kinds = {c.kind for c in ok}
        how = ("可以说" + "".join(f"「{KIND_LABEL[k]}」" for k in KIND_LABEL if k in kinds) + "或尾号。"
               if len(kinds) == len(ok) else "请说卡的尾号。")
        return ToolResult({"status": "need_card", "candidates": [_view(c) for c in ok],
                           "ask": rule["ask"] + how,
                           "note": "卡片列表已展示给用户。直接问用户要操作哪张卡，不要替用户选，也不要重复列出卡片。"},
                          card_list_card(ok))
    _set_question(ctx, None)
    return ok[0]


# ---------- 查卡 🟢 ----------

@bank_tool(
    name="card.list", domain="card", base_risk=Risk.GREEN,
    description="查询用户名下的银行卡：卡种、尾号、状态（正常 / 临时锁定 / 已挂失）、境外 / 线上 / ATM 开关、"
                "单笔和单日限额、信用卡额度、虚拟卡。只返回尾号。",
    capability="查卡片状态和限额", examples=("我的卡都是什么状态",),
    parameters={"type": "object", "properties": {"card_hint": _CARD_HINT_PARAM}},
)
def list_cards(ctx: ToolContext, card_hint: str | None = None) -> ToolResult:
    every = _user_cards(ctx.db, ctx.user_id)
    cards = resolve_cards(every, card_hint) or every
    return ToolResult({"cards": [_view(c) for c in cards], "total": len(every)}, card_list_card(cards))


# ---------- 锁卡 🟡 / 解锁 🔴 / 挂失 🔴 ----------

@bank_tool(
    name="card.lock_propose", domain="card", base_risk=Risk.GREEN,
    description="临时锁卡，生成确认卡（用户确认后立即生效，可随时解锁）。用户说卡丢了、找不到了、被偷了、怀疑被盗刷时，"
                "先用它帮用户止损：知道是哪张卡就填 card_hint；不知道就不填，系统会展示卡片列表，你只需问用户锁哪张。"
                "同时告诉用户确定找不到了可以挂失，但不要替用户决定挂失。",
    capability="临时锁卡（卡丢了先锁卡）", examples=("我的卡好像丢了",),
    parameters={"type": "object", "properties": {"card_hint": _CARD_HINT_PARAM}},
)
def propose_lock(ctx: ToolContext, card_hint: str | None = None, *, card_ids: list[int] | None = None,
                 lost: bool = False) -> ToolResult:
    return _propose_status(ctx, "lock", card_hint, card_ids, {"lost": lost})


@bank_tool(
    name="card.unlock_propose", domain="card", base_risk=Risk.GREEN,
    description="解除卡片的临时锁定或挂失，生成确认卡。恢复交易能力属于放松保护，系统会要求人脸 + 短信验证。",
    capability="解锁 / 解除挂失", examples=("把借记卡解锁",),
    parameters={"type": "object", "properties": {"card_hint": _CARD_HINT_PARAM}},
)
def propose_unlock(ctx: ToolContext, card_hint: str | None = None, *, card_ids: list[int] | None = None) -> ToolResult:
    return _propose_status(ctx, "unlock", card_hint, card_ids)


@bank_tool(
    name="card.report_loss_propose", domain="card", base_risk=Risk.GREEN,
    description="正式挂失借记卡或信用卡，生成确认卡（挂失后原卡立即停用，需要人脸 + 短信验证）。"
                "只在用户明确说要挂失时调用；用户只是说卡丢了、找不到了，先用 card_lock_propose 锁卡。",
    capability="挂失", examples=("挂失我的信用卡",),
    parameters={"type": "object", "properties": {"card_hint": _CARD_HINT_PARAM}},
)
def propose_report_loss(ctx: ToolContext, card_hint: str | None = None, *,
                        card_ids: list[int] | None = None) -> ToolResult:
    return _propose_status(ctx, "report_loss", card_hint, card_ids)


def _propose_status(ctx: ToolContext, op: str, card_hint: str | None, card_ids: list[int] | None,
                    params: dict | None = None) -> ToolResult:
    picked = _pick(ctx, op, card_hint, card_ids=card_ids, params=params)
    if isinstance(picked, ToolResult):
        return picked
    c, label = picked, _label(picked)
    if op == "lock":
        display = action_display(title="锁卡确认", verb="锁卡", summary=f"临时锁定{label}", rows=[
            ["卡片", label], ["生效", "确认后立即生效"], ["锁定期间", "刷卡、取现、线上支付都会被拒绝"],
            ["恢复", "可随时解锁（需人脸 + 短信验证）"], ["确定丢了", "可以说「挂失」办理正式挂失"]])
        tip = "锁卡确认后立即生效，找到卡后可以随时解锁。如果用户确定卡丢了找不回来，可以提醒还能办理正式挂失。"
    elif op == "unlock":
        verb = "解除挂失" if c.status == "lost" else "解锁"
        display = action_display(title=f"{verb}确认", verb=verb, summary=f"{verb}{label}", rows=[
            ["卡片", label], ["当前状态", STATUS_LABEL[c.status]], ["解除后", "恢复刷卡、取现和线上支付"]])
        tip = "恢复交易能力需要人脸 + 短信验证，验证通过之前卡片仍然不能使用。"
    else:
        display = action_display(title="挂失确认", verb="挂失", summary=f"挂失{label}", rows=[
            ["卡片", label], ["挂失类型", "正式挂失"], ["挂失后", "原卡立即停用，所有交易都会被拒绝"],
            ["补卡", "可到网点申请补卡（演示环境不办理）"]])
        tip = "挂失需要人脸 + 短信验证。如果用户只是暂时找不到卡，也可以先临时锁卡，找到后自己解锁。"
    display["fields"] = {"op": op, "card_tail": c.tail}
    return propose_action(ctx, tool=OPS[op]["tool"], args={"card_id": c.id, "from_status": c.status},
                          display=display, data={**_view(c), "tip": tip})


# ---------- 交易限制：收紧 🟡 / 放宽 🔴 ----------

def _direction(setting: str, before, after) -> str:
    """开关打开、限额调高 = 放宽保护；反之收紧。由系统算，不由模型说。"""
    if before == after:
        return "same"
    if setting in SWITCHES:
        return "loosen" if after else "tighten"
    return "loosen" if after > before else "tighten"


@bank_tool(
    name="card.limit_propose", domain="card", base_risk=Risk.GREEN,
    description="修改卡片交易限制：开关境外交易、无卡（线上）支付、ATM 取现，或调整单笔 / 单日限额，生成确认卡。"
                "一次只改一项，多项就分别调用。是收紧还是放宽、要不要强验证由系统判断。不处理信用额度调整。",
    capability="调整卡片交易限制（境外 / 无卡支付 / ATM / 单笔和单日限额）", examples=("关闭信用卡的境外交易",),
    parameters={"type": "object", "properties": {
        "setting": {"type": "string", "enum": list(SETTING_LABEL),
                    "description": "overseas 境外交易；online 无卡 / 线上支付；atm ATM 取现；per_txn 单笔限额；daily 单日限额"},
        "enabled": {"type": "boolean", "description": "开关类（overseas / online / atm）必填：开启 true，关闭 false"},
        "amount_text": {"type": "string", "description": "限额类（per_txn / daily）必填：金额照抄用户原话，如「2000」「五千」，不要换算"},
        "card_hint": _CARD_HINT_PARAM,
    }, "required": ["setting"]},
)
def propose_limit(ctx: ToolContext, setting: str, enabled: bool | None = None, amount_text: str | None = None,
                  card_hint: str | None = None, *, card_ids: list[int] | None = None) -> ToolResult:
    if setting not in SETTING_LABEL:
        return ToolResult({"status": "error", "message": f"不支持的设置项：{setting}"})
    label = SETTING_LABEL[setting]
    if setting in SWITCHES:
        if not isinstance(enabled, bool):
            return ToolResult({"status": "need_value", "ask": f"{label}要开通还是关闭？"})
        value = enabled
    else:
        value = parse_amount(amount_text) if amount_text else None
        if value is None:
            return ToolResult({"status": "need_value", "ask": f"{label}要调到多少？"})
        if not amount_from_user(value, ctx.user_texts):
            return ToolResult({"status": "amount_not_from_user", "message": "金额必须来自用户原话，请向用户确认金额"},
                              notice_card(f"请再说一下{label}要调到多少。", "warning"))
    picked = _pick(ctx, "limit", card_hint, card_ids=card_ids,
                   params={"setting": setting, "enabled": enabled, "amount_text": amount_text})
    if isinstance(picked, ToolResult):
        return picked
    c = picked
    if c.kind == "virtual" and setting == "atm":
        return ToolResult({"status": "not_applicable", "message": "虚拟卡不支持 ATM 取现"}, notice_card("虚拟卡不支持 ATM 取现。"))
    before = c.limits[LIMIT_KEY[setting]]
    direction = _direction(setting, before, value)
    if direction == "same":
        msg = f"{_label(c)}的{label}已经是{_setting_value(setting, value)}，不用修改。"
        return ToolResult({"status": "no_change", "message": msg}, notice_card(msg))
    rows = [["卡片", _label(c)], ["项目", label], ["原设置", _setting_value(setting, before)],
            ["改为", _setting_value(setting, value)], ["调整方向", "收紧保护" if direction == "tighten" else "放宽保护"]]
    if setting == "daily" and value < c.limits["per_txn_fen"]:
        rows.append(["同时", f"单笔限额同步调为 {_setting_value('per_txn', value)}"])
    verb = ("开通" if value else "关闭") if setting in SWITCHES else "调整"
    summary = f"{verb}{_label(c)}的{label}" + ("" if setting in SWITCHES else f"为{fmt_yuan(value)}元")
    display = action_display(title="交易限制修改确认", verb="修改交易限制", summary=summary, rows=rows,
                             fields={"op": "limit", "card_tail": c.tail, "setting": setting, "direction": direction})
    args = {"card_id": c.id, "from_status": c.status, "setting": setting, "before": before, "value": value}
    return propose_action(ctx, tool="card.limit_set", args=args, display=display, data={
        **_view(c), "setting": label, "before": _setting_value(setting, before),
        "after": _setting_value(setting, value), "direction": "收紧保护" if direction == "tighten" else "放宽保护"})


def _limit_problem(c: Card, args: dict) -> str | None:
    setting, value = args["setting"], args["value"]
    label = SETTING_LABEL[setting]
    if c.limits.get(LIMIT_KEY[setting]) != args["before"]:
        return "卡片设置已变化"
    if c.kind == "virtual" and setting == "atm":
        return "虚拟卡不支持 ATM 取现"
    if setting in AMOUNTS:
        if value <= 0:
            return "限额必须大于 0；不想用这张卡可以锁卡"
        if value > MAX_LIMIT_FEN[c.kind]:
            return f"{KIND_LABEL[c.kind]}的{label}最高 {fmt_yuan(MAX_LIMIT_FEN[c.kind])} 元"
        if setting == "per_txn" and value > c.limits["daily_fen"]:
            return f"单笔限额不能高于单日限额（当前 {fmt_yuan(c.limits['daily_fen'])} 元），请先调高单日限额"
    if _direction(setting, args["before"], value) == "same":
        return "设置没有变化"
    return None


# ---------- 限额虚拟卡 🟡 ----------

@bank_tool(
    name="card.virtual_propose", domain="card", base_risk=Risk.GREEN,
    description="申请一张限额虚拟卡（用于网购、订阅试用），从借记卡扣款，只能线上使用，生成确认卡。",
    capability="申请限额虚拟卡", examples=("申请一张500元限额的网购虚拟卡",),
    parameters={"type": "object", "properties": {
        "amount_text": {"type": "string", "description": "虚拟卡限额，照抄用户原话，如「500」「两千」，不要换算；用户没说就不填"},
        "merchant": {"type": "string", "description": "只在某个商户使用时填商户名，照抄用户原话，如「淘宝」；没说就不填"},
        "one_time": {"type": "boolean", "description": "用户说一次性、用一次就作废时填 true"},
    }},
)
def propose_virtual(ctx: ToolContext, amount_text: str | None = None, merchant: str | None = None,
                    one_time: bool = False) -> ToolResult:
    merchant = mask_pii(_CTRL.sub("", (merchant or "").strip()))[:20]
    params = {"merchant": merchant, "one_time": bool(one_time)}
    limit = parse_amount(amount_text) if amount_text else None
    if limit is None:
        _set_question(ctx, {"kind": "card", "op": "virtual_apply", "card_ids": [], "params": params})
        return ToolResult({"status": "need_value", "ask": "虚拟卡的限额要设多少？"})
    _set_question(ctx, None)
    if not amount_from_user(limit, ctx.user_texts):
        return ToolResult({"status": "amount_not_from_user", "message": "金额必须来自用户原话，请向用户确认金额"},
                          notice_card("请再说一下虚拟卡的限额。", "warning"))
    if merchant and not any(merchant in t for t in ctx.user_texts):
        return ToolResult({"status": "merchant_not_from_user", "message": "商户名必须来自用户原话，请向用户确认"},
                          notice_card("请再说一下虚拟卡限定在哪个商户使用。", "warning"))
    funding = next((c for c in _user_cards(ctx.db, ctx.user_id) if c.kind == "debit"), None)
    if funding is None:
        return ToolResult({"status": "not_applicable", "message": "没有可以绑定的借记卡"}, notice_card("没有可以绑定的借记卡。"))
    rows = [["扣款卡", _label(funding)], ["限额", f"¥{fmt_yuan(limit)}（单笔 / 单日）"], ["限定商户", merchant or "不限"],
            ["使用次数", "一次性，用一次后失效" if one_time else "可多次使用"], ["交易范围", "仅线上支付，不支持境外和取现"],
            ["有效期", "1 年"]]
    display = action_display(title="虚拟卡申请确认", verb="申请虚拟卡", rows=rows,
                             summary=f"申请一张限额{fmt_yuan(limit)}元的虚拟卡",
                             fields={"op": "virtual_apply", "limit": fmt_yuan(limit), "merchant": merchant})
    args = {"funding_card_id": funding.id, "limit_fen": limit, "merchant": merchant, "one_time": bool(one_time)}
    return propose_action(ctx, tool="card.virtual_create", args=args, display=display,
                          data={"limit_yuan": fmt_yuan(limit), "merchant": merchant or "不限", "funding_tail": funding.tail})


# ---------- 写工具：只能由网关凭执行令牌调用 ----------

def _card_context(tool: str):
    """每个卡片写工具的策略上下文：执行前用同一个构造器重新校验卡片状态和设置有没有变。"""

    def build(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
        facts = {"op": OP_OF_TOOL[tool]}
        if tool == "card.virtual_create":
            funding = db.get(Card, args["funding_card_id"])
            limit = args["limit_fen"]
            if funding is None or funding.user_id != env.user_id:
                problem = "扣款卡不存在"
            elif funding.status != "active":
                problem = f"{_label(funding)}{STATUS_LABEL[funding.status]}，不能申请虚拟卡"
            elif not 0 < limit <= MAX_LIMIT_FEN["virtual"]:
                problem = f"虚拟卡限额需在 0 到 {fmt_yuan(MAX_LIMIT_FEN['virtual'])} 元之间"
            else:
                problem = None
            return base_context(db, env, tool=tool, block_reason=problem, facts=facts)
        card = db.get(Card, args["card_id"])
        facts["from_status"] = args["from_status"]
        if card is None or card.user_id != env.user_id:
            problem = "卡片不存在"
        elif card.status != args["from_status"]:
            problem = "卡片状态已变化"
        elif tool == "card.limit_set":
            problem = _limit_problem(card, args)
            facts["direction"] = _direction(args["setting"], args["before"], args["value"])
        else:
            problem = None
        return base_context(db, env, tool=tool, block_reason=problem, facts=facts)

    return build


def _load_card(db: Session, args: dict, user_id: int) -> Card:
    card = db.get(Card, args["card_id"])
    if card is None or card.user_id != user_id:
        raise LedgerError("卡片校验失败")
    return card


def _set_status(db: Session, args: dict, user_id: int, status: str) -> dict:
    card = _load_card(db, args, user_id)
    if card.status != status:  # 已经是目标状态：幂等返回
        if card.status != args["from_status"]:
            raise LedgerError("卡片状态已变化")
        card.status = status
    return {"status": "succeeded", "card_tail": card.tail, "card_status": status,
            "receipt_rows": [["卡片状态", STATUS_LABEL[status]], ["生效时间", f"{now():%Y-%m-%d %H:%M:%S}"]]}


@bank_tool(
    name="card.lock", domain="card", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False, stop_loss=True,
    policy_context=_card_context("card.lock"), description="临时锁卡。只能由工具网关凭执行令牌调用。",
)
def lock_card(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    return _set_status(db, args, user_id, "locked")


@bank_tool(
    name="card.unlock", domain="card", base_risk=Risk.RED, side_effect=True, llm_visible=False,
    policy_context=_card_context("card.unlock"), description="解除临时锁定或挂失。只能由工具网关凭执行令牌调用。",
)
def unlock_card(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    return _set_status(db, args, user_id, "active")


@bank_tool(
    name="card.report_loss", domain="card", base_risk=Risk.RED, side_effect=True, llm_visible=False, stop_loss=True,
    policy_context=_card_context("card.report_loss"), description="正式挂失。只能由工具网关凭执行令牌调用。",
)
def report_loss(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    return _set_status(db, args, user_id, "lost")


@bank_tool(
    name="card.limit_set", domain="card", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    # 收紧是止损（熔断时照常可办），放宽不是；before 由系统在提议时读卡片当前设置写入，受摘要保护，执行前还会重新核对
    stop_loss_if=lambda a: _direction(a["setting"], a["before"], a["value"]) == "tighten",
    policy_context=_card_context("card.limit_set"), description="修改卡片交易限制。只能由工具网关凭执行令牌调用。",
)
def set_limit(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    card = _load_card(db, args, user_id)
    setting, value = args["setting"], args["value"]
    key = LIMIT_KEY[setting]
    limits = dict(card.limits)
    if limits[key] not in (args["before"], value):
        raise LedgerError("卡片设置已变化")
    limits[key] = value
    if setting == "daily" and limits["per_txn_fen"] > value:
        limits["per_txn_fen"] = value  # 单笔不能高于单日
    card.limits = limits  # 整体赋值，JSON 列才会被识别为已修改
    return {"status": "succeeded", "card_tail": card.tail,
            "receipt_rows": [[SETTING_LABEL[setting], _setting_value(setting, value)],
                             ["生效时间", f"{now():%Y-%m-%d %H:%M:%S}"]]}


@bank_tool(
    name="card.virtual_create", domain="card", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    policy_context=_card_context("card.virtual_create"), description="开立限额虚拟卡。只能由工具网关凭执行令牌调用。",
)
def create_virtual(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    card = db.scalars(select(Card).where(Card.idempotency_key == idempotency_key)).first()
    if card is None:  # 幂等：同一个动作只开一张卡
        funding = db.get(Card, args["funding_card_id"])
        if funding is None or funding.user_id != user_id or funding.status != "active":
            raise LedgerError("扣款卡校验失败")
        limit, merchant = args["limit_fen"], args["merchant"] or None
        name = "一次性虚拟卡" if args["one_time"] else f"{merchant}专用虚拟卡" if merchant else "网购虚拟卡"
        card = Card(user_id=user_id, kind="virtual", name=name, card_no=f"625965{secrets.randbelow(10**10):010d}",
                    account_id=funding.account_id, merchant=merchant, one_time=args["one_time"],
                    limits={"overseas": False, "online": True, "atm": False, "per_txn_fen": limit, "daily_fen": limit},
                    expires_on=(now() + timedelta(days=365)).date(), idempotency_key=idempotency_key)
        db.add(card)
        db.flush()
    return {"status": "succeeded", "card_tail": card.tail,
            "receipt_rows": [["虚拟卡", f"尾号 {card.tail}"], ["有效期至", f"{card.expires_on:%Y-%m-%d}"],
                             ["卡号与安全码", "请在 App「卡片」页查看（演示环境不展示完整卡号）"]]}


# ---------- 规则兜底（模型不可用时）----------

def rule_turn(ctx: ToolContext, intent, text: str) -> str | None:
    """卡片意图，以及「要操作哪张卡」「限额设多少」这类追问的回答。不是卡片相关的话返回 None。"""
    pq = (ctx.session.state or {}).get("pending_question") or {}
    if intent.intent == "card":
        s = intent.slots
        hint = s.get("card_hint")
        if not hint and s["op"] != "list":  # 省略补全：「我的信用卡被偷了」→「直接挂失吧」
            hint = next((h for t in ctx.user_texts[1:] if (h := hint_in(t))), "")
        result = _run(ctx, s["op"], hint, None, s)
        return _rule_text(ctx, result, lost=bool(s.get("lost")))
    if intent.intent != "unknown" or pq.get("kind") != "card":
        return None
    op, params = pq["op"], pq.get("params") or {}
    if op == "virtual_apply":
        if not (amount := card_amount_text(text)):
            return None
        return _rule_text(ctx, _run(ctx, op, None, None, {**params, "amount_text": amount}))
    candidates = [c for c in _user_cards(ctx.db, ctx.user_id) if c.id in pq["card_ids"]]
    if len(resolve_cards(candidates, text)) == len(candidates):
        return None  # 这句话没说是哪张卡
    return _rule_text(ctx, _run(ctx, op, text, pq["card_ids"], params), lost=bool(params.get("lost")))


def _run(ctx: ToolContext, op: str, card_hint: str | None, card_ids: list[int] | None, p: dict) -> ToolResult:
    match op:
        case "list":
            return list_cards(ctx, card_hint)
        case "lock":
            return propose_lock(ctx, card_hint, card_ids=card_ids, lost=bool(p.get("lost")))
        case "unlock":
            return propose_unlock(ctx, card_hint, card_ids=card_ids)
        case "report_loss":
            return propose_report_loss(ctx, card_hint, card_ids=card_ids)
        case "limit":
            return propose_limit(ctx, p.get("setting"), p.get("enabled"), p.get("amount_text"), card_hint,
                                 card_ids=card_ids)
        case "virtual_apply":
            return propose_virtual(ctx, p.get("amount_text"), p.get("merchant"), p.get("one_time", False))
    raise ValueError(op)


def _rule_text(ctx: ToolContext, r: ToolResult, *, lost: bool = False) -> str:
    if r.card:
        ctx.cards.append(r.card)
    status = r.data.get("status")
    if status in ("need_card", "need_value"):
        return (LOST_TIP if lost else "") + r.data["ask"]
    if status == "pending_confirmation":
        if lost:
            return "别着急，先帮你临时锁卡止损：确认后立即生效，找到了可以随时解锁。确定找不到了，可以说「挂失」办理正式挂失。"
        return "请核对确认卡，无误后点击确认。" + ("这一步需要完成人脸和短信验证。" if r.data["level"] == "RED" else "")
    if r.card and r.card["type"] == "notice":
        return r.card["text"]
    if r.card and r.card["type"] == "card_list":
        n = len(r.card["cards"])
        return f"你名下共有 {n} 张卡，状态和限额见卡片。" if n == r.data["total"] else "查到了，状态和限额见卡片。"
    return r.data.get("message") or "好的。"
