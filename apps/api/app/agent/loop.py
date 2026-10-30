"""Agent 主循环（M0）。

模型只负责理解和提议：它能调用的只有只读工具和 transfer.propose。确认、认证、执行都不经过模型。
模型不可用（没配密钥、超时、报错）时，自动降级到规则兜底，M0 的两条链路照样能跑通。
"""

import json
import secrets
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..cards import notice_card
from ..clock import now
from ..config import get_settings
from ..models import ChatMessage, ChatSession, User
from ..nlu import amounts_in, rule_intent
from ..security import breaker, inspect, mask_pii
from ..tools import ToolContext, ToolResult, by_llm_name, llm_tool_specs
from ..tools.bank import get_balance
from ..tools.bill import rule_turn as bill_rule_turn
from ..tools.card import rule_turn as card_rule_turn
from ..tools.gateway import GatewayError, ToolViolation, run_read_tool
from ..tools.transfer import aa_status, propose, propose_aa
from .grounding import check, collect_facts
from .llm import LLMError, get_llm
from .prompts import NO_BYPASS, help_text, system_prompt

MAX_TOOL_STEPS = 4
HISTORY_MESSAGES = 8
_BYPASS_FLAGS = {"bypass_auth", "authority_claim", "ignore_instructions", "role_override"}


@dataclass
class TurnResult:
    session_id: str
    trace_id: str
    text: str
    source: str  # llm / rule / template
    cards: list[dict] = field(default_factory=list)

    @property
    def ai_generated(self) -> bool:
        return self.source == "llm"


def _session(db: Session, user_id: int, session_id: str | None, channel: str) -> ChatSession:
    s = db.get(ChatSession, session_id) if session_id else None
    if s is None or s.user_id != user_id:
        s = ChatSession(id="S" + secrets.token_hex(8), user_id=user_id, channel=channel, state={})
        db.add(s)
        db.flush()
    return s


def _recent_user_texts(db: Session, session_id: str, limit: int = 3) -> list[str]:
    rows = db.scalars(select(ChatMessage.content).where(ChatMessage.session_id == session_id, ChatMessage.role == "user")
                      .order_by(ChatMessage.id.desc()).limit(limit)).all()
    return list(rows)


def handle_turn(db: Session, *, user_id: int, session_id: str | None, text: str,
                selection: dict | None = None, channel: str = "app") -> TurnResult:
    session = _session(db, user_id, session_id, channel)
    trace_id = secrets.token_hex(8)
    guard = inspect(text or "")
    audit.record(db, trace_id, "user_message", {"text": mask_pii(guard.text), "flags": guard.flags,
                                                "selection": selection, "channel": channel}, session.id)
    if guard.suspicious:
        audit.record(db, trace_id, "injection_suspected", {"flags": guard.flags}, session.id)
        breaker.on_injection(db, user_id=user_id, session_id=session.id, trace_id=trace_id, flags=guard.flags)
    if guard.text:
        db.add(ChatMessage(session_id=session.id, role="user", content=guard.text, trace_id=trace_id))
        db.flush()
    ctx = ToolContext(db=db, user_id=user_id, session=session, trace_id=trace_id,
                      user_texts=_recent_user_texts(db, session.id), channel=channel, injection_flag=guard.suspicious)

    if selection:
        text_out, source = _selection_turn(ctx, selection), "template"
    elif get_settings().llm_ready:
        try:
            out = _llm_turn(ctx, guard.text)
        except LLMError as e:
            audit.record(db, trace_id, "llm_error", {"error": str(e)[:300]}, session.id)
            out = (_template_reply(ctx.cards), "template") if ctx.cards else None
        text_out, source = out or _rule_turn(ctx, guard)
    else:
        text_out, source = _rule_turn(ctx, guard)

    db.add(ChatMessage(session_id=session.id, role="assistant", content=text_out, trace_id=trace_id))
    audit.record(db, trace_id, "assistant_reply", {"source": source, "text": text_out,
                                                   "cards": [c["type"] for c in ctx.cards]}, session.id)
    db.commit()
    return TurnResult(session_id=session.id, trace_id=trace_id, text=text_out, source=source, cards=ctx.cards)


# ---------- 模型路径 ----------

def _mask_strings(value):
    """只脱敏字符串叶子，保留金额等数字的值和 JSON 类型。"""
    if isinstance(value, dict):
        return {k: _mask_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_strings(v) for v in value]
    return mask_pii(value) if isinstance(value, str) else value


def _mask_json_text(raw: str) -> str:
    try:
        return json.dumps(_mask_strings(json.loads(raw)), ensure_ascii=False)
    except (ValueError, TypeError):
        return mask_pii(raw)  # 模型可能给出非法参数 JSON，保留原有工具错误处理


def _masked_exchange(messages: list[dict]) -> list[dict]:
    masked = _mask_strings(messages)
    for original, message in zip(messages, masked):
        # 协议里的这两个字段也是 JSON 文本，须解析后脱敏，不能当普通字符串处理。
        if original["role"] == "tool":
            message["content"] = _mask_json_text(original["content"])
        for call, masked_call in zip(original.get("tool_calls", []), message.get("tool_calls", [])):
            masked_call["function"]["arguments"] = _mask_json_text(call["function"]["arguments"])
    return masked


