"""任务规划（技术文档 §4.4）：多步请求 → JSON 计划 → 确定性校验 → 计划卡 → 用户确认后按步执行。

入口只有 route()（agent/loop.py 在模型和规则之前调用）：
  1. 会话里有进行中的计划时，先认控制语：继续、「蛋糕不要了」（重新规划）、「撤销锁定」（补偿）、取消计划、转人工；
  2. 「撤销锁定」「转人工」「每月 15 号给我妈转 2000」这类必须确定性处理的话（否则规则会当成锁卡、立即转账）；
  3. 多步请求（跨场景动作 ≥ 2 个，或「生日 / 纪念日」+ 订花 / 存钱）才进规划器：有模型用模型出计划，
     模型不可用时用规则模板出计划；两者都过同一个校验器，校验不过就不执行、退回追问；
  4. 刚出过候选商品卡时的「要第二个」（两种模式都确定性处理，按候选卡顺序选）；
  5. 规则模式下其余单步的心愿罐、买东西（出候选卡）、定时转账（tools/automation.py 的 rule_turn）。
其余返回 None，照常走模型或规则。

安全边界：计划里只能出现已注册的模型可见工具（只读工具和提议工具），写操作仍由提议工具生成确认卡，
每一步都单独走确认 →（🔴 强验证）→ 令牌 → 网关，确认计划不等于确认任何一笔付款。
金额只能来自用户原话，报价单号、心愿罐编号只能引用前序步骤的结果（{{s1.pot_id}}），模型不能自己填。
"""

import json
import re
import secrets
from collections.abc import Callable
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from .. import audit
from ..auth import actions
from ..cards import auth_reply, notice_card
from ..clock import now
from ..config import get_settings
from ..core_mock.shops import CATEGORY_LABELS, SHOPS
from ..models import ChatMessage, ChatSession, PendingAction, TaskPlan
from ..money import fmt_yuan, parse_amount
from ..nlu import amount_from_user, asks_rule, normalize_hint, rule_intent
from ..nlu.when import parse_date, parse_when
from ..security import mask_pii
from ..tools import REGISTRY, ToolContext, ToolResult, ToolSpec, by_llm_name, model_tools
from ..tools import automation as auto
from ..tools.gateway import GatewayError, run_read_tool
from .llm import LLMError

MAX_STEPS = 12
DUE_DAYS_BEFORE = 2  # 临期步骤在事件日前 2 天 10:00 执行（「生日前 2 天自动询价」）
DUE_HOUR = 10
ACTIVE = ("draft", "running", "waiting", "rolling_back")

# 规划器提示词里介绍的工具，以及每个工具可以被后续步骤引用的输出字段。校验器的白名单是全部模型可见工具，
# 但只有这里声明过的输出字段才能被引用。
PLAN_TOOLS = {
    "pot.lock_propose": ("把一笔钱存进心愿罐（生成确认卡）", ("pot_id",)),
    "merchant.quote": ("向合作商户询价（category 填 flower 鲜花 / cake 蛋糕 等类目代码），按收礼人挑一件，只读", ("quote_id",)),
    "order.propose": ("按报价下单（生成订单确认卡），pot_id 填心愿罐步骤的引用就从心愿罐付款", ("order_id",)),
    "transfer.propose": ("立即转账（生成确认卡）", ("txn_no",)),
    "schedule.transfer_propose": ("设置定时 / 每月转账（生成确认卡）", ("schedule_id",)),
    "account.balance": ("查余额，只读", ()),
}
_AMOUNT_KEYS = ("amount_text",)
_PAYEE_KEYS = ("payee_hint",)
_ID_KEYS = ("quote_id", "pot_id", "order_id", "schedule_id")  # 只能来自前序步骤的输出
_DATE_KEYS = ("until", "date")
_REF = re.compile(r"^\{\{\s*(s\d{1,2})\.(\w+)\s*\}\}$")
_SCALAR = (str, int, float, bool, type(None))

STEP_LABELS = {"pending": "待执行", "awaiting": "待你确认", "done": "已完成", "failed": "未完成",
               "cancelled": "已取消", "removed": "已移出计划"}
_TERMINAL = ("done", "cancelled", "removed")


