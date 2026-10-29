"""HTTP 接口。M0 没有登录，固定使用演示用户（DEMO_USER_ID）。"""

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, clock
from ..agent.loop import handle_turn
from ..auth import actions
from ..config import get_settings
from ..core_mock.ledger import is_balanced, ledger_balance
from ..core_mock.seed import seed
from ..db import Base, get_db, get_engine, new_session
from ..flags import kill_switch_on, set_flag
from ..models import Account, AuditLog, PendingAction
from ..money import fmt_yuan
from ..security import breaker

api = APIRouter(prefix="/api")
admin = APIRouter(prefix="/admin")
dev = APIRouter(prefix="/dev")


def current_user_id() -> int:
    return get_settings().demo_user_id


class ChatIn(BaseModel):
    session_id: str | None = None
    text: str = Field(default="", max_length=500)
    selection: dict | None = None


@api.post("/chat")
def chat(body: ChatIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)):
    if not body.text.strip() and not body.selection:
        raise HTTPException(400, "消息不能为空")
    r = handle_turn(db, user_id=user_id, session_id=body.session_id, text=body.text, selection=body.selection)
    return {"session_id": r.session_id, "trace_id": r.trace_id, "reply": {"text": r.text, "source": r.source,
            "ai_generated": r.ai_generated}, "cards": r.cards}


def _outcome(fn, db: Session, **kwargs):
    try:
        out = fn(db, **kwargs)
    except actions.ActionError as e:
        db.commit()  # 拒绝事件也要落审计
        raise HTTPException(e.status_code, str(e)) from e
    db.commit()
    return {"status": out.status, "card": out.card, "message": out.message}


class ConfirmIn(BaseModel):
    digest: str


@api.post("/actions/{action_id}/confirm")
def confirm(action_id: str, body: ConfirmIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)):
    return _outcome(actions.confirm, db, action_id=action_id, digest=body.digest, user_id=user_id)


@api.post("/actions/{action_id}/cancel")
def cancel(action_id: str, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)):
    return _outcome(actions.cancel, db, action_id=action_id, user_id=user_id)


class VerifyIn(BaseModel):
    factor: Literal["face", "sms"]
    value: str = Field(max_length=200)


@api.post("/auth/challenges/{challenge_id}/verify")
def verify(challenge_id: str, body: VerifyIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)):
    return _outcome(actions.verify_factor, db, challenge_id=challenge_id, factor=body.factor, value=body.value,
                    user_id=user_id)


@api.get("/accounts")
def accounts(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)):
    rows = db.scalars(select(Account).where(Account.user_id == user_id)).all()
    return [{"id": a.id, "name": a.name, "tail": a.card_tail, "balance": fmt_yuan(a.balance_fen)} for a in rows]


@api.get("/health")
def health():
    s = get_settings()
    return {"ok": True, "llm": {"ready": s.llm_ready, "provider": s.llm_provider, "model": s.llm_model,
                                "base_url": s.llm_base_url},
            "demo_mode": s.demo_mode}


# ---------- 运营后台 ----------

@admin.get("/audit")
def audit_list(trace_id: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))
    if trace_id:
        q = select(AuditLog).where(AuditLog.trace_id == trace_id).order_by(AuditLog.id)
    return [{"id": x.id, "trace_id": x.trace_id, "session_id": x.session_id, "event": x.event_type,
             "payload": x.payload, "hash": x.hash[:16], "at": x.created_at.isoformat()} for x in db.scalars(q)]


@admin.post("/audit/verify")
def audit_verify(db: Session = Depends(get_db)):
    return audit.verify(db)


@admin.get("/ledger/check")
def ledger_check(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)):
    accts = db.scalars(select(Account).where(Account.user_id == user_id)).all()
    return {"balanced": is_balanced(db),
            "accounts": [{"id": a.id, "balance_fen": a.balance_fen, "ledger_fen": ledger_balance(db, a.id)} for a in accts]}


class KillSwitchIn(BaseModel):
    enabled: bool
    reason: str = ""


@admin.post("/kill-switch")
def kill_switch(body: KillSwitchIn, db: Session = Depends(get_db)):
    set_flag(db, "kill_switch", {"enabled": body.enabled, "reason": body.reason})
    audit.record(db, "admin", "kill_switch", {"enabled": body.enabled, "reason": body.reason})
    db.commit()
    return {"enabled": kill_switch_on(db)}


@admin.get("/actions")
def action_list(db: Session = Depends(get_db)):
    rows = db.scalars(select(PendingAction).order_by(PendingAction.created_at.desc()).limit(50)).all()
    return [{"id": a.id, "level": a.level, "status": a.status, "display": a.display, "reasons": a.reasons,
             "trace_id": a.trace_id, "created_at": a.created_at.isoformat()} for a in rows]


