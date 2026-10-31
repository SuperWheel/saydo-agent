"""熔断器（注入防御 L8，技术文档 §5.7）。触发条件和阈值在 breaker.yaml。

按用户 / 会话的处置：认证连续失败、疑似注入、发起转账过于频繁、模型越权调用工具 → 只读 / 暂停办理 / 转人工。
按工具的断路器：下游异常率过高 → 断开（closed → open → half_open），冷却后放一次探测请求。
每次触发、拦截、解除都写审计；处置可在运营后台（/admin/risk-events）解除，
用户也可以在熔断提示卡上点「联系人工客服」，由模拟客服核实后解除（security/support.py）。

止损类操作不受任何熔断影响：熔断不能妨碍用户止损。止损按这次调用判断（ToolSpec.is_stop_loss）：
锁卡、挂失整个工具都是（stop_loss=True）；改交易限制只有收紧时是（stop_loss_if）。
被拦下时向用户说明原因类别、恢复方式和申诉渠道（《反电信网络诈骗法》第三十二条）。

安全外壳里只加了调用点，逻辑都在这里：
  auth/actions.py   propose_action → check_propose；用户确认资金转出 → on_money_confirmed；执行前重新校验 → check；
                    验证失败 / 通过 → on_auth_failed / on_auth_passed
  agent/loop.py     L1 注入标记 → on_injection；模型越权调用工具 → on_tool_violation
  tools/gateway.py  执行写工具前 → check；工具调用前后 → circuit_block / tool_ok / tool_failed
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import audit
from ..cards import notice_card
from ..clock import now
from ..flags import get_flag, kill_switch_on, set_flag
from ..models import BreakerTrip, SecurityEvent, SystemFlag
from ..tools import REGISTRY, ToolContext, ToolResult, ToolSpec

CONFIG_PATH = Path(__file__).with_name("breaker.yaml")
LEVEL_LABELS = {"readonly": "会话只读", "freeze": "暂停办理", "handoff": "已转人工", "system": "系统只读保护",
                "circuit": "服务暂不可用"}
_SEVERITY = {"handoff": 3, "freeze": 2, "readonly": 1}
STILL_AVAILABLE = "查询和锁卡、挂失等止损操作不受影响"
SUPPORT_OPERATOR = "模拟人工客服"  # 用户侧核实解除（security/support.py）时 released_by 的前缀


@lru_cache
def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    for t in cfg["triggers"]:
        if t["level"] not in _SEVERITY:
            raise ValueError(f"熔断条件 {t['id']} 的处置等级 {t['level']} 不存在")
        if t["block"] not in ("*", "@moves_money", "@same"):
            raise ValueError(f"熔断条件 {t['id']} 的拦截范围 {t['block']} 不存在")
    return cfg


# ---------- 拦截结果：给用户的熔断提示 ----------

@dataclass
class Block:
    level: str  # readonly / freeze / handoff / system / circuit
    reason: str  # 原因类别
    affects: str  # 暂停了什么
    recover: str  # 怎么恢复
    trip_id: str | None = None
    trigger: str | None = None

    @property
    def text(self) -> str:
        return f"安全保护已开启：{self.reason}。{self.affects}，{self.recover}。{STILL_AVAILABLE}。{load_config()['appeal']}"

    @property
    def card(self) -> dict:
        """熔断提示卡：notice 卡加一段结构化的 breaker 字段，前端据此展示原因、恢复方式和申诉渠道。"""
        return {**notice_card(self.text, "error"), "breaker": {
            "level": self.level, "level_label": LEVEL_LABELS[self.level], "reason": self.reason,
            "affects": self.affects, "recover": self.recover, "still_available": STILL_AVAILABLE,
            "appeal": load_config()["appeal"], "handoff": self.level == "handoff", "trip_id": self.trip_id,
            "support": self.trip_id is not None,  # 有处置记录的才能找人工客服核实解除；系统只读、断路器不行
        }}

    def as_dict(self) -> dict:
        return {"level": self.level, "reason": self.reason, "trip_id": self.trip_id, "trigger": self.trigger}


def _what(selectors: list[str]) -> str:
    def one(sel: str) -> str:
        if sel == "*":
            return "资金和账户变更"
        if sel == "@moves_money":
            return "转账等资金操作"
        spec = REGISTRY.get(sel)
        return load_config()["labels"].get(spec.domain, "该业务") if spec else "该业务"
    return "、".join(dict.fromkeys(one(s) for s in selectors))


def _trip_block(trip: BreakerTrip) -> Block:
    who = "本次会话中的" if trip.session_id else "你的账户"
    affects = f"{who}{_what(trip.tools)}暂停办理"
    if trip.level == "handoff":
        affects, recover = affects + "，需要人工客服核实", "不会自动恢复，请联系人工客服核实后解除"
    elif trip.expires_at:
        minutes = max(1, round((trip.expires_at - now()).total_seconds() / 60))
        recover = f"约 {minutes} 分钟后（{trip.expires_at:%H:%M}）自动恢复，或联系人工客服提前解除"
    elif trip.session_id:
        recover = "开启新会话后恢复，或联系人工客服解除"
    else:
        recover = "请联系人工客服核实后解除"
    return Block(trip.level, trip.reason, affects, recover, trip.id, trip.trigger)


def _system_block(db: Session) -> Block:
    why = get_flag(db, "kill_switch").get("reason") or "系统维护"
    return Block("system", f"系统处于只读保护状态（{why}）", "暂不受理资金和账户变更操作", "恢复时间以公告为准")


# ---------- 检查：策略引擎和工具网关两处都调用 ----------

def _active(trip: BreakerTrip) -> bool:
    return trip.released_at is None and (trip.expires_at is None or trip.expires_at > now())


def _blocks(trip: BreakerTrip, spec: ToolSpec) -> bool:
    return any(sel == "*" or sel == spec.name or (sel == "@moves_money" and spec.moves_money) for sel in trip.tools)


def active_trips(db: Session, user_id: int, session_id: str | None = None) -> list[BreakerTrip]:
    t = now()
    rows = db.scalars(select(BreakerTrip).where(
        BreakerTrip.user_id == user_id, BreakerTrip.released_at.is_(None),
        or_(BreakerTrip.expires_at.is_(None), BreakerTrip.expires_at > t),
        or_(BreakerTrip.session_id.is_(None), BreakerTrip.session_id == session_id),
    )).all()
    return sorted(rows, key=lambda r: -_SEVERITY[r.level])


def check(db: Session, *, user_id: int, tool: str, session_id: str | None, args: dict | None = None) -> Block | None:
    """这次写操作现在能不能办。止损类永远放行；其次看全局只读开关、用户 / 会话处置、工具断路器。"""
    spec = REGISTRY.get(tool)
    if spec is None or spec.is_stop_loss(args):
        return None
    if kill_switch_on(db):
        return _system_block(db)
    for trip in active_trips(db, user_id, session_id):
        if _blocks(trip, spec):
            return _trip_block(trip)
    return circuit_block(db, spec)


def check_propose(ctx: ToolContext, tool: str, args: dict, data: dict | None = None) -> ToolResult | None:
    """提议写操作前的熔断检查。返回 None 表示放行，否则是带熔断提示卡的拦截结果。
    出确认卡不计频率：资金转出在用户点确认时才算一笔（on_money_confirmed）。"""
    db = ctx.db
    block = check(db, user_id=ctx.user_id, tool=tool, session_id=ctx.session.id, args=args)
    if block is None:
        return None
    audit.record(db, ctx.trace_id, "breaker_blocked", {"tool": tool, **block.as_dict()}, ctx.session.id)
    return ToolResult({**(data or {}), "status": "blocked", "breaker": block.level, "reason": block.text},
                      block.card)


# ---------- 安全事件：计数并按触发条件处置 ----------

def _record(db: Session, kind: str, *, user_id: int, session_id: str | None, trace_id: str | None,
            tool: str | None = None, detail: dict | None = None) -> list[BreakerTrip]:
    ev = SecurityEvent(user_id=user_id, session_id=session_id, kind=kind, tool=tool, detail=detail or {}, created_at=now())
    db.add(ev)
    db.flush()
    return [trip for t in load_config()["triggers"] if t["event"] == kind
            if (trip := _evaluate(db, t, ev, trace_id)) is not None]


def _evaluate(db: Session, t: dict, ev: SecurityEvent, trace_id: str | None) -> BreakerTrip | None:
    session_id = ev.session_id if t.get("scope") == "session" else None
    if t.get("scope") == "session" and session_id is None:
        return None
    tool = ev.tool if t.get("per_tool") else None
    q = select(BreakerTrip).where(BreakerTrip.trigger == t["id"], BreakerTrip.user_id == ev.user_id)
    q = q.where(BreakerTrip.session_id == session_id) if session_id else q.where(BreakerTrip.session_id.is_(None))
    # 同一目标的处置不会重叠，水位线越大越新（解除时水位线会推进）
    last = next((r for r in db.scalars(q.order_by(BreakerTrip.watermark.desc())) if tool is None or tool in r.tools), None)
    if last is not None and _active(last):
        return None  # 已经在处置中，不重复触发

    def same(q):
        q = q.where(SecurityEvent.user_id == ev.user_id)
        if session_id:
            q = q.where(SecurityEvent.session_id == session_id)
        return q.where(SecurityEvent.tool == tool) if tool else q

    since = last.watermark if last else 0  # 上次处置之后重新计数
    if reset := t.get("reset_on"):
        since = max(since, db.scalar(same(select(func.max(SecurityEvent.id)).where(SecurityEvent.kind == reset))) or 0)
    q = same(select(func.count()).select_from(SecurityEvent).where(SecurityEvent.kind == t["event"], SecurityEvent.id > since))
    if "window_s" in t:
        q = q.where(SecurityEvent.created_at >= now() - timedelta(seconds=t["window_s"]))
    if (db.scalar(q) or 0) < t["count"]:
        return None
    return _trip(db, t, ev, session_id, tool, trace_id)


def _trip(db: Session, t: dict, ev: SecurityEvent, session_id: str | None, tool: str | None,
          trace_id: str | None) -> BreakerTrip:
    reason = t["reason"].format(count=t["count"], minutes=t.get("window_s", 0) // 60)
    trip = BreakerTrip(
        id="B" + secrets.token_hex(8), user_id=ev.user_id, session_id=session_id, trigger=t["id"], level=t["level"],
        tools=[ev.tool] if t["block"] == "@same" else [t["block"]], reason=reason, trace_id=trace_id, watermark=ev.id,
        created_at=now(), expires_at=now() + timedelta(seconds=t["duration_s"]) if t.get("duration_s") else None,
    )
    db.add(trip)
    db.flush()
    audit.record(db, trace_id or "system", "breaker_tripped", {
        "trip_id": trip.id, "trigger": trip.trigger, "level": trip.level, "scope": "session" if session_id else "user",
        "tools": trip.tools, "reason": reason, "event": ev.kind, "tool": tool,
        "expires_at": trip.expires_at.isoformat() if trip.expires_at else None,
    }, ev.session_id)
    return trip


def on_auth_failed(db: Session, action) -> Block | None:
    """强认证失败一次。返回拦截结果表示已触发熔断（或本来就在熔断中），本次动作应直接取消。"""
    _record(db, "auth_failed", user_id=action.user_id, session_id=action.session_id, trace_id=action.trace_id,
            tool=action.tool)
    return check(db, user_id=action.user_id, tool=action.tool, session_id=action.session_id)


def on_money_confirmed(db: Session, action) -> None:
    """用户点了确认的一笔资金转出，计入「发起资金转出」频率。只在确认时记：没确认就作废 / 取消 / 过期的卡、
    改参数后作废的旧卡都不算；同一张卡只能确认一次，不会重复算。已经在熔断中的不再计数。
    触发熔断时不在这里拦：确认流程紧接着的执行前重新校验（check）会把这一笔拦下、作废并释放额度。"""
    spec = REGISTRY.get(action.tool)
    if spec is None or not spec.moves_money or spec.is_stop_loss(action.args):
        return
    if check(db, user_id=action.user_id, tool=action.tool, session_id=action.session_id, args=action.args) is not None:
        return
    _record(db, "money_confirmed", user_id=action.user_id, session_id=action.session_id, trace_id=action.trace_id,
            tool=action.tool, detail={"action_id": action.id, "summary": action.display.get("summary", "")})


def on_auth_passed(db: Session, action) -> None:
    _record(db, "auth_passed", user_id=action.user_id, session_id=action.session_id, trace_id=action.trace_id,
            tool=action.tool)


def on_injection(db: Session, *, user_id: int, session_id: str, trace_id: str, flags: list[str]) -> list[BreakerTrip]:
    cfg = load_config()["injection"]
    counted = [f for f in flags if f in cfg["counted_flags"]]
    if not counted:
        return []
    high = any(f in cfg["high_confidence_flags"] for f in flags) or \
        (cfg.get("invisible_with_flag_is_high") and "invisible_chars" in flags)
    trips = _record(db, "injection", user_id=user_id, session_id=session_id, trace_id=trace_id, detail={"flags": flags})
    if high:
        trips += _record(db, "injection_high", user_id=user_id, session_id=session_id, trace_id=trace_id,
                         detail={"flags": flags})
    return trips


def on_tool_violation(db: Session, *, user_id: int, session_id: str, trace_id: str, tool: str, error: str) -> None:
    """模型调用了写工具 / 不存在的工具，或在参数里夹带白名单外的字段。"""
    _record(db, "tool_violation", user_id=user_id, session_id=session_id, trace_id=trace_id, tool=tool,
            detail={"error": error[:200]})


# ---------- 按工具的断路器 ----------
# 状态存在 system_flags 的 circuit:<工具名>。正常时不写库：出现第一次失败后才开始记录窗口内的调用，
# 错误率 = 窗口内（从第一次失败起）的失败数 / 调用数；失败都滑出窗口后停止记录。

def _ts() -> int:
    return int(now().timestamp())


def circuit_block(db: Session, spec: ToolSpec) -> Block | None:
    c = get_flag(db, f"circuit:{spec.name}")
    if spec.stop_loss or c.get("state", "closed") == "closed":
        return None
    cooldown = load_config()["circuit"]["cooldown_s"]
    if c["state"] == "open" and _ts() - c["opened_at"] >= cooldown:
        set_flag(db, f"circuit:{spec.name}", {**c, "state": "half_open"})  # 冷却结束：放下一次调用去探测
        audit.record(db, "system", "circuit_half_open", {"tool": spec.name})
        return None
    if c["state"] == "half_open":
        return None
    label = load_config()["labels"].get(spec.domain, "该业务")
    wait = max(1, cooldown - (_ts() - c["opened_at"]))
    return Block("circuit", f"{label}的后台服务暂时异常", f"{label}暂停受理", f"约 {wait} 秒后自动重试")


def _window(c: dict) -> list[list[int]]:
    start = _ts() - load_config()["circuit"]["window_s"]
    return [x for x in c.get("calls", []) if x[0] >= start]


def tool_ok(db: Session, spec: ToolSpec) -> None:
    key = f"circuit:{spec.name}"
    c = get_flag(db, key)
    if not c:
        return
    if c.get("state") == "half_open":
        set_flag(db, key, {})
        audit.record(db, "system", "circuit_closed", {"tool": spec.name, "by": "probe"})
        return
    calls = [*_window(c), [_ts(), 1]]
    set_flag(db, key, {**c, "calls": calls} if any(ok == 0 for _, ok in calls) else {})


def tool_failed(db: Session, spec: ToolSpec, error: Exception, *, trace_id: str | None = None) -> None:
    cfg, key = load_config()["circuit"], f"circuit:{spec.name}"
    c = get_flag(db, key) or {"state": "closed"}
    calls = [*_window(c), [_ts(), 0]]
    c = {**c, "calls": calls, "last_error": f"{type(error).__name__}: {error}"[:200]}
    failures = sum(1 for _, ok in calls if ok == 0)
    if c["state"] == "half_open" or (
            c["state"] == "closed" and len(calls) >= cfg["min_calls"] and failures / len(calls) > cfg["error_rate"]):
        c |= {"state": "open", "opened_at": _ts()}
        audit.record(db, trace_id or "system", "circuit_open", {
            "tool": spec.name, "calls": len(calls), "failures": failures, "error": c["last_error"]})
    set_flag(db, key, c)


# ---------- 运营后台 ----------

class BreakerError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def release(db: Session, trip_id: str, *, operator: str, note: str) -> BreakerTrip:
    trip = db.get(BreakerTrip, trip_id)
    if trip is None:
        raise BreakerError(404, "熔断记录不存在")
    if not _active(trip):
        raise BreakerError(409, "该熔断已经失效，不需要解除")
    trip.released_at, trip.released_by, trip.release_note = now(), operator, note
    trip.watermark = db.scalar(select(func.max(SecurityEvent.id))) or 0  # 解除后重新计数
    audit.record(db, "admin", "breaker_released", {"trip_id": trip.id, "trigger": trip.trigger, "level": trip.level,
                                                   "operator": operator, "note": note, "trace_id": trip.trace_id})
    return trip


def reset_circuit(db: Session, tool: str, *, operator: str) -> None:
    if not get_flag(db, f"circuit:{tool}"):
        raise BreakerError(404, "该工具没有断路记录")
    set_flag(db, f"circuit:{tool}", {})
    audit.record(db, "admin", "circuit_closed", {"tool": tool, "by": operator})


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def trip_view(trip: BreakerTrip) -> dict:
    status = "released" if trip.released_at else "active" if _active(trip) else "expired"
    return {
        "id": trip.id, "user_id": trip.user_id, "session_id": trip.session_id,
        "scope": "session" if trip.session_id else "user", "trigger": trip.trigger, "level": trip.level,
        "level_label": LEVEL_LABELS[trip.level], "tools": trip.tools, "reason": trip.reason, "status": status,
        "trace_id": trip.trace_id, "created_at": _iso(trip.created_at), "expires_at": _iso(trip.expires_at),
        "released_at": _iso(trip.released_at), "released_by": trip.released_by, "release_note": trip.release_note,
        "released_via": None if not trip.released_by else
        "support" if trip.released_by.startswith(SUPPORT_OPERATOR) else "admin",
        "notice": _trip_block(trip).text if status == "active" else None,
    }


def overview(db: Session, limit: int = 50) -> dict:
    trips = db.scalars(select(BreakerTrip).order_by(BreakerTrip.created_at.desc()).limit(limit)).all()
    events = db.scalars(select(SecurityEvent).order_by(SecurityEvent.id.desc()).limit(limit)).all()
    circuits = db.scalars(select(SystemFlag).where(SystemFlag.key.like("circuit:%"))).all()
    views = sorted((trip_view(t) for t in trips), key=lambda v: v["status"] != "active")
    return {
        "trips": views,
        "events": [{"id": e.id, "kind": e.kind, "user_id": e.user_id, "session_id": e.session_id, "tool": e.tool,
                    "detail": e.detail, "at": _iso(e.created_at)} for e in events],
        "circuits": [{"tool": f.key.removeprefix("circuit:"), "state": f.value.get("state", "closed"),
                      "calls": len(f.value.get("calls", [])), "last_error": f.value.get("last_error")}
                     for f in circuits if f.value],
        "kill_switch": get_flag(db, "kill_switch"),
        "config": {"version": load_config()["version"], "appeal": load_config()["appeal"],
                   "triggers": load_config()["triggers"], "circuit": load_config()["circuit"]},
    }
