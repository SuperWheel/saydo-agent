"""模拟人工客服：核实后解除熔断（演示环境，技术文档 §5.7）。

用户在熔断提示卡上点「联系人工客服」：排队 → 客服接入 → 按熔断原因核实（核对触发熔断的那几次操作、确认是否本人、
会话只读以外再做一次模拟身份验证）→ 核实通过由「客服」调 breaker.release 解除，审计里记操作人和核实记录；
用户说不是本人操作就保持冻结，建议锁卡 / 挂失。真实上线时这一步由坐席在工作台完成，这里只模拟用户看到的流程。

只能处理自己账户的熔断。每次核实（通过或不通过）都写一条 support_review 安全事件（不计入任何触发条件）和审计，
运营后台「熔断与风险」能看到解除来自模拟人工客服和核实记录。
"""

import hmac

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..clock import now
from ..models import BreakerTrip, SecurityEvent
from . import breaker
from .breaker import SUPPORT_OPERATOR, BreakerError, load_config

AGENT = "小言"  # 模拟坐席的名字
BANNER = "模拟人工客服 · 演示环境"

# 按触发熔断的事件问一句「是不是你本人」：(问题, 是, 否)
QUESTIONS = {
    "money_confirmed": ("上面这几笔资金转出，都是你本人确认的吗？", "是我本人操作的", "有不是我操作的"),
    "auth_failed": ("这几次身份验证没通过，是你本人在操作吗？", "是我本人，输错了", "不是我在操作"),
    "injection": ("这些消息是你本人发的吗？", "是我发的（测试或误操作）", "不是我发的"),
    "injection_high": ("这些消息是你本人发的吗？", "是我发的（测试或误操作）", "不是我发的"),
    "tool_violation": ("这是 AI 助手的越权调用，已经拦下，你的账户没有变化。现在是你本人在使用这个会话吗？", "是我本人", "不是我"),
}
_DEFAULT_Q = ("最近的这些操作是你本人做的吗？", "是我本人操作的", "不是我")

FLAG_LABELS = {
    "ignore_instructions": "要求忽略规则", "role_override": "要求切换身份", "forged_system": "伪造系统消息",
    "encoded_injection": "编码夹带指令", "bypass_auth": "要求跳过验证", "authority_claim": "自称管理员或工作人员",
}
OUTCOMES = {"released": "核实通过，已解除", "rejected": "用户否认本人操作，保持冻结", "identity_failed": "身份验证未通过，保持冻结"}


def _trip(db: Session, trip_id: str, user_id: int) -> BreakerTrip:
    trip = db.get(BreakerTrip, trip_id)
    if trip is None or trip.user_id != user_id:  # 别人的熔断当作不存在
        raise BreakerError(404, "熔断记录不存在")
    if not breaker._active(trip):
        raise BreakerError(409, "这次安全保护已经解除或到期，可以直接继续办理")
    return trip


def _trigger(trip: BreakerTrip) -> dict | None:
    return next((t for t in load_config()["triggers"] if t["id"] == trip.trigger), None)


def _describe(e: SecurityEvent) -> str:
    if e.kind == "money_confirmed":
        return e.detail.get("summary") or "资金转出"
    if e.kind == "auth_failed":
        return f"{breaker._what([e.tool])}的身份验证没有通过"
    if e.kind in ("injection", "injection_high"):
        flags = "、".join(dict.fromkeys(FLAG_LABELS[f] for f in e.detail.get("flags", []) if f in FLAG_LABELS))
        return f"消息里出现疑似绕过验证的内容（{flags or '可疑指令'}）"
    if e.kind == "tool_violation":
        return "AI 助手尝试越权调用工具，已拦下"
    return e.kind


def _evidence(db: Session, trip: BreakerTrip) -> list[dict]:
    """触发这次熔断的那几次事件（最多 count 条，按时间先后），给用户逐条核对。"""
    t = _trigger(trip)
    if t is None:  # 手工建的处置没有对应的触发条件
        return []
    q = select(SecurityEvent).where(SecurityEvent.user_id == trip.user_id, SecurityEvent.kind == t["event"],
                                    SecurityEvent.id <= trip.watermark)
    if trip.session_id:
        q = q.where(SecurityEvent.session_id == trip.session_id)
    if t.get("per_tool"):
        q = q.where(SecurityEvent.tool.in_(trip.tools))
    rows = db.scalars(q.order_by(SecurityEvent.id.desc()).limit(t["count"])).all()
    return [{"at": f"{e.created_at:%H:%M:%S}", "text": _describe(e)} for e in reversed(rows)]