class PlanRejected(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("；".join(reasons))
        self.reasons = reasons


def _is_write(spec: ToolSpec) -> bool:
    return spec.name.endswith("propose")  # 提议工具：生成确认卡，本身不写数据


def _spec(name: str) -> ToolSpec | None:
    return REGISTRY.get(str(name)) or by_llm_name(str(name))


# ---------- 校验器（确定性） ----------

def _ancestors(steps: dict[str, dict], sid: str) -> set[str]:
    seen, todo = set(), list(steps[sid]["depends_on"])
    while todo:
        x = todo.pop()
        if x not in seen:
            seen.add(x)
            todo.extend(steps[x]["depends_on"])
    return seen


def _topo(steps: list[dict]) -> list[str] | None:
    """Kahn 拓扑排序；有环返回 None。"""
    deps = {s["id"]: set(s["depends_on"]) for s in steps}
    order, ready = [], [s["id"] for s in steps if not deps[s["id"]]]
    while ready:
        x = ready.pop(0)
        order.append(x)
        for s in steps:
            if x in deps[s["id"]]:
                deps[s["id"]].discard(x)
                if not deps[s["id"]]:
                    ready.append(s["id"])
    return order if len(order) == len(steps) else None


def check_structure(steps: list[dict]) -> list[str]:
    """依赖存在、无环、引用只指向祖先步骤且字段已声明。重新规划、追加补偿步骤后也跑一遍。"""
    errors = []
    by_id = {s["id"]: s for s in steps}
    for s in steps:
        for d in s["depends_on"]:
            if d not in by_id or d == s["id"]:
                errors.append(f"{s['id']} 依赖了不存在的步骤 {d}")
    if errors:
        return errors
    if _topo(steps) is None:
        return ["步骤之间存在循环依赖"]
    for s in steps:
        anc = _ancestors(by_id, s["id"])
        for k, v in s["args"].items():
            if isinstance(v, str) and (m := _REF.match(v)):
                ref, field = m.groups()
                if ref not in anc:
                    errors.append(f"{s['id']}.{k} 引用了不是前序步骤的 {ref}")
                elif field not in PLAN_TOOLS.get(by_id[ref]["tool"], ("", ()))[1]:
                    errors.append(f"{s['id']}.{k} 引用的 {ref}.{field} 不是该步骤的输出")
    return errors


def _in_text(value: str, text: str) -> bool:
    v = normalize_hint(value).removeprefix("我的").replace(" ", "")
    return bool(v) and v in text.replace(" ", "")


def validate(raw: dict, *, source_text: str, at: datetime) -> tuple[str, date | None, list[dict]]:
    """检查模型（或规则模板）给出的计划。通过时返回 (目标, 事件日期, 规范化的步骤)，否则抛 PlanRejected。"""
    errors: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list) or not raw["steps"]:
        raise PlanRejected(["计划格式不对"])
    if len(raw["steps"]) > MAX_STEPS:
        raise PlanRejected([f"步骤超过 {MAX_STEPS} 个，请拆开办理"])
    goal = mask_pii(re.sub(r"[\x00-\x1f\x7f]", "", str(raw.get("goal") or "多步任务")))[:24]
    event_date = None
    if raw.get("event_date"):
        try:
            event_date = date.fromisoformat(str(raw["event_date"])[:10])
        except ValueError:
            errors.append("事件日期格式不对")
    said = parse_date(source_text, at)
    if event_date and said and event_date != said:
        errors.append(f"事件日期 {event_date} 和你说的不一致")
    event_date = event_date or said
    allowed = {s.name for s in model_tools()}
    steps, seen = [], set()
    for i, s in enumerate(raw["steps"]):
        if not isinstance(s, dict):
            errors.append(f"第 {i + 1} 步格式不对")
            continue
        sid = str(s.get("id") or f"s{i + 1}")
        spec = _spec(s.get("tool", ""))
        if not re.fullmatch(r"s\d{1,2}", sid) or sid in seen:
            errors.append(f"步骤编号 {sid} 不合法或重复")
        seen.add(sid)
        if spec is None or spec.name not in allowed:  # 写工具、未注册工具一律拒绝：写操作只能通过提议工具出确认卡
            errors.append(f"{sid} 使用了不允许的工具 {s.get('tool')}")
            continue
        args = s.get("args") or {}
        depends = s.get("depends_on") or []
        if not isinstance(args, dict) or not isinstance(depends, list):
            errors.append(f"{sid} 的参数格式不对")
            continue
        props = spec.parameters.get("properties", {})
        if unknown := set(args) - set(props):
            errors.append(f"{sid} 有未知参数 {', '.join(sorted(unknown))}")
        if missing := [k for k in spec.parameters.get("required", []) if args.get(k) in (None, "")]:
            errors.append(f"{sid} 缺少参数 {', '.join(missing)}")
        for k, v in args.items():
            if not isinstance(v, _SCALAR):
                errors.append(f"{sid}.{k} 只能是标量")
            elif isinstance(v, str) and "{{" in v and not _REF.match(v):
                errors.append(f"{sid}.{k} 的引用格式不对")
            elif isinstance(v, str) and _REF.match(v):
                continue
            elif k in _ID_KEYS and v not in (None, ""):
                errors.append(f"{sid}.{k} 只能引用前序步骤的结果，不能直接填写")
            elif k in _AMOUNT_KEYS and v not in (None, ""):
                fen = parse_amount(str(v))
                if fen is None or not amount_from_user(fen, [source_text]):
                    errors.append(f"{sid} 的金额「{v}」不是你说的")
            elif k in _PAYEE_KEYS and v and not _in_text(str(v), source_text):
                errors.append(f"{sid} 的收款人「{v}」不是你说的")
            elif k in _DATE_KEYS and v:
                try:
                    d = date.fromisoformat(str(v)[:10])
                except ValueError:
                    errors.append(f"{sid}.{k} 日期格式不对")
                    continue
                if not at.date() <= d <= at.date() + timedelta(days=366):
                    errors.append(f"{sid}.{k} 日期超出范围")
            elif k == "when_text" and v:
                mine, theirs = parse_when(source_text, at), parse_when(str(v), at)
                if theirs is None or (mine and (mine.first, mine.recurrence) != (theirs.first, theirs.recurrence)):
                    errors.append(f"{sid} 的执行时间和你说的不一致")
        when = s.get("at", "now")
        if when not in ("now", "due") or (when == "due" and event_date is None):
            errors.append(f"{sid} 的执行时机不对")
        steps.append({"id": sid, "tool": spec.name, "args": {k: v for k, v in args.items() if v not in (None, "")},
                      "depends_on": [str(d) for d in depends], "at": when, "kind": "write" if _is_write(spec) else "read",
                      "origin": "plan", "status": "pending"})
    if not errors:
        errors = check_structure(steps)
    if errors:
        raise PlanRejected(errors)
    return goal, event_date, steps


# ---------- 规则模板：模型不可用时的规划 ----------

_EVENT = re.compile(r"生日|纪念日|周年|过节|结婚")
_PERSON = [("妈妈", re.compile(r"我妈|妈妈|老妈|母亲")), ("爸爸", re.compile(r"我爸|爸爸|老爸|父亲")),
           ("爱人", re.compile(r"爱人|老婆|媳妇|妻子|太太")), ("妹妹", re.compile(r"我妹|妹妹"))]
_ORDER_WORD = re.compile(r"(订|买|预订|准备|送|定)[^，,。；;]{0,8}(花|蛋糕)")
_WANT = {"flower": re.compile(r"花(?!钱|了|费|在)"), "cake": re.compile(r"蛋糕")}


def _person(text: str) -> str:
    return next((name for name, pat in _PERSON if pat.search(text)), "")


def _clauses(text: str) -> list[str]:
    return [c for c in re.split(r"[，,。；;！!]|然后|并且|再|还要|顺便", text) if c.strip()]


def _transfer_clauses(text: str) -> list[dict]:
    out = []
    for c in _clauses(text):
        if auto.schedule_slots(c):
            continue
        it = rule_intent(c)
        if it.intent == "transfer" and it.slots.get("amount_text"):
            out.append(it.slots)
    return out


def action_kinds(text: str) -> dict[str, int]:
    kinds: dict[str, int] = {}
    if _ORDER_WORD.search(text):
        kinds["order"] = sum(bool(p.search(text)) for p in _WANT.values()) or 1
    if auto.pot_slots(text):
        kinds["pot"] = 1
    if auto.schedule_slots(text):
        kinds["schedule"] = 1
    if n := len(_transfer_clauses(text)):
        kinds["transfer"] = n
    return kinds


def is_multi_step(text: str) -> bool:
    """多步请求才进规划器：跨场景动作合计 ≥ 2 个（订花和蛋糕算 2 个），或者为某个日子订花订蛋糕（询价 + 下单）。"""
    kinds = action_kinds(text)
    if not kinds or set(kinds) == {"transfer"}:  # 纯转账（含「再给我妹转」）照旧单步处理
        return False
    return sum(kinds.values()) >= 2 or bool(_EVENT.search(text) and "order" in kinds)


