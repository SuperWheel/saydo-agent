"""审计哈希链。只追加：hash = SHA256(prev_hash ‖ 规范化事件)，任何一条被改，后面的链都会断开。"""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import now
from ..models import AuditLog

GENESIS = "0" * 64


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(prev_hash: str, body: dict) -> str:
    return hashlib.sha256((prev_hash + canonical(body)).encode()).hexdigest()


def _body(trace_id: str, session_id: str | None, event_type: str, payload: dict, created_at) -> dict:
    return {
        "trace_id": trace_id, "session_id": session_id, "event_type": event_type,
        "payload": payload, "created_at": created_at.isoformat(),
    }


def record(db: Session, trace_id: str, event_type: str, payload: dict, session_id: str | None = None) -> AuditLog:
    """写一条审计事件。和业务操作在同一个事务里，要么一起成功，要么一起回滚。"""
    prev = db.scalar(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)) or GENESIS
    at = now()
    payload = json.loads(canonical(payload))  # 先规范化，保证存进去的就是参与哈希的内容
    log = AuditLog(
        trace_id=trace_id, session_id=session_id, event_type=event_type, payload=payload,
        prev_hash=prev, hash=_digest(prev, _body(trace_id, session_id, event_type, payload, at)), created_at=at,
    )
    db.add(log)
    db.flush()
    return log


def verify(db: Session) -> dict:
    prev, count = GENESIS, 0
    for log in db.scalars(select(AuditLog).order_by(AuditLog.id)):
        expected = _digest(prev, _body(log.trace_id, log.session_id, log.event_type, log.payload, log.created_at))
        if log.prev_hash != prev or log.hash != expected:
            return {"ok": False, "count": count, "broken_at": log.id}
        prev, count = log.hash, count + 1
    return {"ok": True, "count": count, "broken_at": None}
