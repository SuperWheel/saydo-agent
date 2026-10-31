"""Agent 主循环（M0）。

模型只负责理解和提议：它能调用的只有只读工具和 transfer.propose。确认、认证、执行都不经过模型。
模型不可用（没配密钥、超时、报错）时，自动降级到规则兜底，M0 的两条链路照样能跑通。
"""

import json
import re
import secrets
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..cards import notice_card
from ..clock import now
from ..config import get_settings
from ..models import ChatMessage, ChatSession, Contact, PendingAction, User
from ..nlu import amounts_in, names_merchant, narrow_payees, parse_amount, resolve_payee, rule_intent
from ..nlu.transfer import phones_in
from ..security import breaker, inspect, mask_pii
from ..tools import REGISTRY, ToolContext, ToolResult, by_llm_name, llm_tool_specs
from ..tools.bank import get_balance
from ..tools.bill import rule_turn as bill_rule_turn, workflow_args as bill_workflow_args
from ..tools.bill_analysis import rule_turn as adhoc_rule_turn
from ..tools.card import rule_turn as card_rule_turn
from ..tools.knowledge import rule_turn as kb_rule_turn
from ..tools.subscription import rule_turn as sub_rule_turn
from ..tools.gateway import GatewayError, ToolViolation, run_read_tool
from ..tools.transfer import aa_status, propose, propose_aa
from ..tools.wealth import rule_turn as wealth_rule_turn
from ..tools.automation import edit_order
from ..tools.wealth import buy_from_card, submit_quiz
from . import planner  # 跨场景（WS7）：多步请求、计划控制语、定时转账
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
    workflow: bool = False  # 这一轮走了确定性工作流（不调模型）
    elapsed_ms: int = 0  # 服务端处理这一轮用的时间，气泡下方显示，和模型路径对比

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
    started = time.perf_counter()
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

    on_workflow = False
    if selection:
        text_out, source = _selection_turn(ctx, selection), "template"
    # 跨场景（WS7）唯一入口：计划的控制语（继续 / 「蛋糕不要了」/ 撤销锁定 / 转人工）、多步请求（进规划器）、
    # 定时转账等必须确定性处理的话。不是这些就返回 None，照常走模型或规则。说明见 agent/planner.py
    elif (planned := planner.route(ctx, guard, get_llm if get_settings().llm_ready else None)) is not None:
        text_out, source = planned
    elif (workflow := _fast_path(ctx, guard)) is not None:  # 固定流程（查余额、给我妈转 500、锁卡……）：规则确定性作答，不用等模型
        text_out, source, on_workflow = *_rule_turn(ctx, guard), True
        _record_workflow(ctx, *workflow)
    elif get_settings().llm_ready:
        try:
            out = _llm_turn(ctx, guard.text)
            _order_cards(ctx.cards, guard.text)
        except LLMError as e:
            audit.record(db, trace_id, "llm_error", {"error": str(e)[:300]}, session.id)
            out = (_template_reply(ctx.cards), "template") if ctx.cards else None
        # 模型这轮作废（报错、虚构卡片）时退回规则：订花、心愿罐这类单步请求的规则在规划器里
        text_out, source = out or planner.route(ctx, guard, None) or _rule_turn(ctx, guard)
    else:
        text_out, source = _rule_turn(ctx, guard)

    _void_blocked_cards(ctx)
    db.add(ChatMessage(session_id=session.id, role="assistant", content=text_out, trace_id=trace_id))
    took = round((time.perf_counter() - started) * 1000)
    audit.record(db, trace_id, "assistant_reply", {"source": source, "text": text_out, "workflow": on_workflow,
                                                   "elapsed_ms": took, "cards": [c["type"] for c in ctx.cards]}, session.id)
    db.commit()
    return TurnResult(session_id=session.id, trace_id=trace_id, text=text_out, source=source, cards=ctx.cards,
                      workflow=on_workflow, elapsed_ms=took)