def template_plan(text: str, at: datetime) -> dict | None:
    kinds = action_kinds(text)
    if not kinds:
        return None
    who = _person(text)
    event = parse_date(text, at) if _EVENT.search(text) else None
    occasion = (m := _EVENT.search(text)) and m.group() or ""
    goal = f"{who}{occasion}准备" if who and occasion else "多步任务"
    steps, n = [], 0

    def add(tool, args, depends=(), when="now"):
        nonlocal n
        n += 1
        steps.append({"id": f"s{n}", "tool": tool, "args": args, "depends_on": list(depends), "at": when})
        return f"s{n}"

    pot_id = None
    if (slots := auto.pot_slots(text)) and slots["amount_text"]:
        pot_id = add("pot.lock_propose", {"amount_text": slots["amount_text"],
                                          "purpose": slots["purpose"] or (f"{who}{occasion}" if who else "心愿"),
                                          "until": f"{event}" if event else None})
    if "order" in kinds:
        for cat, pat in _WANT.items():
            if not pat.search(text):
                continue
            when = "due" if event else "now"
            q = add("merchant.quote", {"category": cat, "for_whom": who, "date": f"{event}" if event else None}, when=when)
            args = {"quote_id": f"{{{{{q}.quote_id}}}}"}
            if pot_id:
                args["pot_id"] = f"{{{{{pot_id}.pot_id}}}}"
            add("order.propose", args, [q, *([pot_id] if pot_id else [])], when=when)
    for t in _transfer_clauses(text):
        add("transfer.propose", {"payee_hint": t["payee_hint"], "amount_text": t["amount_text"], "memo": t.get("memo")})
    if s := auto.schedule_slots(text):
        add("schedule.transfer_propose", s)
    return {"goal": goal, "event_date": f"{event}" if event else None, "steps": steps} if steps else None


# ---------- 模型规划 ----------

PLANNER_PROMPT = """你是手机银行助手的任务规划器。把用户的多步请求拆成 JSON 计划。只输出一个 JSON 对象，不要任何解释或代码块标记。
现在是 {now}（星期{weekday}）。

可用工具（只能用这些，名称、参数名必须一字不差）：
{tools}

规则：
1. 金额参数 amount_text 照抄用户原话（如「500」），不要换算，也不要填用户没说过的金额。
   收款人参数 payee_hint 也照抄原话（用户说「我妈」就写「我妈」，不要改成「妈妈」「母亲」）。
2. 后面的步骤要用前面步骤的结果时，写成引用，例如 "{{{{s2.quote_id}}}}"、"{{{{s1.pot_id}}}}"，并把被引用的步骤写进 depends_on。
   quote_id、pot_id、order_id 只能这样引用，绝对不要自己编。
3. 为某一天（生日、纪念日）准备的询价和下单，at 写 "due"（系统会在那天前 2 天自动执行，到时再请用户确认订单）；其他步骤写 "now"。
4. event_date 是那一天的日期 YYYY-MM-DD；没有就写 null。日期参数也写 YYYY-MM-DD。
5. 付款、转账、存钱都只是生成确认卡，用户逐笔确认后才执行。

格式：{{"goal": "不超过 12 个字", "event_date": "YYYY-MM-DD 或 null", "steps": [
  {{"id": "s1", "tool": "工具名", "args": {{...}}, "depends_on": [], "at": "now"}}]}}"""


def _tool_lines() -> str:
    lines = []
    for name, (desc, outputs) in PLAN_TOOLS.items():
        spec = REGISTRY.get(name)
        if spec is None:
            continue
        params = ", ".join(f"{k}{'' if k in spec.parameters.get('required', []) else '?'}"
                           for k in spec.parameters.get("properties", {}))
        out = f" → 输出 {', '.join(outputs)}" if outputs else ""
        lines.append(f"- {spec.llm_name}({params})：{desc}{out}")
    return "\n".join(lines)


def _extract_json(content: str) -> dict:
    s = content.strip()
    if "```" in s:
        s = re.sub(r"^.*?```(?:json)?|```.*$", "", s, flags=re.S).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("规划器没有输出 JSON")
    return json.loads(s[start:end + 1])


def _llm_plan(llm, ctx: ToolContext, text: str, at: datetime) -> dict:
    prompt = PLANNER_PROMPT.format(now=f"{at:%Y-%m-%d %H:%M}", weekday="一二三四五六日"[at.weekday()], tools=_tool_lines())
    ctx.db.commit()  # 调模型前结束事务，不占着写锁
    reply = llm.chat([{"role": "system", "content": prompt}, {"role": "user", "content": mask_pii(text)}])
    audit.record(ctx.db, ctx.trace_id, "llm_call", {"step": "planner", "model": llm.settings.llm_model,
                                                    "usage": reply.usage, "tool_calls": []}, ctx.session.id)
    return _extract_json(reply.content)


# ---------- 计划卡 ----------

def _step_title(plan_steps: list[dict], s: dict) -> str:
    a = s["args"]
    by_id = {x["id"]: x for x in plan_steps}
    match s["tool"]:
        case "pot.lock_propose":
            fen = parse_amount(str(a.get("amount_text", "")))
            return f"存 ¥{fmt_yuan(fen) if fen else a.get('amount_text')} 进心愿罐（{a.get('purpose') or '心愿'}）"
        case "merchant.quote":
            cat = auto.category_of(a.get("category", "")) or ""
            shops = "、".join(sh.name for sh in SHOPS.values() if sh.category == cat)
            return f"询价：{CATEGORY_LABELS.get(cat, cat)}（{shops}）"
        case "order.propose":
            ref = _REF.match(str(a.get("quote_id", "")))
            cat = by_id[ref.group(1)].get("category") if ref and ref.group(1) in by_id else None
            src = "，从心愿罐付款" if a.get("pot_id") else ""
            return f"下单{CATEGORY_LABELS.get(cat, '')}{src}"
        case "transfer.propose":
            fen = parse_amount(str(a.get("amount_text", "")))
            return f"给{a.get('payee_hint')}转 ¥{fmt_yuan(fen) if fen else a.get('amount_text')}"
        case "schedule.transfer_propose":
            return f"定时转账：{a.get('when_text')}给{a.get('payee_hint')}转 {a.get('amount_text')}"
        case "pot.release_propose":
            return "释放心愿罐，剩余资金退回活期"
        case "order.cancel_propose":
            return f"取消{s.get('item') or ''}订单并退款"
    spec = REGISTRY.get(s["tool"])
    return spec.capability or spec.name