def _need_identity(trip: BreakerTrip) -> bool:
    return trip.level != "readonly"  # 会话只读开新会话就能恢复，核实是否本人就够；暂停办理和转人工都要验身份


def script(db: Session, trip_id: str, *, user_id: int) -> dict:
    """客服接入后的核实脚本：问候、触发记录、要问的问题、是否需要身份验证。"""
    trip = _trip(db, trip_id, user_id)
    block = breaker._trip_block(trip)
    t = _trigger(trip)
    question, yes, no = QUESTIONS.get(t["event"] if t else "", _DEFAULT_Q)
    return {
        "trip_id": trip.id, "level": trip.level, "level_label": breaker.LEVEL_LABELS[trip.level],
        "reason": trip.reason, "affects": block.affects, "recover": block.recover,
        "auto_recover_at": f"{trip.expires_at:%H:%M}" if trip.expires_at else None,
        "agent": AGENT, "banner": BANNER, "queue_ahead": 2,
        "greeting": f"你好，我是人工客服{AGENT}（模拟）。看到你的账户因为「{trip.reason}」开启了安全保护"
                    f"（{breaker.LEVEL_LABELS[trip.level]}）。为了你的资金安全，需要和你核实几件事，大约 1 分钟。",
        "evidence": _evidence(db, trip), "question": question, "yes": yes, "no": no,
        "need_identity": _need_identity(trip),
        "identity_prompt": "请完成一次身份验证（模拟人脸识别，演示环境不采集真实人脸）",
    }


def review(db: Session, trip_id: str, *, user_id: int, self_operated: bool, identity: str = "") -> dict:
    """提交核实结果。本人操作且（需要时）身份验证通过 → 由模拟客服解除；否则保持冻结。"""
    trip = _trip(db, trip_id, user_id)
    evidence, need_identity = _evidence(db, trip), _need_identity(trip)
    identity_ok = hmac.compare_digest(identity.encode(), f"face-ok:{trip.id}".encode())  # 模拟人脸识别：断言绑定这次熔断
    steps = [f"核对触发记录 {len(evidence)} 条" if evidence else "说明熔断原因",
             "用户确认为本人操作" if self_operated else "用户表示不是本人操作"]
    if self_operated:
        steps.append(("模拟人脸识别通过" if identity_ok else "模拟人脸识别未通过") if need_identity else "会话只读，免身份验证")
    outcome = "rejected" if not self_operated else "identity_failed" if need_identity and not identity_ok else "released"
    record = "；".join(steps)
    operator = f"{SUPPORT_OPERATOR}·{AGENT}"
    db.add(SecurityEvent(user_id=user_id, session_id=trip.session_id, kind="support_review", detail={
        "trip_id": trip.id, "outcome": outcome, "outcome_label": OUTCOMES[outcome], "record": record, "operator": operator}, created_at=now()))
    db.flush()
    audit.record(db, "support", "breaker_support_review", {
        "trip_id": trip.id, "trigger": trip.trigger, "level": trip.level, "outcome": outcome, "record": record,
        "operator": operator, "trace_id": trip.trace_id}, trip.session_id)

    if outcome == "rejected":
        return {"status": "kept", "outcome": outcome, "record": record,
                "message": "好的，为了你的资金安全，安全保护会继续保持，期间不会有资金转出。建议你马上锁卡；"
                           "如果卡片丢失或可能被盗用，请挂失。我们已记录本次情况，工作人员会尽快联系你（演示环境不会真的来电）。",
                "advice": [{"label": "立即锁卡", "text": "帮我锁卡"}, {"label": "挂失银行卡", "text": "我卡丢了，帮我挂失"}]}
    if outcome == "identity_failed":
        return {"status": "kept", "outcome": outcome, "record": record,
                "message": "身份验证没有通过，安全保护保持不变。可以再验证一次，或拨打客服热线 95xxx。"}
    breaker.release(db, trip.id, operator=operator, note=f"核实通过：{record}"[:200])
    others = [r.reason for r in breaker.active_trips(db, user_id) if r.id != trip.id]
    message = "核实通过，已为你解除这次安全保护，核实记录已存档。现在可以继续办理了。"
    if others:
        message += f"另外还有 {len(others)} 项安全保护在生效（{'、'.join(others)}），需要的话可以再联系我核实。"
    return {"status": "released", "outcome": outcome, "record": record, "message": message, "remaining": others,
            "trip": breaker.trip_view(trip)}