def _void_blocked_cards(ctx: ToolContext) -> None:
    """这一轮出了熔断提示：此前还没确认、现在会被熔断拦下的确认卡，在画面上一并作废。
    后端本来就会在执行前重新校验并作废它们（`auth/actions._revalidate`），这里只是不让旧卡的按钮还亮着。"""
    trip_card = next((c for c in ctx.cards if c.get("breaker")), None)
    if trip_card is None:
        return
    pending = ctx.db.scalars(select(PendingAction).where(PendingAction.user_id == ctx.user_id,
                                                        PendingAction.status == "pending")).all()
    ids = [a.id for a in pending if breaker.check(ctx.db, user_id=a.user_id, tool=a.tool, session_id=a.session_id,
                                                   args=a.args) is not None]
    if ids:
        trip_card["void_action_ids"] = ids
        trip_card["void_reason"] = "安全保护已开启，这张确认卡作废，没有发生资金变动"


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
    domains = _tool_domains(text, messages)
    tools = llm_tool_specs(domains, extra=ALWAYS_TOOLS)
    datas: list[dict] = []
    final = ""
    for step in range(MAX_TOOL_STEPS + 1):
        db.commit()  # 调模型前结束事务，不占着数据库写锁
        reply = llm.chat(messages, tools=tools, tool_choice="auto" if step < MAX_TOOL_STEPS else "none")
        audit.record(db, ctx.trace_id, "llm_call", {"step": step, "model": llm.settings.llm_model, "usage": reply.usage,
                                                    "tool_calls": [tc.function.name for tc in reply.tool_calls]},
                     ctx.session.id)
        if not reply.tool_calls:
            final = _plain(reply.content)
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
        if any(not c.get("kb_no_hit") for c in ctx.cards):
            # 模型同时查了知识库和别的工具（「有没有保本稳赚的理财」）：已经有别的卡片作答，就不再出「知识库没查到」
            ctx.cards[:] = [c for c in ctx.cards if not c.get("kb_no_hit")]
        if any(c["type"] in ENDS_TURN for c in ctx.cards):
            # 出了确认卡 / 澄清卡 / 提示卡：卡片本身就是答复，不再调一次模型写「请核对确认卡」。
            # 省一次调用（约一半耗时），也免得模型把待确认说成「已办理」
            audit.record(db, ctx.trace_id, "llm_skipped", {"reason": "card_ends_turn",
                                                          "cards": [c["type"] for c in ctx.cards]}, ctx.session.id)
            return _template_reply(ctx.cards), "template"

    if not final:
        return _template_reply(ctx.cards), "template"
    # 用户本轮话里的金额也算有据（复述「转 3000 元」不是编造），其余数字只能来自工具结果
    said = collect_facts([{"amount_fen": f} for f in amounts_in(text)])
    facts = collect_facts(datas) | collect_facts(ctx.cards) | _context_facts() | said
    # 对话轮次本身从不执行办理；本轮只有查询类卡片时，「已锁定」这类说法可能是在讲状态
    kinds = {c["type"] for c in ctx.cards}
    opts = {"executed": False, "has_card": bool(kinds),
            "status_only": bool(kinds) and not kinds & {"confirm", "clarify"}}
    g = check(final, facts, **opts)
    if g.ok:
        return _brief(ctx, final), "llm"
    audit.record(db, ctx.trace_id, "grounding_failed", {"text": final, "problem": g.describe()}, ctx.session.id)
    if g.phantom_card:
        return None  # 模型说出了卡片却没调用工具，重写文字也变不出卡片：这一轮由规则引擎确定性地出卡
    messages += [{"role": "assistant", "content": final},
                 {"role": "user", "content": f"[系统校验] 上一条回复有问题：{g.describe()}。请只使用工具结果里的数字重写，并且不要声称已经办理完成（转账、锁卡等都要用户确认后才执行）。"}]
    db.commit()
    retry = _plain(llm.chat(messages, tools=tools, tool_choice="none").content)
    g2 = check(retry, facts, **opts)
    audit.record(db, ctx.trace_id, "grounding_retry", {"text": retry, "ok": g2.ok, "problem": g2.describe()}, ctx.session.id)
    if retry and g2.ok:
        return _brief(ctx, retry), "llm"
    if (g.promises or g2.promises) and not ctx.cards:  # 「有没有保本稳赚的理财」：给一句合规的话，而不是帮助语
        return NO_PROMISE, "template"
    return _template_reply(ctx.cards), "template"  # 两次都不过：丢弃模型文字，只返回模板


_MARKDOWN = re.compile(r"\*\*|__|`|^#{1,6}\s*", re.M)