def _decorate(steps: list[dict]) -> None:
    for s in steps:
        if s["tool"] == "merchant.quote":
            s["category"] = auto.category_of(s["args"].get("category", ""))
    by_id = {s["id"]: s for s in steps}
    for s in steps:
        ref = _REF.match(str(s["args"].get("quote_id", "")))
        if s["tool"] == "order.propose" and ref and ref.group(1) in by_id:
            s["category"] = by_id[ref.group(1)].get("category")
        s["title"] = _step_title(steps, s)


def _risk(s: dict) -> str:
    if s["kind"] == "read":
        return "🟢 自动"
    if s.get("level") == "RED":
        return "🔴 需强验证"
    return "🟡 需你确认"


def plan_card(plan: TaskPlan) -> dict:
    due = plan.due_at
    steps = []
    for s in plan.steps:
        at_label = f"临期 {due:%m-%d %H:%M}" if s["at"] == "due" and due and s["status"] == "pending" else ""
        steps.append({"id": s["id"], "title": s["title"], "status": s["status"],
                      "status_label": STEP_LABELS[s["status"]], "kind": s["kind"], "risk": _risk(s),
                      "at_label": at_label, "detail": s.get("detail") or s.get("note") or ""})
    stuck = any(s["status"] == "failed" for s in plan.steps)  # 有没办成的步骤时才显示「继续」（重试）
    ops = {"draft": ["confirm", "cancel"], "running": ["advance"] * stuck + ["cancel", "handoff"],
           "waiting": ["fast_forward", "cancel", "handoff"], "rolling_back": ["advance"] * stuck + ["handoff"]
           }.get(plan.status, [])
    if plan.status == "waiting" and not get_settings().demo_mode:
        ops.remove("fast_forward")
    return {"type": "plan", "plan_id": plan.id, "goal": plan.goal, "status": plan.status,
            "status_label": auto.PLAN_STATUS_LABELS[plan.status], "version": plan.version, "note": plan.note or "",
            "event_date": f"{plan.event_date}" if plan.event_date else None,
            "due_at": due.isoformat() if due else None, "due_label": f"{due:%m-%d %H:%M}" if due else "",
            "steps": steps, "ops": ops, "source": plan.source,
            # 移出计划、撤销的步骤：前端把对话里这些确认卡标成已作废（后端早已作废，这里只是让画面一致）
            "void_action_ids": [s["action_id"] for s in plan.steps
                                if s.get("action_id") and s["status"] in ("removed", "cancelled")],
            "hint": "确认计划不等于确认付款：每一笔存入、下单、转账都会单独出确认卡。"}


# ---------- 执行器 ----------

def _resolve(plan: TaskPlan, args: dict) -> dict:
    by_id = {s["id"]: s for s in plan.steps}
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and (m := _REF.match(v)):
            value = (by_id[m.group(1)].get("output") or {}).get(m.group(2))
            if value is None:
                raise GatewayError(f"前序步骤没有给出 {m.group(2)}")
            out[k] = value
        else:
            out[k] = v
    return out


def _scalars(d: dict | None) -> dict:
    return {k: v for k, v in (d or {}).items() if isinstance(v, _SCALAR) and k != "note"}


def _audit_tool(ctx: ToolContext, tool: str, args: dict, result: ToolResult, **extra) -> None:
    """和模型调用工具同样的审计事件，回放和回归集都按它统计。"""
    audit.record(ctx.db, ctx.trace_id, "tool_call", {
        "tool": tool, "args": mask_pii(json.dumps(args, ensure_ascii=False))[:500],
        "status": result.data.get("status", "ok"), "card": result.card["type"] if result.card else None, **extra,
    }, ctx.session.id)


def _run_step(ctx: ToolContext, plan: TaskPlan, s: dict) -> bool:
    """执行一步。只读步骤直接拿结果；写步骤由提议工具生成确认卡，等用户确认。返回这一步是否已完成。"""
    spec = REGISTRY[s["tool"]]
    try:
        args = _resolve(plan, s["args"])
        result = run_read_tool(spec, ctx, args)  # 与模型调用工具同一个网关：白名单参数、断路器、保存点
    except GatewayError as e:
        s["status"], s["note"] = "failed", str(e)
        return False
    _audit_tool(ctx, spec.name, args, result, plan_id=plan.id, step=s["id"])
    card = result.card
    if s["kind"] != "read":
        if card and card["type"] == "confirm":
            s["status"], s["action_id"], s["level"] = "awaiting", card["action_id"], card["level"]
            s["auth_hint"] = card.get("auth_hint", "")
            s["note"] = ""
        else:
            s["status"] = "failed"
            s["note"] = (card or {}).get("text") or result.data.get("message") or "没能生成确认卡"
        if card:
            ctx.cards.append(card)
        return False
    if result.data.get("status") not in (None, "ok"):
        s["status"], s["note"] = "failed", result.data.get("message") or "查询没有结果"
        return False
    s["status"], s["output"] = "done", _scalars(result.data)
    if spec.name == "merchant.quote":
        d = result.data
        s["detail"] = f"{d['shop_label']} · {d['item']} ¥{fmt_yuan(d['price_fen'])} · {d['delivery']}"
    elif card:
        ctx.cards.append(card)
    return True


def _sync(db: Session, plan: TaskPlan) -> bool:
    """把确认卡的结果同步到步骤上。返回有没有变化。"""
    actions.expire_stale(db, plan.user_id)
    changed = False
    for s in plan.steps:
        if s["status"] != "awaiting":
            continue
        a = db.get(PendingAction, s["action_id"])
        if a is None or a.status in actions.ACTIVE or a.status == "executing":
            continue
        changed = True
        if a.status == "succeeded":
            s["status"], s["output"] = "done", _scalars(a.result)
            s["detail"] = a.display.get("summary", "")
            if s["tool"] == "order.propose":
                s["item"] = a.display.get("fields", {}).get("item")
        elif a.status == "cancelled":
            s["status"], s["note"] = "cancelled", "你取消了这一步"
        else:
            s["status"] = "failed"
            s["note"] = {"expired": "确认卡已过期", "invalidated": "授权已作废（情况有变化）"}.get(
                a.status, (a.result or {}).get("error") or "没有办成")
    return changed