# 按 trace 回放：一轮对话从用户消息到执行结果的全部审计事件都带同一个 trace_id（确认、验证沿用出卡时的 trace）
_OUTCOME_EVENTS = {"execution": None, "execution_failed": "failed", "action_cancelled": "cancelled",
                   "action_expired": "expired", "action_invalidated": "invalidated", "breaker_blocked": "blocked",
                   "auth_challenge": "awaiting_auth", "action_created": "pending"}


def _trace_summary(trace_id: str, events: list[AuditLog]) -> dict:
    first = next((e for e in events if e.event_type == "user_message"), None)
    level, outcome, flags = None, "replied", []
    for e in events:
        p = e.payload
        if e.event_type in ("action_created", "policy_decision") and p.get("level"):
            level = p["level"]
        if e.event_type == "policy_decision" and p.get("block"):
            outcome = "blocked"
        if e.event_type in _OUTCOME_EVENTS:
            outcome = _OUTCOME_EVENTS[e.event_type] or p.get("status", "succeeded")
        if e.event_type == "injection_suspected":
            flags = p.get("flags", [])
    return {"trace_id": trace_id, "session_id": events[0].session_id, "text": first.payload.get("text") if first else None,
            "started_at": events[0].created_at.isoformat(), "ended_at": events[-1].created_at.isoformat(),
            "events": len(events), "level": level, "outcome": outcome, "flags": flags,
            "breaker": any(e.event_type in ("breaker_tripped", "breaker_blocked") for e in events)}


@admin.get("/traces")
def trace_list(limit: int = 20, db: Session = Depends(get_db)):
    """最近的对话轮次（不含运营操作），每条带用户原话、风险等级和最终结果，供运营后台选择回放。"""
    rows = db.execute(select(AuditLog.trace_id).where(AuditLog.trace_id.not_in(("admin", "system")))
                      .group_by(AuditLog.trace_id).order_by(func.max(AuditLog.id).desc()).limit(min(limit, 100))).all()
    out = []
    for (trace_id,) in rows:
        events = db.scalars(select(AuditLog).where(AuditLog.trace_id == trace_id).order_by(AuditLog.id)).all()
        out.append(_trace_summary(trace_id, list(events)))
    return out


# ---------- 熔断（技术文档 §5.7） ----------

@admin.get("/risk-events")
def risk_events(limit: int = 50, db: Session = Depends(get_db)):
    """熔断处置记录、最近的安全事件、工具断路器状态和当前阈值。"""
    return breaker.overview(db, min(limit, 200))


class ReleaseIn(BaseModel):
    operator: str = Field(default="运营后台", max_length=32)
    note: str = Field(default="", max_length=200)


def _breaker_op(fn, db: Session, *args, **kwargs):
    try:
        out = fn(db, *args, **kwargs)
    except breaker.BreakerError as e:
        raise HTTPException(e.status_code, str(e)) from e
    db.commit()
    return out


@admin.post("/breaker/trips/{trip_id}/release")
def breaker_release(trip_id: str, body: ReleaseIn, db: Session = Depends(get_db)):
    return breaker.trip_view(_breaker_op(breaker.release, db, trip_id, operator=body.operator, note=body.note))


@admin.post("/breaker/circuits/{tool}/reset")
def circuit_reset(tool: str, body: ReleaseIn, db: Session = Depends(get_db)):
    _breaker_op(breaker.reset_circuit, db, tool, operator=body.operator)
    return {"tool": tool, "state": "closed"}


# ---------- 开发接口（仅演示模式） ----------

def _demo_only():
    if not get_settings().demo_mode:
        raise HTTPException(404)


@dev.post("/seed", dependencies=[Depends(_demo_only)])
def reset_seed():
    clock.reset()
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with new_session() as s:
        seed(s)
    return {"ok": True}


class TravelIn(BaseModel):
    days: int = 0
    hours: int = 0


@dev.post("/time-travel", dependencies=[Depends(_demo_only)])
def time_travel(body: TravelIn):
    return {"now": clock.travel(timedelta(days=body.days, hours=body.hours)).isoformat()}


@dev.post("/aa/shares/{share_id}/pay", dependencies=[Depends(_demo_only)])
def aa_share_pay(share_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)):
    """模拟 AA 参与人付款（对方银行入账），收款状态从「已发送」变为「已付清」。"""
    from ..tools.transfer import aa_mark_paid  # 转账补全（WS1）

    try:
        card = aa_mark_paid(db, user_id=user_id, share_id=share_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    db.commit()
    return {"card": card}