def _plain(text: str | None) -> str:
    """聊天气泡按纯文本显示：去掉模型爱加的 Markdown 粗体、行内代码和标题符号。"""
    return _MARKDOWN.sub("", text or "").strip()


MAX_REPLY = 60  # 回复字数上限（提示词第 7 条）；卡片上有细节，文字只说结论
_SENTENCE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;\n]*")


def _brief(ctx: ToolContext, text: str) -> str:
    """模型回复超过上限时按整句截短：保留开头几句（至少第一句），不在句子中间截断。
    事实校验已经对全文做过，删句子只会少说，不会多说。"""
    if len(text) <= MAX_REPLY:
        return text
    out = ""
    for s in _SENTENCE.findall(text):
        if out and len(out) + len(s) > MAX_REPLY:
            break
        out += s
    out = re.sub(r"[；;，,、：:]+$", "。", out.strip())  # 截在分号处时补成句号，不像没说完
    audit.record(ctx.db, ctx.trace_id, "reply_trimmed", {"from": len(text), "to": len(out)}, ctx.session.id)
    return out


# ---------- 发给模型的工具：只发这句话涉及的业务域 ----------
# 全部 34 个工具的说明约 5,400 token，一次调用里要多花 3–4 秒；按规则意图只发相关业务域，认不出来时照旧发全部

NO_PROMISE = "理财产品都有风险，本金和收益都不作保证。可以先做风险测评，再看适合你的产品。"
ENDS_TURN = {"confirm", "clarify", "notice"}
# 查余额、转人工任何时候都可能用到；知识问答（WS9）也是：「锁卡和挂失有什么区别」按规则会归到卡片域，但问的是规则
ALWAYS_TOOLS = ("account.balance", "handoff.request", "kb.search")
_INTENT_DOMAIN = {"balance": "account", "transfer": "transfer", "aa": "transfer", "aa_status": "transfer",
                  "card": "card", "wealth": "wealth", "sub": "subscription", "adhoc_analysis": "bill", "faq": "knowledge"}
_CLAUSE = re.compile(r"[，,。.？?！!；;\n]|再|另外|然后|顺便")


def _tool_domains(text: str, messages: list[dict]) -> set[str] | None:
    """这句话涉及的业务域，加上历史里出现过的工具所在的域（「再给我妹转 800」这类接着说的话、
    以及重放的历史工具调用都要有对应的工具定义）。整句或任何一段规则认不出来就返回 None：发全部工具。"""
    domains: set[str] = set()
    for part in (text, *_CLAUSE.split(text)):
        if not (part := part.strip()):
            continue
        intent = rule_intent(part).intent
        domain = "bill" if intent.startswith("bill_") else _INTENT_DOMAIN.get(intent)
        if domain is None:
            return None
        domains.add(domain)
    for m in messages:
        for call in m.get("tool_calls") or []:
            if spec := by_llm_name(call["function"]["name"]):
                domains.add(spec.domain)
    return domains


_CARD_DOMAIN = (("balance", "account"), ("bill_", "bill"), ("card_", "card"), ("sub_", "subscription"),
                ("wealth_", "wealth"), ("aa", "transfer"), ("contact", "transfer"))


# 确定性工作流：规则引擎能完整识别、参数齐全的固定流程，不经过模型。
# 白名单按回归集实测定：规则模式下这些意图的用例和模型给出的卡片一致。
# 明细查询只在规则接得住整句话时走工作流（`bill.workflow_args`），「周末打车比工作日多花多少」这类规则会答偏的交给模型。
# 不在名单里的照常走模型：知识问答、自由分析、规划。
# 每个意图对应模型路径上会调的那个工具：工作流轮次按这个工具名把交互写进历史，下一轮交给模型时上下文和模型自己调过一样
WORKFLOW_TOOLS = {
    "balance": "account.balance", "aa": "aa.propose", "aa_status": "aa.status", "transfer": "transfer.propose",
    "bill_report": "bill.report", "bill_categories": "bill.categories", "bill_anomalies": "bill.anomalies",
    "bill_query": "bill.query",
    "card:list": "card.list", "card:lock": "card.lock_propose", "card:unlock": "card.unlock_propose",
    "card:report_loss": "card.report_loss_propose", "card:limit": "card.limit_propose",
    "card:virtual_apply": "card.virtual_propose",
    "sub:list": "sub.list", "sub:upcoming": "sub.upcoming", "sub:cancel": "sub.cancel_propose",
    "wealth:assess": "wealth.assessment", "wealth:holdings": "wealth.holdings", "wealth:compare": "wealth.compare",
    "wealth:recommend": "wealth.recommend", "wealth:subscribe": "wealth.subscribe_propose",
    "wealth:redeem": "wealth.redeem_propose",
}
FAST_INTENTS = set(WORKFLOW_TOOLS)
_COREF = re.compile(r"[他她它]|那个|那张|这张|那笔|这笔|刚才|上面|同样|也")  # 多轮里指代上文的说法，交给模型理解