def _cascade(plan: TaskPlan) -> None:
    by_id = {s["id"]: s for s in plan.steps}
    moved = True
    while moved:
        moved = False
        for s in plan.steps:
            if s["status"] == "pending" and any(by_id[d]["status"] in ("cancelled", "removed") for d in s["depends_on"]):
                s["status"], s["note"], moved = "cancelled", "前一步取消了，这一步不再执行", True


def touch(plan: TaskPlan) -> None:
    flag_modified(plan, "steps")  # 步骤是就地修改的 JSON，要显式标记才会写回


def _finish(plan: TaskPlan) -> None:
    if plan.status in ("draft", "cancelled", "handoff"):
        return
    if plan.status == "rolling_back" and not any(s["kind"] == "compensate" and s["status"] not in _TERMINAL
                                                 for s in plan.steps):
        for s in plan.steps:  # 补偿都办完了：其余没执行的步骤不再执行
            if s["status"] in ("pending", "failed"):
                s["status"], s["note"] = "cancelled", "计划已撤销"
    live = [s for s in plan.steps if s["status"] not in _TERMINAL]
    if not live:
        plan.status = "cancelled" if plan.status == "rolling_back" else "done"
    elif plan.status != "rolling_back":
        waiting = all(s["status"] == "pending" and s["at"] == "due" for s in live)
        plan.status = "waiting" if waiting and plan.due_at and now() < plan.due_at else "running"


def advance(ctx: ToolContext, plan: TaskPlan) -> None:
    """按拓扑顺序推进：只读步骤直接执行，写步骤一次只出一张确认卡（写操作串行，顺序确定）。"""
    ctx.user_texts = [plan.source_text, *ctx.user_texts]  # 参数来源校验：计划里的金额来自当初那句话
    _sync(ctx.db, plan)
    by_id = {s["id"]: s for s in plan.steps}
    order = _topo([s for s in plan.steps]) or [s["id"] for s in plan.steps]
    for _ in range(MAX_STEPS * 2):
        _cascade(plan)
        if any(s["status"] == "awaiting" for s in plan.steps):
            break
        due_ok = plan.due_at is None or now() >= plan.due_at
        ready = [by_id[i] for i in order if by_id[i]["status"] == "pending"
                 and all(by_id[d]["status"] in ("done", "removed") if by_id[i]["kind"] == "compensate"
                         else by_id[d]["status"] == "done" for d in by_id[i]["depends_on"])
                 and (by_id[i]["at"] != "due" or due_ok)
                 and (plan.status != "rolling_back" or by_id[i]["kind"] == "compensate")]  # 回退中只做补偿
        if not ready:
            break
        reads = [s for s in ready if s["kind"] == "read"]
        if reads:
            if not all([_run_step(ctx, plan, s) for s in reads]):
                break
            continue
        _run_step(ctx, plan, ready[0])
    _finish(plan)
    touch(plan)


def _progress_text(plan: TaskPlan) -> str:
    awaiting = next((s for s in plan.steps if s["status"] == "awaiting"), None)
    failed = next((s for s in plan.steps if s["status"] == "failed"), None)
    if awaiting:
        return f"下一步：{awaiting['title']}，请核对下方确认卡。{auth_reply(awaiting)}"
    if failed:
        return f"这一步没办成：{failed['title']}（{failed['note']}）。可以说「继续」重试，或说「转人工」。"
    if plan.status == "waiting":
        return f"前面的步骤办好了。临期步骤会在 {plan.due_at:%m-%d %H:%M} 自动询价，到时请你确认订单。"
    if plan.status == "done":
        return "计划里的步骤都办完了。"
    if plan.status == "cancelled":
        return "计划已撤销，需要回退的步骤都已处理。"
    return "计划已更新。"


# ---------- 计划的创建、控制、回退 ----------

def _new_plan(ctx: ToolContext, *, goal: str, event_date: date | None, steps: list[dict], text: str,
              source: str) -> TaskPlan:
    due = None
    if event_date and any(s["at"] == "due" for s in steps):
        due = datetime.combine(event_date - timedelta(days=DUE_DAYS_BEFORE), datetime.min.time()).replace(hour=DUE_HOUR)
        if due <= now():  # 日子太近：临期步骤确认计划后马上执行
            due = None
            for s in steps:
                s["at"] = "now"
    _decorate(steps)
    plan = TaskPlan(id="PL" + secrets.token_hex(5), user_id=ctx.user_id, session_id=ctx.session.id, goal=goal,
                    source_text=text, source=source, event_date=event_date, due_at=due, status="draft", steps=steps)
    ctx.db.add(plan)
    ctx.db.flush()
    return plan


def create_plan(ctx: ToolContext, text: str, llm: Callable | None) -> tuple[str, str]:
    at, raw, source = now(), None, "rule"
    if llm is not None:
        try:
            raw, source = _llm_plan(llm(), ctx, text, at), "llm"
        except (LLMError, ValueError) as e:  # 模型不可用或没给出 JSON：退回规则模板（模板同样要过校验）
            audit.record(ctx.db, ctx.trace_id, "planner_fallback", {"error": str(e)[:200]}, ctx.session.id)
            raw = None
    if raw is None:
        raw, source = template_plan(text, at), "rule"
    if raw is None:
        return "这件事要分几步办，我没能完整理解。可以分开说，比如先说「存500进心愿罐」，再说「帮我订一束花」。", source
    try:
        goal, event_date, steps = validate(raw, source_text=text, at=at)
    except PlanRejected as e:
        audit.record(ctx.db, ctx.trace_id, "plan_rejected", {"source": source, "reasons": e.reasons,
                                                             "plan": mask_pii(json.dumps(raw, ensure_ascii=False))[:1500]},
                     ctx.session.id)
        ctx.cards.append(notice_card(f"这个计划没有通过安全校验（{e.reasons[0]}），我先不执行。", "warning"))
        return f"这个计划没有通过安全校验（{e.reasons[0]}），我先不执行。请把要办的事说具体一些，比如金额、收款人和日期。", source
    plan = _new_plan(ctx, goal=goal, event_date=event_date, steps=steps, text=text, source=source)
    audit.record(ctx.db, ctx.trace_id, "plan_created", {"plan_id": plan.id, "source": source, "goal": goal,
                                                        "steps": [{k: s[k] for k in ("id", "tool", "depends_on", "at")}
                                                                  for s in plan.steps]}, ctx.session.id)
    card = plan_card(plan)
    _audit_tool(ctx, "plan.create", {"steps": len(steps)}, ToolResult({"status": "draft"}, card), plan_id=plan.id)
    ctx.cards.append(card)
    writes = sum(s["kind"] == "write" for s in plan.steps)
    due = f"其中询价和下单会在 {plan.due_at:%m-%d %H:%M}（{plan.event_date:%m-%d} 前 {DUE_DAYS_BEFORE} 天）自动开始。" if plan.due_at else ""
    return (f"我把这件事拆成了 {len(plan.steps)} 步，请先看一下计划。{due}"
            f"确认后我按步骤办，其中 {writes} 步涉及资金，每一步都会单独请你确认。"), source


