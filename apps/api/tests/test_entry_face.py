"""进入前人脸核身（auth/entry.py）：签发、过期、未核身被拒、重置后作废、审计与回放、配置开关、IM 渠道不受影响。规则模式。"""

import time

import pytest
from sqlalchemy import select

from app.auth import entry
from app.config import get_settings
from app.db import new_session
from app.models import AuditLog

from .conftest import enter, say


def bare(client):
    """去掉夹具里带的进入凭证，模拟刚打开 App、还没过人脸。"""
    client.headers.pop("X-Entry-Token", None)
    return client


def test_issue_returns_short_lived_token_and_audits(client):
    out = enter(client)
    assert out["ttl_s"] == 1800 and out["token"].count(".") == 1
    with new_session() as db:
        log = db.scalars(select(AuditLog).where(AuditLog.trace_id == out["trace_id"])).one()
    assert log.event_type == "entry_face_passed"
    assert log.payload == {"channel": "app", "factor": "face", "simulated": True, "ttl_s": 1800,
                           "jti": log.payload["jti"]}
    assert say(client, "我还有多少钱")["cards"][0]["type"] == "balance"


def test_chat_and_other_user_endpoints_rejected_without_entry(client):
    bare(client)
    for method, url, body in (("post", "/api/chat", {"text": "我还有多少钱"}), ("get", "/api/accounts", None),
                              ("get", "/api/handoff/abc", None), ("post", "/api/plans/sync", {}),
                              ("post", "/api/actions/A1/confirm", {"digest": "x"})):
        r = getattr(client, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code == 401, url
        assert r.headers["X-Entry-Required"] == "1" and "人脸" in r.json()["detail"]
    assert client.get("/api/health").json()["entry_face_required"] is True  # 健康检查不需要凭证
    client.headers["X-Entry-Token"] = "forged.sig"
    assert client.post("/api/chat", json={"text": "我还有多少钱"}).status_code == 401


def test_wrong_liveness_rejected_and_audited(client):
    r = bare(client).post("/api/auth/entry-face", json={"value": "face-ok:someone-else"})
    assert r.status_code == 400 and "未通过" in r.json()["detail"]
    with new_session() as db:
        assert db.scalars(select(AuditLog).where(AuditLog.event_type == "entry_face_failed")).one()
    assert client.post("/api/chat", json={"text": "我还有多少钱"}).status_code == 401


def test_token_expires_by_wall_clock_not_demo_clock(client, monkeypatch):
    client.post("/dev/fast-forward", json={"days": 3})  # 演示时间快进不影响进入凭证
    assert client.get("/api/accounts").status_code == 200
    later = time.time() + 1801
    monkeypatch.setattr(entry.time, "time", lambda: later)
    r = client.get("/api/accounts")
    assert r.status_code == 401 and "过期" in r.json()["detail"]
    enter(client)  # 重新过人脸就能继续
    assert client.get("/api/accounts").status_code == 200


def test_token_bound_to_user(client):
    with new_session() as db:
        with pytest.raises(entry.EntryError):
            entry.check(db, client.headers["X-Entry-Token"], user_id=2)


def test_reset_demo_revokes_entry(client):
    assert client.post("/dev/seed").status_code == 200
    r = client.post("/api/chat", json={"text": "我还有多少钱"})
    assert r.status_code == 401 and "重置" in r.json()["detail"]
    enter(client)
    assert say(client, "我还有多少钱")["cards"]


def test_entry_trace_shows_in_replay(client):
    out = enter(client)
    traces = client.get("/admin/traces").json()
    t = next(x for x in traces if x["trace_id"] == out["trace_id"])
    assert t["text"] == "进入前人脸核身" and t["outcome"] == "verified" and t["events"] == 1
    rows = client.get(f"/admin/audit?trace_id={out['trace_id']}").json()
    assert [x["event"] for x in rows] == ["entry_face_passed"]
    assert client.post("/admin/audit/verify").json()["ok"]


def test_can_be_disabled_by_config(client, monkeypatch):
    monkeypatch.setenv("ENTRY_FACE_REQUIRED", "false")
    get_settings.cache_clear()
    bare(client)
    assert client.get("/api/health").json()["entry_face_required"] is False
    assert say(client, "我还有多少钱")["cards"][0]["type"] == "balance"


def test_im_channel_not_gated_and_handoff_needs_entry(client):
    """企业微信走 im_turn，不经过 HTTP，不需要进入凭证；引导链接里的转交码要在 App 里过了人脸才取得到。"""
    from app.channels import im_turn

    with new_session() as db:
        r = im_turn(db, channel="wecom", external_id="zhangsan", text="给我妈转500", user_id=1)
        db.commit()
    assert r.downgraded and "?r=" in r.text
    code = r.text.split("?r=")[1].split()[0]
    token = client.headers["X-Entry-Token"]
    assert bare(client).get(f"/api/handoff/{code}").status_code == 401
    client.headers["X-Entry-Token"] = token
    assert client.get(f"/api/handoff/{code}").json()["text"] == "给我妈转500"