def _intent_key(text: str) -> str:
    ri = rule_intent(text)
    return f"{ri.intent}:{ri.slots['op']}" if ri.slots.get("op") else ri.intent


def _payee_answer(ctx: ToolContext, text: str) -> int | None:
    """同名追问（两个张伟）时用户打字回答「同事那个」「尾号0921的」：按备注 / 尾号能唯一确定就返回那位收款人。"""
    pq = (ctx.session.state or {}).get("pending_question") or {}
    if pq.get("kind") != "payee" or "transfer" not in FAST_INTENTS:
        return None
    candidates = [c for c in (ctx.db.get(Contact, i) for i in pq["contact_ids"]) if c and c.user_id == ctx.user_id]
    narrowed = narrow_payees(candidates, text)
    return narrowed[0].id if len(candidates) > 1 and len(narrowed) == 1 else None


def _fast_path(ctx: ToolContext, guard) -> tuple[str, dict] | None:
    """这句话是一个固定流程，且规则引擎拿得到全部参数时直接走规则（确定性工作流），不调模型。返回模型路径上
    对应的工具名和参数（写历史用），不走工作流时返回 None。
    模型路径要一到两次调用（7–13 秒），规则是毫秒级；出的卡片、确认和验证流程完全一样，都过同一套安全外壳。

    条件：没有注入嫌疑；有待回答的追问时只接「同事那个」这种能唯一确定收款人的回答；整句和每个分句都是同一个
    白名单意图（问了两件事交给模型）；多轮里有指代（「她也转 500」「刚才那个取消掉」）交给模型；
    转账要找得到收款人、说了金额；取消订阅要点名商户。"""
    if not get_settings().workflow_enabled or guard.suspicious:
        return None
    text = guard.text
    if (ctx.session.state or {}).get("pending_question"):
        cid = _payee_answer(ctx, text)
        return ("transfer.propose", {"contact_id": cid}) if cid is not None else None
    key = _intent_key(text)
    if key not in FAST_INTENTS or (len(ctx.user_texts) > 1 and _COREF.search(text)):
        return None
    parts = [p.strip() for p in _CLAUSE.split(text) if p.strip()]
    allowed = {key, "unknown"} if key == "aa" else {key}  # AA 的金额、人名本来就分在几个分句里（「火锅 856，我和小王 AA」）
    if not parts or any(_intent_key(p) not in allowed for p in parts):
        return None
    slots = rule_intent(text).slots
    if key == "transfer" and not (parse_amount(slots.get("amount_text") or "") is not None and (
            phones_in(text) or resolve_payee(ctx.db, ctx.user_id, slots["payee_hint"]))):
        return None
    if key == "sub:cancel" and not names_merchant(text):
        return None
    if key == "bill_query":
        args = bill_workflow_args(ctx, text)
        return None if args is None else ("bill.query", args)
    spec = REGISTRY[WORKFLOW_TOOLS[key]]
    return spec.name, {k: v for k, v in slots.items() if k in spec.parameters.get("properties", {}) and v not in (None, "")}


def _record_workflow(ctx: ToolContext, tool_name: str, args: dict) -> None:
    """把工作流这一轮写成一组工具调用存进历史，格式和模型路径一样（`_history` 按轮次重放）。
    不写的话下一轮模型只看到「请核对确认卡」这类文字：会重查一遍（「把它锁一下」又列一次卡），
    或者照抄文字说「已生成确认卡」而不调工具。"""
    if not ctx.cards:
        return
    call_id = f"wf_{ctx.trace_id}"
    exchange = [{"role": "assistant", "content": "", "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": REGISTRY[tool_name].llm_name, "arguments": json.dumps(args, ensure_ascii=False)}}]},
                {"role": "tool", "tool_call_id": call_id,
                 "content": json.dumps({"status": "ok", "cards": ctx.cards}, ensure_ascii=False)}]
    ctx.db.add(ChatMessage(session_id=ctx.session.id, role="tool", trace_id=ctx.trace_id,
                           content=json.dumps(_masked_exchange(exchange), ensure_ascii=False)))