def _history(db: Session, session_id: str) -> list[dict]:
    rows = db.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id,
                                               ChatMessage.role.in_(("user", "assistant")))
                      .order_by(ChatMessage.id.desc()).limit(HISTORY_MESSAGES + 1)).all()
    rows = list(reversed(rows[1:]))  # rows[0] 是本轮用户消息
    # 按完整轮次补回真实工具交互，否则历史只剩「已生成卡片」的文字，模型容易照抄而不调用工具。
    exchanges: dict[str, list[dict]] = {}
    traces = [m.trace_id for m in rows if m.role == "assistant" and m.trace_id]
    for m in db.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id,
                                                ChatMessage.role == "tool", ChatMessage.trace_id.in_(traces))
                        .order_by(ChatMessage.id)):
        try:
            exchange = json.loads(m.content)
        except (ValueError, TypeError):
            continue  # 旧版本可能留下损坏的 JSON，不让一条坏记录阻断整个会话
        if not isinstance(exchange, list):
            continue
        exchanges.setdefault(m.trace_id, []).extend(exchange)
    messages = []
    for m in rows:
        if m.role == "assistant":
            messages.extend(exchanges.get(m.trace_id, []))
        messages.append({"role": m.role, "content": mask_pii(m.content)})
    return messages


def _run_tool_call(ctx: ToolContext, tc) -> ToolResult:
    spec = by_llm_name(tc.function.name)
    try:
        if spec is None:
            raise GatewayError(f"未知工具 {tc.function.name}")
        args = json.loads(tc.function.arguments or "{}")
        if not isinstance(args, dict):
            raise GatewayError("参数必须是对象")
        result = run_read_tool(spec, ctx, args)
    except (GatewayError, json.JSONDecodeError, TypeError) as e:
        result = ToolResult({"status": "error", "message": str(e)})
        if spec is None or isinstance(e, ToolViolation):  # 越权调用：同一会话反复出现就降为只读
            breaker.on_tool_violation(ctx.db, user_id=ctx.user_id, session_id=ctx.session.id, trace_id=ctx.trace_id,
                                      tool=spec.name if spec else tc.function.name, error=str(e))
    audit.record(ctx.db, ctx.trace_id, "tool_call", {
        "tool": spec.name if spec else tc.function.name, "args": mask_pii(tc.function.arguments or "")[:500],
        "status": result.data.get("status", "ok"), "card": result.card["type"] if result.card else None,
    }, ctx.session.id)
    return result


def _llm_turn(ctx: ToolContext, text: str) -> tuple[str, str] | None:
    """返回 None 表示这一轮模型不可信，交给规则引擎。"""
    db, llm = ctx.db, get_llm()
    user = db.get(User, ctx.user_id)
    messages = [{"role": "system", "content": system_prompt(user.name, now())}, *_history(db, ctx.session.id),
                {"role": "user", "content": mask_pii(text)}]  # 手机号、卡号不出域：工具再从用户原话里还原
    tools = llm_tool_specs()
    datas: list[dict] = []
    final = ""
    for step in range(MAX_TOOL_STEPS + 1):
        db.commit()  # 调模型前结束事务，不占着数据库写锁
        reply = llm.chat(messages, tools=tools, tool_choice="auto" if step < MAX_TOOL_STEPS else "none")
        audit.record(db, ctx.trace_id, "llm_call", {"step": step, "model": llm.settings.llm_model, "usage": reply.usage,
                                                    "tool_calls": [tc.function.name for tc in reply.tool_calls]},
                     ctx.session.id)
        if not reply.tool_calls:
            final = reply.content.strip()
            break
        assistant = {"role": "assistant", "content": reply.content,
                     "tool_calls": [tc.model_dump(exclude_none=True) for tc in reply.tool_calls]}
        if reply.reasoning:  # GLM 思考模式：交错思考 + 工具调用时必须把思考内容随工具结果一起带回
            assistant["reasoning_content"] = reply.reasoning
        start = len(messages)
        messages.append(assistant)
        for tc in reply.tool_calls:
            result = _run_tool_call(ctx, tc)
            if result.card:
                ctx.cards.append(result.card)
            datas.append(result.data)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result.data, ensure_ascii=False)})
        # 一组调用及结果整体保存，不影响用户/助手文字的历史窗口；完整账号、卡号、手机号不出域。
        messages[start:] = _masked_exchange(messages[start:])
        db.add(ChatMessage(session_id=ctx.session.id, role="tool", trace_id=ctx.trace_id,
                           content=json.dumps(messages[start:], ensure_ascii=False)))

    if not final:
        return _template_reply(ctx.cards), "template"
    # 用户本轮话里的金额也算有据（复述「转 3000 元」不是编造），其余数字只能来自工具结果
    said = collect_facts([{"amount_fen": f} for f in amounts_in(text)])
    facts = collect_facts(datas) | collect_facts(ctx.cards) | _context_facts() | said
    g = check(final, facts, executed=False, has_card=bool(ctx.cards))  # M0 里对话轮次本身从不执行转账
    if g.ok:
        return final, "llm"
    audit.record(db, ctx.trace_id, "grounding_failed", {"text": final, "problem": g.describe()}, ctx.session.id)
    if g.phantom_card:
        return None  # 模型说出了卡片却没调用工具，重写文字也变不出卡片：这一轮由规则引擎确定性地出卡
    messages += [{"role": "assistant", "content": final},
                 {"role": "user", "content": f"[系统校验] 上一条回复有问题：{g.describe()}。请只使用工具结果里的数字重写，并且不要声称已经完成转账。"}]
    db.commit()
    retry = llm.chat(messages, tools=tools, tool_choice="none").content.strip()
    g2 = check(retry, facts, executed=False, has_card=bool(ctx.cards))
    audit.record(db, ctx.trace_id, "grounding_retry", {"text": retry, "ok": g2.ok, "problem": g2.describe()}, ctx.session.id)
    if retry and g2.ok:
        return retry, "llm"
    return _template_reply(ctx.cards), "template"  # 两次都不过：丢弃模型文字，只返回模板