def _cancel_action(ctx: ToolContext, s: dict) -> None:
    a = ctx.db.get(PendingAction, s.get("action_id") or "")
    if a is not None and a.status in actions.ACTIVE:
        actions.cancel(ctx.db, action_id=a.id, user_id=ctx.user_id)


def _dependents(plan: TaskPlan, roots: set[str]) -> set[str]:
    out, moved = set(roots), True
    while moved:
        moved = False
        for s in plan.steps:
            if s["id"] not in out and out & set(s["depends_on"]):
                out.add(s["id"])
                moved = True
    return out


def _add_compensation(plan: TaskPlan, tool: str, args: dict, title: str, after: str | None) -> str:
    sid = f"s{max(int(s['id'][1:]) for s in plan.steps) + 1}"
    plan.steps.append({"id": sid, "tool": tool, "args": args, "depends_on": [after] if after else [], "at": "now",
                       "kind": "compensate", "origin": "system", "status": "pending", "title": title})
    return sid


def _withdraw(ctx: ToolContext, plan: TaskPlan, ids: set[str]) -> list[dict]:
    """把这些步骤移出计划：没执行的直接移除，等确认的作废确认卡；已执行的返回出来，由调用方决定怎么补偿。"""
    done = []
    order = _topo(plan.steps) or [s["id"] for s in plan.steps]
    for sid in reversed(order):  # 逆拓扑序
        s = next(x for x in plan.steps if x["id"] == sid)
        if sid not in ids or s["status"] in ("removed", "cancelled"):
            continue
        if s["status"] == "awaiting":
            _cancel_action(ctx, s)
        if s["status"] == "done":
            done.append(s)
            continue
        s["status"], s["note"] = "removed", ""
    return done


def _compensate(ctx: ToolContext, plan: TaskPlan, done_steps: list[dict]) -> list[str]:
    """已执行的步骤按逆序追加补偿步骤：先取消订单（退款回心愿罐），再释放心愿罐。转账已到账的无法自动撤回。"""
    notes, last = [], None
    orders = [s for s in done_steps if s["tool"] == "order.propose"]
    pots = [s for s in done_steps if s["tool"] == "pot.lock_propose"]
    for s in orders:
        title = f"取消{s.get('item') or CATEGORY_LABELS.get(s.get('category'), '')}订单并退款"
        last = _add_compensation(plan, "order.cancel_propose", {"order_hint": s["output"]["order_id"]}, title, last)
        s["status"], s["note"] = "removed", "已安排取消并退款"
    for s in pots:
        last = _add_compensation(plan, "pot.release_propose", {"pot_hint": s["output"]["pot_id"]},
                                 "释放心愿罐，剩余资金退回活期", last)
        s["status"], s["note"] = "removed", "已安排释放"
    for s in done_steps:
        if s["tool"] == "transfer.propose":
            notes.append(f"其中「{s['title']}」已到账，不能自动撤回，需要追款可以说「转人工」。")
    return notes


def _replan(ctx: ToolContext, plan: TaskPlan, note: str) -> tuple[str, str]:
    if errors := check_structure(plan.steps):  # 改过的计划再校验一次
        plan.status = "cancelled"
        return f"重新规划没有通过校验（{errors[0]}），计划已停止。", "rule"
    plan.version += 1
    plan.note = f"第 {plan.version} 版：{note}"
    if plan.status != "draft":
        advance(ctx, plan)
    touch(plan)
    card = plan_card(plan)
    _audit_tool(ctx, "plan.replan", {"note": note}, ToolResult({"status": plan.status}, card), plan_id=plan.id,
                version=plan.version)
    ctx.cards.insert(0, card)
    tail = "" if plan.status == "draft" else _progress_text(plan)
    return f"好的，{note}，已重新规划。{tail}", "rule"


def drop_item(ctx: ToolContext, plan: TaskPlan, cat: str) -> tuple[str, str]:
    _sync(ctx.db, plan)
    roots = {s["id"] for s in plan.steps if s.get("category") == cat and s["status"] not in ("removed", "cancelled")}
    label = CATEGORY_LABELS[cat]
    if not roots:
        return f"计划里没有{label}，不需要调整。", "rule"
    done = _withdraw(ctx, plan, _dependents(plan, roots))
    for s in done:
        if s["kind"] == "read":
            s["status"] = "removed"
    notes = _compensate(ctx, plan, [s for s in done if s["kind"] != "read"])
    note = f"去掉了{label}" + ("，已下单的需要取消并退款" if any(s["tool"] == "order.propose" for s in done) else "")
    text, src = _replan(ctx, plan, note)
    return text + "".join(notes), src


def undo_lock(ctx: ToolContext, plan: TaskPlan | None) -> tuple[str, str] | None:
    """「撤销锁定」：释放心愿罐。计划里还没下的、要从心愿罐付款的订单随之移出计划。"""
    if plan is not None:
        _sync(ctx.db, plan)
        pots = [s for s in plan.steps if s["tool"] == "pot.lock_propose" and s["status"] not in ("removed", "cancelled")]
        if pots:
            ids = {s["id"] for s in pots}
            deps = _dependents(plan, ids) - ids
            if plan.status == "draft":  # 还没确认计划：去掉存钱这步，订单改从活期付款
                for s in plan.steps:
                    if s["id"] in ids:
                        s["status"] = "removed"
                    if s["id"] in deps:
                        s["args"].pop("pot_id", None)
                        s["depends_on"] = [d for d in s["depends_on"] if d not in ids]
                        s["title"] = _step_title(plan.steps, s)
                return _replan(ctx, plan, "去掉了心愿罐，订单改从活期付款")
            done = _withdraw(ctx, plan, ids | {d for d in deps if not _order_done(plan, d)})
            plan.status = "rolling_back"
            notes = _compensate(ctx, plan, [s for s in done if s["tool"] == "pot.lock_propose"])
            text, src = _replan(ctx, plan, "撤销心愿罐锁定，还没下的订单不再下单")
            return text + "".join(notes), src
    pot, hits = auto._pick_pot(ctx.db, ctx.user_id, "")
    if not hits:
        pending = ctx.db.scalars(select(PendingAction).where(  # 存入还没确认：直接作废那张确认卡
            PendingAction.session_id == ctx.session.id, PendingAction.tool == "pot.lock",
            PendingAction.status.in_(actions.ACTIVE))).all()
        for a in pending:
            actions.cancel(ctx.db, action_id=a.id, user_id=ctx.user_id)
        if pending:
            ctx.cards.append(notice_card("已作废存入心愿罐的确认卡，没有发生资金变动。"))
            return "好的，存入心愿罐已取消，没有发生资金变动。", "rule"
        return None
    result = auto.pot_release_propose(ctx, "")
    _audit_tool(ctx, "pot.release_propose", {}, result)
    return _reply_for(ctx, result), "rule"


