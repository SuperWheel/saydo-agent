"""支付协议拦截、用户选择与非交互执行路径。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import clock
from app.auth.actions import PolicyEnv, decide
from app.config import Settings, get_settings
from app.db import new_session
from app.models import PaymentConsent, PendingAction
from app.payments import AGREEMENT_VERSION, agreement_valid

from .conftest import confirm, enter, say
from .helpers import balance, events, pass_auth


@pytest.fixture
def unsigned(client, monkeypatch):
    monkeypatch.setenv("PAYMENT_AGREEMENT_PRESIGNED", "false")
    get_settings.cache_clear()
    assert client.post("/dev/seed").status_code == 200
    enter(client)
    return client


def choose(client, response, action="sign", **extra):
    card = response["cards"][0]
    return say(client, "同意《支付服务协议》" if action == "sign" else "暂不同意", response["session_id"],
               selection={"type": "payment_agreement", "action": action, "version": AGREEMENT_VERSION,
                          "action_id": card["action_id"], **extra})


def test_default_unsigned_and_presigned_fixture(client, monkeypatch):
    monkeypatch.delenv("PAYMENT_AGREEMENT_PRESIGNED")
    assert Settings(_env_file=None).payment_agreement_presigned is False
    with new_session() as db:
        assert agreement_valid(db, 1)


def test_agreement_sign_resume_and_execute(unsigned):
    response = say(unsigned, "给我妈转500")
    card = response["cards"][0]
    assert card["type"] == "payment_agreement" and card["status"] == "unsigned"
    assert card["version"] == AGREEMENT_VERSION and "500.00" in card["pending_summary"]
    assert 4 <= len(card["points"]) <= 6 and len(card["sections"]) >= 4
    with new_session() as db:
        action = db.get(PendingAction, card["action_id"])
        old_expiry = action.expires_at
        assert action.status == "pending" and action.reserved_fen == 50_000
        assert not agreement_valid(db, 1)
    clock.travel(timedelta(seconds=90))
    signed = choose(unsigned, response)
    confirmation = signed["cards"][0]
    assert confirmation["type"] == "confirm" and confirmation["action_id"] == card["action_id"]
    assert signed["reply"]["text"] == "已记录您的同意，请核对这笔交易。"
    with new_session() as db:
        assert db.get(PendingAction, card["action_id"]).expires_at > old_expiry
        assert agreement_valid(db, 1)
    assert confirm(unsigned, confirmation).json()["status"] == "succeeded"
    assert "payment_agreement_signed" in events(unsigned)
    assert unsigned.post("/admin/audit/verify").json()["ok"]


def test_decline_cancels_and_releases(unsigned):
    response = say(unsigned, "给我妈转500")
    assert choose(unsigned, response, "decline")["cards"][0]["type"] == "notice"
    with new_session() as db:
        action = db.get(PendingAction, response["cards"][0]["action_id"])
        assert action.status == "cancelled" and action.reserved_fen == 0
        assert not agreement_valid(db, 1)
    assert "payment_agreement_declined" in events(unsigned)
    assert balance(unsigned) == "28,650.32"


def test_confirm_api_cannot_skip_agreement(unsigned):
    response = say(unsigned, "给我妈转500")
    with new_session() as db:
        action = db.get(PendingAction, response["cards"][0]["action_id"])
        forged = {"action_id": action.id, "digest": action.digest}
    rejected = confirm(unsigned, forged)
    assert rejected.status_code == 409 and "支付服务协议" in rejected.json()["detail"]
    assert balance(unsigned) == "28,650.32"


@pytest.mark.parametrize("sentence", ["存500到心愿罐", "每月15号给我妈转500"])
def test_other_covered_proposals(unsigned, sentence):
    response = say(unsigned, sentence)
    assert response["cards"][0]["type"] == "payment_agreement", response
    assert choose(unsigned, response)["cards"][0]["type"] == "confirm"


def test_order_requires_agreement(unsigned):
    response = say(unsigned, "给妈妈订花")
    picked = say(unsigned, "要第一个", response["session_id"])
    assert picked["cards"][0]["type"] == "payment_agreement"
    assert choose(unsigned, picked)["cards"][0]["type"] == "confirm"


def test_unsigned_scheduled_execution_blocked(unsigned):
    from app.tools.automation import run_authorized
    from app.policy import Risk

    response = say(unsigned, "给我妈转500")
    with new_session() as db:
        action = db.get(PendingAction, response["cards"][0]["action_id"])
        decision = decide(db, action.tool, dict(action.args), PolicyEnv(1, "schedule"))
        assert decision.block and "支付服务协议" in decision.block_reason
        status, card = run_authorized(db, user_id=1, session_id=action.session_id, trace_id="test-scheduled",
                                     tool=action.tool, args=dict(action.args), display=action.display,
                                     authorized=Risk.YELLOW, label="定时转账")
        assert status == "needs_user" and "支付服务协议" in card["text"]
        db.commit()
    assert balance(unsigned) == "28,650.32"


@pytest.mark.parametrize("change", ["version", "session", "cancelled"])
def test_invalid_sign_does_not_create_consent(unsigned, change):
    response = say(unsigned, "给我妈转500")
    if change == "session":
        response["session_id"] = say(unsigned, "你好")["session_id"]
    if change == "cancelled":
        choose(unsigned, response, "decline")
    result = choose(unsigned, response, **({"version": "2020.01"} if change == "version" else {}))
    assert result["cards"][0]["type"] == "notice"
    with new_session() as db:
        assert not agreement_valid(db, 1)


def test_revoke_agreement_during_strong_auth(client):
    card = say(client, "给房东转500")["cards"][0]
    auth = confirm(client, card).json()["card"]
    with new_session() as db:
        db.get(PaymentConsent, 1).agreement_revoked_at = clock.now()
        db.commit()
    assert pass_auth(client, auth)["status"] == "invalidated"
    assert balance(client) == "28,650.32"


def test_view_agreement(client):
    card = say(client, "查看支付服务协议", selection={"type": "payment_agreement", "action": "view"})["cards"][0]
    assert card["status"] == "signed" and card["signed_at"] and card["action_id"] is None


def test_view_unsigned_and_sign_without_action(unsigned):
    response = say(unsigned, "查看《支付服务协议》", selection={"type": "payment_agreement", "action": "view"})
    card = response["cards"][0]
    assert card["status"] == "unsigned" and card["action_id"] is None
    signed = say(unsigned, "只是按钮展示文字", response["session_id"], selection={
        "type": "payment_agreement", "action": "sign", "version": AGREEMENT_VERSION, "action_id": None})
    assert signed["cards"][0]["type"] == "payment_agreement" and signed["cards"][0]["status"] == "signed"
    assert signed["cards"][0]["action_id"] is None and signed["cards"][0]["signed_at"]
    assert signed["reply"]["text"] == "已签署《支付服务协议》。"
    with new_session() as db:
        assert agreement_valid(db, 1) and db.scalar(select(PendingAction.id)) is None
    audit = [event for event in unsigned.get("/admin/audit").json() if event["event"] == "payment_agreement_signed"]
    assert audit[0]["payload"]["version"] == AGREEMENT_VERSION and audit[0]["payload"]["action_id"] is None


def test_decline_without_action(unsigned):
    response = say(unsigned, "暂不同意《支付服务协议》", selection={
        "type": "payment_agreement", "action": "decline", "action_id": None})
    assert "暂未同意" in response["reply"]["text"]
    with new_session() as db:
        assert not agreement_valid(db, 1) and db.scalar(select(PendingAction.id)) is None
    audit = [event for event in unsigned.get("/admin/audit").json() if event["event"] == "payment_agreement_declined"]
    assert audit[0]["payload"]["version"] == AGREEMENT_VERSION


def test_expired_action_cannot_be_revived_by_signing(unsigned):
    response = say(unsigned, "给我妈转500")
    clock.travel(timedelta(seconds=121))
    assert choose(unsigned, response)["cards"][0]["type"] == "notice"
    with new_session() as db:
        action = db.get(PendingAction, response["cards"][0]["action_id"])
        assert action.status == "expired" and action.reserved_fen == 0
        assert not agreement_valid(db, 1)


def test_first_success_invites_and_dismiss_suppresses(client):
    response = say(client, "给我妈转100")
    out = confirm(client, response["cards"][0]).json()
    assert out["status"] == "succeeded" and out["extra_cards"][0]["mode"] == "invite"
    dismissed = say(client, "暂不开通", response["session_id"], selection={"type": "mandate_offer", "action": "dismiss"})
    assert dismissed["cards"][0]["type"] == "notice"
    second = say(client, "给我妹转100")["cards"][0]
    assert not confirm(client, second).json().get("extra_cards")
    assert "mandate_offer_dismissed" in events(client)
    clock.travel(timedelta(days=30, seconds=1))
    enter(client)
    third = say(client, "给我妈转100")["cards"][0]
    assert confirm(client, third).json()["extra_cards"][0]["mode"] == "invite"


def test_verified_success_also_returns_invite(client):
    card = say(client, "给房东转100")["cards"][0]
    auth = confirm(client, card).json()["card"]
    assert pass_auth(client, auth)["extra_cards"][0]["mode"] == "invite"