def _order_cards(cards: list[dict], text: str) -> None:
    """一句话问了几件事（「我还有多少钱？这个月钱怎么花这么快？」）时，查询卡片按用户说的先后排，
    不按模型调用工具的先后。有确认卡、澄清卡时不动：那一轮的卡片顺序就是办理顺序。"""
    if len(cards) < 2 or any(c["type"] in ENDS_TURN for c in cards):
        return
    said: dict[str, int] = {}
    for i, part in enumerate(_CLAUSE.split(text)):
        intent = rule_intent(part.strip()).intent if part.strip() else "unknown"
        domain = "bill" if intent.startswith("bill_") else _INTENT_DOMAIN.get(intent)
        if domain:
            said.setdefault(domain, i)

    def rank(card: dict) -> int:
        domain = next((d for prefix, d in _CARD_DOMAIN if card["type"].startswith(prefix)), None)
        return said.get(domain, len(said) + 99)
    cards.sort(key=rank)  # 稳定排序：同一业务域、没提到的业务域保持原来的先后


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
        case "notice" if c.get("breaker"):
            return "安全保护已开启，原因、恢复方式和申诉渠道见下方提示卡。"  # 提示卡全文 100 多字，气泡里不重复
        case "notice":
            return c["text"]
    return c.get("reply") or "好的。"  # 展示类卡片自带按模板渲染的一句话


# ---------- 规则兜底路径 ----------

def _rule_turn(ctx: ToolContext, guard) -> tuple[str, str]:
    text = guard.text
    prefix = NO_BYPASS if _BYPASS_FLAGS & set(guard.flags) else ""
    pq = (ctx.session.state or {}).get("pending_question")
    if (contact_id := _payee_answer(ctx, text)) is not None:  # 打字回答同名追问
        result = propose(ctx, contact_id=contact_id, amount_fen=pq["amount_fen"], memo=pq.get("memo"))
        return prefix + _apply(ctx, result), "rule"
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
    if intent.intent == "faq":  # 知识问答（WS9）
        return prefix + _apply(ctx, kb_rule_turn(ctx, text)), "rule"
    if intent.intent == "adhoc_analysis":
        return prefix + _apply(ctx, adhoc_rule_turn(ctx, text)), "rule"
    if (reply := wealth_rule_turn(ctx, intent, text)) is not None:  # 理财意图，以及「买多少」「赎回多少」追问的回答
        return prefix + reply, "rule"
    if (reply := sub_rule_turn(ctx, intent, text)) is not None:  # 订阅意图，以及「取消哪一个」追问的回答
        return prefix + reply, "rule"
    return prefix + help_text(), "rule"


def _apply(ctx: ToolContext, result: ToolResult) -> str:
    if result.card:
        ctx.cards.append(result.card)
    return _template_reply(ctx.cards)


def _selection_turn(ctx: ToolContext, selection: dict) -> str:
    if selection.get("type") == "risk_answers":  # 理财（WS5）：问卷卡回传用户逐题点选的答案
        return _apply(ctx, submit_quiz(ctx, selection))
    if selection.get("type") == "order_edit":  # 订单确认卡上「修改」：换商品或送达日期，出新的确认卡
        return _apply(ctx, edit_order(ctx, selection))
    if selection.get("type") == "wealth_buy":  # 理财产品详情卡上的「申购」：和说出来走同一个提议工具
        return buy_from_card(ctx, selection)
    if selection.get("type") == "plan":  # 计划卡的按钮（WS7）
        return planner.selection_turn(ctx, selection)
    pq = (ctx.session.state or {}).get("pending_question") or {}
    if selection.get("type") == "contact" and pq.get("kind") == "payee" and selection.get("contact_id") in pq.get("contact_ids", []):
        return _apply(ctx, propose(ctx, contact_id=selection["contact_id"], amount_fen=pq["amount_fen"], memo=pq.get("memo")))
    ctx.cards.append(notice_card("这个选项已经失效，请重新说一下要做什么。", "warning"))
    return _template_reply(ctx.cards)