def _order_done(plan: TaskPlan, sid: str) -> bool:
    s = next(x for x in plan.steps if x["id"] == sid)
    return s["tool"] == "order.propose" and s["status"] == "done"


def cancel_plan(ctx: ToolContext, plan: TaskPlan) -> tuple[str, str]:
    _sync(ctx.db, plan)
    if plan.status == "draft":
        plan.status = "cancelled"
        card = plan_card(plan)
        _audit_tool(ctx, "plan.cancel", {}, ToolResult({"status": "cancelled"}, card), plan_id=plan.id)
        ctx.cards.append(card)
        return "好的，计划已取消，什么都没有办理。", "rule"
    done = _withdraw(ctx, plan, {s["id"] for s in plan.steps})
    for s in done:
        if s["kind"] == "read":
            s["status"] = "removed"
    plan.status = "rolling_back"
    notes = _compensate(ctx, plan, [s for s in done if s["kind"] != "read"])
    text, src = _replan(ctx, plan, "撤销整个计划，已办的步骤按相反顺序回退")
    return text + "".join(notes), src


def request_handoff(ctx: ToolContext, plan: TaskPlan | None) -> tuple[str, str]:
    package = {}
    if plan is not None:
        _sync(ctx.db, plan)
        package["plan"] = {"plan_id": plan.id, "goal": plan.goal, "status": plan.status,
                           "steps": [{"title": s["title"], "status": s["status"]} for s in plan.steps]}
        plan.status = "handoff"
    card = auto.handoff(ctx.db, user_id=ctx.user_id, session_id=ctx.session.id, trace_id=ctx.trace_id,
                        reason="你申请了人工服务", package=package)
    _audit_tool(ctx, "handoff.request", {}, ToolResult({"status": "handoff"}, card))
    if plan is not None:
        ctx.cards.append(plan_card(plan))
    ctx.cards.append(card)
    return card["text"], "rule"


def confirm_plan(ctx: ToolContext, plan: TaskPlan) -> tuple[str, str]:
    if plan.status != "draft":
        return continue_plan(ctx, plan)
    plan.status = "running"
    audit.record(ctx.db, ctx.trace_id, "plan_confirmed", {"plan_id": plan.id, "version": plan.version}, ctx.session.id)
    advance(ctx, plan)
    ctx.cards.insert(0, plan_card(plan))
    return "计划已确认，开始按步骤办理。" + _progress_text(plan), "rule"


def continue_plan(ctx: ToolContext, plan: TaskPlan) -> tuple[str, str]:
    if plan.status == "draft":
        return confirm_plan(ctx, plan)
    _sync(ctx.db, plan)
    for s in plan.steps:  # 「继续」= 重试没办成的步骤（重新出确认卡）
        if s["status"] == "failed":
            s["status"], s["note"] = "pending", ""
    advance(ctx, plan)
    ctx.cards.insert(0, plan_card(plan))
    return _progress_text(plan), "rule"


# ---------- 入口 ----------

_CONTINUE = re.compile(r"^(继续|下一步|接着办|接着来|继续办|好的?，?继续|确认计划|开始吧?|执行计划)[吧。！!]?$")
_HANDOFF = re.compile(r"转人工|人工客服|找人工|真人客服|人工服务|转接客服|找客服")
_CANCEL_PLAN = re.compile(r"(取消|撤销|放弃|不要了|算了)[^，,。]{0,4}(计划|全部|所有|整个)|(全部|都|计划|整个)[^，,。]{0,2}(不要了|取消|撤销|算了|不办了)")
_DROP = re.compile(r"(?P<a>蛋糕|鲜花|花束|花(?!钱|费))[^，,。]{0,3}(不要了|不用了|不订了|算了|取消|别订了|不买了|去掉)"
                   r"|(不要|别订|不用订|取消|不订|去掉|不买)(那个|那束|那份|了)?(?P<b>蛋糕|鲜花|花束|花(?!钱|费|了))")
_UNDO_LOCK = re.compile(r"(撤销|撤回|取消|释放|解除|退回|不存了)[^，,。]{0,8}(锁定|心愿罐|存的钱|预留|存入)"
                        r"|心愿罐[^，,。]{0,6}(释放|退回|退出来|不要了|取出|撤销|取消)")


def _undo_lock_said(text: str) -> bool:
    """「撤销锁定」说的是心愿罐；「取消信用卡的锁定」是卡片业务，不归这里。"""
    return bool(_UNDO_LOCK.search(text)) and ("心愿罐" in text or "卡" not in text)


def active_plan(db: Session, session_id: str, *, include_done: bool = False) -> TaskPlan | None:
    """会话里最近的进行中计划。include_done：已办完的计划也算（「蛋糕不要了」「撤销锁定」要对它做补偿）。"""
    statuses = (*ACTIVE, "done") if include_done else ACTIVE
    return db.scalars(select(TaskPlan).where(TaskPlan.session_id == session_id, TaskPlan.status.in_(statuses))
                      .order_by(TaskPlan.created_at.desc())).first()


def _reply_for(ctx: ToolContext, result: ToolResult) -> str:
    if result.card:
        ctx.cards.append(result.card)
    card = result.card or {}
    if card.get("type") == "confirm":
        return "请核对确认卡，无误后点击确认。" + auth_reply(card)
    return card.get("text") or card.get("reply") or result.data.get("message") or "好的。"


def _control(ctx: ToolContext, plan: TaskPlan, text: str) -> tuple[str, str] | None:
    if _HANDOFF.search(text):
        return request_handoff(ctx, plan)
    if _CANCEL_PLAN.search(text):
        return cancel_plan(ctx, plan)
    if _undo_lock_said(text):
        return undo_lock(ctx, plan)
    if m := _DROP.search(text):
        return drop_item(ctx, plan, auto.category_of(m.group("a") or m.group("b")))
    if _CONTINUE.match(text.strip()) and plan.status != "done":
        return continue_plan(ctx, plan)
    return None