def _context_facts() -> set[str]:
    t = now()
    return collect_facts([t.year, t.month, t.day, get_settings().action_ttl_s // 60, get_settings().otp_window_s // 60])


def _template_reply(cards: list[dict]) -> str:
    if not cards:
        return help_text()
    c = cards[-1]
    match c["type"]:
        case "balance":
            return f"你名下账户余额合计 {c['total']} 元。"
        case "confirm":
            extra = "这笔需要完成人脸和短信验证。" if c["level"] == "RED" else ""
            return f"请核对确认卡，无误后点击确认。{extra}"
        case "clarify":
            return c["question"]
        case "notice":
            return c["text"]
    return c.get("reply") or "好的。"  # 展示类卡片自带按模板渲染的一句话


# ---------- 规则兜底路径 ----------

def _rule_turn(ctx: ToolContext, guard) -> tuple[str, str]:
    text = guard.text
    prefix = NO_BYPASS if _BYPASS_FLAGS & set(guard.flags) else ""
    pq = (ctx.session.state or {}).get("pending_question")
    if pq and pq.get("kind") == "amount" and (found := amounts_in(text)) and len(found) == 1 and len(pq["contact_ids"]) == 1:
        result = propose(ctx, contact_id=pq["contact_ids"][0], amount_fen=found.pop(), memo=pq.get("memo"))
        return prefix + _apply(ctx, result), "rule"
    if pq and pq.get("kind") == "amount" and pq.get("phone") and (found := amounts_in(text)) and len(found) == 1:
        result = propose(ctx, payee_hint=pq["phone"], amount_fen=found.pop(), memo=pq.get("memo"))  # 按手机号转账补金额
        return prefix + _apply(ctx, result), "rule"

    intent = rule_intent(text)
    audit.record(ctx.db, ctx.trace_id, "intent", {"engine": "rule", "intent": intent.intent, "slots": intent.slots},
                 ctx.session.id)
    if intent.intent == "balance":
        return prefix + _apply(ctx, get_balance(ctx)), "rule"
    if intent.intent == "transfer":
        s = intent.slots
        result = propose(ctx, payee_hint=s["payee_hint"], amount_text=s["amount_text"], memo=s.get("memo"))
        if result.data.get("status") == "need_amount":
            return prefix + "要转多少钱？", "rule"
        return prefix + _apply(ctx, result), "rule"
    if intent.intent == "aa":
        return prefix + _apply(ctx, propose_aa(ctx, **intent.slots)), "rule"
    if intent.intent == "aa_status":
        result = aa_status(ctx)
        _apply(ctx, result)
        return prefix + result.data["summary"], "rule"
    if intent.intent.startswith("bill_"):
        return prefix + _apply(ctx, bill_rule_turn(ctx, intent.intent, text)), "rule"
    if (reply := card_rule_turn(ctx, intent, text)) is not None:  # 卡片意图，以及「锁哪张卡」这类追问的回答
        return prefix + reply, "rule"
    return prefix + help_text(), "rule"


def _apply(ctx: ToolContext, result: ToolResult) -> str:
    if result.card:
        ctx.cards.append(result.card)
    return _template_reply(ctx.cards)


def _selection_turn(ctx: ToolContext, selection: dict) -> str:
    pq = (ctx.session.state or {}).get("pending_question") or {}
    if selection.get("type") == "contact" and pq.get("kind") == "payee" and selection.get("contact_id") in pq.get("contact_ids", []):
        return _apply(ctx, propose(ctx, contact_id=selection["contact_id"], amount_fen=pq["amount_fen"], memo=pq.get("memo")))
    ctx.cards.append(notice_card("这个选项已经失效，请重新说一下要做什么。", "warning"))
    return _template_reply(ctx.cards)