def route(ctx: ToolContext, guard, llm: Callable | None = None) -> tuple[str, str] | None:
    """跨场景（WS7）入口，见模块说明。llm 是取模型客户端的函数（模型不可用时为 None）。"""
    text = (guard.text or "").strip()
    if not text:
        return None
    plan = active_plan(ctx.db, ctx.session.id, include_done=True)
    if plan is not None and (out := _control(ctx, plan, text)) is not None:
        touch(plan)
        return out
    if _HANDOFF.search(text):
        return request_handoff(ctx, None)
    if _undo_lock_said(text) and (out := undo_lock(ctx, None)) is not None:
        return out
    if (picked := auto.pick_turn(ctx, text)) is not None:  # 候选卡之后「要第二个」
        args, result = picked
        _audit_tool(ctx, "order.propose", args, result)
        if (result.card or {}).get("type") == "confirm":
            ctx.cards.append(result.card)
            return f"好的，选了{result.data['shop']}的{result.data['item']}，请核对订单确认卡。", "rule"
        return _reply_for(ctx, result), "rule"
    if asks_rule(text):  # 「用助手订花付款要不要人脸验证」问的是规则：不出候选商品、不进定时转账，交给知识问答
        return None
    if is_multi_step(text):
        return create_plan(ctx, text, llm)
    if slots := auto.schedule_slots(text):  # 「每月15号给我妈转2000」：两种模式都确定性处理，不能被当成立即转账
        result = auto.schedule_transfer_propose(ctx, **slots)
        _audit_tool(ctx, "schedule.transfer_propose", slots, result)
        return _reply_for(ctx, result), "rule"
    if (slots := auto.cancel_slots(ctx, text)) is not None:  # 「帮我退款鞋子」：模型只拿到账单工具时会说没下过单
        result = auto.order_cancel_propose(ctx, **slots)
        _audit_tool(ctx, "order.cancel_propose", slots, result)
        return _reply_for(ctx, result), "rule"
    if llm is None and (out := auto.rule_turn(ctx, text)) is not None:
        reply, calls = out
        for name, args, result in calls:
            _audit_tool(ctx, name, args, result)
        name, _, last = calls[-1]
        keep = reply is not None and (name == "automation.list" or (last.card or {}).get("type") == "confirm")
        if not keep:
            return _reply_for(ctx, last), "rule"
        ctx.cards.append(last.card)
        return reply, "rule"
    return None


_OPS = {"confirm": confirm_plan, "advance": continue_plan, "cancel": cancel_plan,
        "handoff": lambda ctx, plan: request_handoff(ctx, plan)}


def selection_turn(ctx: ToolContext, selection: dict) -> str:
    """计划卡上的按钮（确认计划 / 继续 / 取消 / 转人工）。"""
    plan = ctx.db.get(TaskPlan, str(selection.get("plan_id") or ""))
    op = _OPS.get(selection.get("op"))
    if plan is None or plan.user_id != ctx.user_id or op is None or plan.status not in ACTIVE:
        ctx.cards.append(notice_card("这个计划已经结束或失效了。", "warning"))
        return "这个计划已经结束或失效了。"
    audit.record(ctx.db, ctx.trace_id, "plan_op", {"plan_id": plan.id, "op": selection.get("op")}, ctx.session.id)
    text = op(ctx, plan)[0]
    touch(plan)
    return text


# ---------- 确认卡办完后的同步、时间快进触发的临期任务 ----------

def _context(db: Session, session: ChatSession, trace_id: str) -> ToolContext:
    return ToolContext(db=db, user_id=session.user_id, session=session, trace_id=trace_id, user_texts=[])


def _say(db: Session, session_id: str | None, text: str, trace_id: str) -> None:
    if session_id and db.get(ChatSession, session_id) is not None:
        db.add(ChatMessage(session_id=session_id, role="assistant", content=text, trace_id=trace_id))


def sync_session(db: Session, *, user_id: int, session_id: str | None) -> dict:
    """确认卡办完（成功、取消、过期）后前端调一次：计划里的步骤跟着推进，出下一张确认卡。"""
    session = db.get(ChatSession, session_id) if session_id else None
    plan = active_plan(db, session_id) if session is not None and session.user_id == user_id else None
    if plan is None or plan.status == "draft" or not _sync(db, plan):
        return {"text": None, "cards": []}
    trace_id = secrets.token_hex(8)
    ctx = _context(db, session, trace_id)
    advance(ctx, plan)
    text = _progress_text(plan)
    audit.record(db, trace_id, "plan_synced", {"plan_id": plan.id, "status": plan.status}, session_id)
    _say(db, session_id, text, trace_id)
    return {"text": text, "trace_id": trace_id, "cards": [plan_card(plan), *ctx.cards]}


def tick(db: Session, *, user_id: int) -> list[dict]:
    """定时扫描（演示时由时间快进触发）：到期的定时转账、到期的心愿罐、到了临期时间的计划。"""
    trace_id = "sched-" + secrets.token_hex(5)
    audit.record(db, trace_id, "scheduler_tick", {"user_id": user_id, "now": now().isoformat()})
    events = [{"session_id": sid, "text": text, "cards": [card]}
              for sid, text, card in auto.run_due_transfers(db, user_id, trace_id)]
    for plan in db.scalars(select(TaskPlan).where(TaskPlan.user_id == user_id,
                                                  TaskPlan.status.in_(("running", "waiting")),
                                                  TaskPlan.due_at <= now())).all():
        session = db.get(ChatSession, plan.session_id)
        ctx = _context(db, session, trace_id)
        before = [s["status"] for s in plan.steps]
        advance(ctx, plan)
        if [s["status"] for s in plan.steps] == before:
            continue  # 还在等用户确认前面的步骤
        when = f"（{plan.event_date:%m-%d}）" if plan.event_date else ""
        text = f"「{plan.goal}」的日子快到了{when}，已按计划为你询价。" + _progress_text(plan)
        events.append({"session_id": plan.session_id, "text": text, "cards": [plan_card(plan), *ctx.cards]})
    events += [{"session_id": sid, "text": text, "cards": [card]}
               for sid, text, card in auto.release_expired_pots(db, user_id, trace_id)]
    for e in events:
        _say(db, e["session_id"], e["text"], trace_id)
        e["trace_id"] = trace_id
    return events
