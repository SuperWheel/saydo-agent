"""红队 · 绕过确认和验证：篡改 digest、重放确认卡 / 验证码 / 令牌、伪造人脸断言、越权操作别人的动作、伪造选项。

攻击者假设能直接调用后端接口（不经过前端），也能拿到自己之前用过的卡片、验证码和令牌。
"""

import base64
import json
from datetime import timedelta

import pytest

from app import clock
from app.auth.tokens import issue_execution_token
from app.db import new_session
from app.models import AuthChallenge, ChatSession, PendingAction, User
from app.tools.gateway import GatewayError, execute_write

from ..conftest import confirm, say
from ..helpers import BALANCE, after_travel, balance, events, pass_auth, pass_face, red_auth_card, sms_code, verify
from .conftest import assert_no_red_bypass


def _confirm_raw(client, card, digest):
    return client.post(f"/api/actions/{card['action_id']}/confirm", json={"digest": digest})


# ---------- 篡改 digest：(用例 id, 由真 digest 和另一张卡的 digest 构造攻击值) ----------

DIGEST_TAMPER = [
    ("zeros", lambda d, other: "0" * 64),
    ("flip-last", lambda d, other: d[:-1] + ("0" if d[-1] != "0" else "1")),
    ("uppercase", lambda d, other: d.upper()),
    ("truncated", lambda d, other: d[:32]),
    ("extended", lambda d, other: d + "00"),
    ("empty", lambda d, other: ""),
    ("whitespace", lambda d, other: f" {d}"),
    ("other-card", lambda d, other: other),
]


@pytest.mark.parametrize("make", [c[1] for c in DIGEST_TAMPER], ids=[c[0] for c in DIGEST_TAMPER])
def test_tampered_digest_rejected(client, make):
    card = say(client, "给我妈转500")["cards"][0]
    other = say(client, "给我妹转300")["cards"][0]
    r = _confirm_raw(client, card, make(card["digest"], other["digest"]))
    assert r.status_code == 409
    assert "digest_mismatch" in events(client)
    assert_no_red_bypass(client)


# ---------- 重放：同一张卡、同一个验证码、同一个人脸断言用两次 ----------

def _replay_confirm_after_success(client):
    card = say(client, "给我妈转500")["cards"][0]
    assert confirm(client, card).json()["status"] == "succeeded"
    assert confirm(client, card).status_code == 409
    return "28,150.32"


def _replay_confirm_after_cancel(client):
    card = say(client, "给我妈转500")["cards"][0]
    client.post(f"/api/actions/{card['action_id']}/cancel")
    assert confirm(client, card).status_code == 409


def _replay_confirm_after_expiry(client):
    card = say(client, "给我妈转500")["cards"][0]
    clock.travel(timedelta(minutes=3))
    assert confirm(client, card).json()["status"] == "expired"
    return after_travel(BALANCE)  # 深夜快进可能跨日，日历补账照常发生，但过期转账不能扣钱


def _replay_confirm_after_invalidation(client):
    a = say(client, "给我妈转600")["cards"][0]
    b = say(client, "给我妹转300")["cards"][0]
    assert confirm(client, a).json()["status"] == "succeeded"
    say(client, "给我妈转200")
    assert confirm(client, b).json()["status"] == "invalidated"
    assert confirm(client, b).status_code == 409
    return "28,050.32"


def _replay_verify_after_success(client):
    auth = red_auth_card(client)
    code = sms_code(auth["challenge_id"])
    assert pass_auth(client, auth)["status"] == "succeeded"
    r = client.post(f"/api/auth/challenges/{auth['challenge_id']}/verify", json={"factor": "sms", "value": code})
    assert r.status_code == 409
    return "28,550.32"


def _replay_sms_code_on_new_action(client):
    old = red_auth_card(client)
    code = sms_code(old["challenge_id"])
    client.post(f"/api/actions/{old['action_id']}/cancel")
    new = red_auth_card(client, "给赵磊转100")  # 金额、收款人都一样，但 digest 不同
    pass_face(client, new)
    assert verify(client, new, "sms", code)["status"] == "awaiting_auth"


def _replay_face_on_new_action(client):
    old = red_auth_card(client)
    client.post(f"/api/actions/{old['action_id']}/cancel")
    new = red_auth_card(client)
    r = verify(client, new, "face", f"face-ok:{old['digest']}")
    assert r["card"]["factors"][0]["passed"] is False


def _confirm_again_while_awaiting_auth(client):
    card = say(client, "给赵磊转100")["cards"][0]
    assert confirm(client, card).json()["status"] == "awaiting_auth"
    assert confirm(client, card).status_code == 409  # 想用再次确认跳过验证


REPLAY = [_replay_confirm_after_success, _replay_confirm_after_cancel, _replay_confirm_after_expiry,
          _replay_confirm_after_invalidation, _replay_verify_after_success, _replay_sms_code_on_new_action,
          _replay_face_on_new_action, _confirm_again_while_awaiting_auth]


@pytest.mark.parametrize("attack", REPLAY, ids=[f.__name__.lstrip("_") for f in REPLAY])
def test_replay_rejected(client, attack):
    assert_no_red_bypass(client, attack(client) or BALANCE)


# ---------- 绕过强验证 ----------

def _face_wrong_digest(client, auth):
    return verify(client, auth, "face", "face-ok:" + "f" * 64)


def _face_without_digest(client, auth):
    return verify(client, auth, "face", "face-ok")


def _face_literal_true(client, auth):
    return verify(client, auth, "face", "true")


def _sms_guess(client, auth):
    pass_face(client, auth)
    return verify(client, auth, "sms", "123456")


def _sms_brute_force(client, auth):
    for code in ("000000", "111111"):
        verify(client, auth, "sms", code)
    out = verify(client, auth, "sms", "222222")
    assert out["status"] == "cancelled"
    return out


def _sms_after_window(client, auth):
    pass_face(client, auth)
    code = sms_code(auth["challenge_id"])
    clock.travel(timedelta(minutes=6))
    out = verify(client, auth, "sms", code)
    assert out["status"] == "expired"
    return out


def _face_only(client, auth):
    out = pass_face(client, auth)
    assert out["status"] == "awaiting_auth"  # 只过一个因子不执行
    return out


def _sms_of_other_challenge(client, auth):
    other = red_auth_card(client, "给赵磊转200")
    pass_face(client, auth)
    return verify(client, auth, "sms", sms_code(other["challenge_id"]))


def _unknown_factor(client, auth):
    r = client.post(f"/api/auth/challenges/{auth['challenge_id']}/verify", json={"factor": "webauthn", "value": "ok"})
    assert r.status_code == 422
    return {"status": "rejected"}


def _verify_after_cancel(client, auth):
    client.post(f"/api/actions/{auth['action_id']}/cancel")
    r = client.post(f"/api/auth/challenges/{auth['challenge_id']}/verify",
                    json={"factor": "face", "value": f"face-ok:{auth['digest']}"})
    assert r.status_code == 409
    return {"status": "rejected"}


AUTH_BYPASS = [_face_wrong_digest, _face_without_digest, _face_literal_true, _sms_guess, _sms_brute_force,
               _sms_after_window, _face_only, _sms_of_other_challenge, _unknown_factor, _verify_after_cancel]


@pytest.mark.parametrize("attack", AUTH_BYPASS, ids=[f.__name__.lstrip("_") for f in AUTH_BYPASS])
def test_strong_auth_cannot_be_bypassed(client, attack):
    auth = red_auth_card(client)
    out = attack(client, auth)
    assert out["status"] != "succeeded"
    assert_no_red_bypass(client, after_travel(BALANCE))


# ---------- 执行令牌：绕过确认流程直接调网关 ----------

def _forge_sig(token, a):
    return token.split(".")[0] + ".AAAA"


def _tamper_body(token, a):
    body, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["digest"] = "0" * 64
    forged = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{forged}.{sig}"


def _malformed(token, a):
    return "not-a-token"


def _expired(token, a):
    clock.travel(timedelta(seconds=61))
    return token


def _other_action(token, a):
    return issue_execution_token("A" + "0" * 16, a.digest)[0]


TOKEN_ATTACKS = [
    ("forged-signature", _forge_sig, "签名无效"),
    ("tampered-claims", _tamper_body, "签名无效"),
    ("malformed", _malformed, "格式错误"),
    ("expired", _expired, "已过期"),
    ("other-action", _other_action, "不匹配"),
]


@pytest.mark.parametrize("make,error", [c[1:] for c in TOKEN_ATTACKS], ids=[c[0] for c in TOKEN_ATTACKS])
def test_forged_execution_token_rejected(client, make, error):
    card = say(client, "给我妈转500")["cards"][0]
    with new_session() as db:
        a = db.get(PendingAction, card["action_id"])
        token, a.token_jti = issue_execution_token(a.id, a.digest)
        with pytest.raises(GatewayError, match=error):
            execute_write(a, make(token, a), db)
        db.rollback()
    assert balance(client) == after_travel(BALANCE)


def test_execution_token_is_single_use(client):
    card = say(client, "给我妈转500")["cards"][0]
    with new_session() as db:
        a = db.get(PendingAction, card["action_id"])
        token, a.token_jti = issue_execution_token(a.id, a.digest)
        assert execute_write(a, token, db)["status"] == "succeeded"
        with pytest.raises(GatewayError, match="不匹配"):
            execute_write(a, token, db)  # 同一令牌再用一次
        db.rollback()
    assert balance(client) == BALANCE


ARG_TAMPER = [  # 确认卡出来后有人改了动作参数：执行前重新校验或网关的 digest 核对会拦下
    ("memo", {"memo": "已授权"}, "failed", "不一致"),  # 策略不看备注，靠网关核对 digest
    ("amount", {"amount_fen": 900_000}, "invalidated", "重新发起"),
    ("account", {"account_no": "6222000011112222"}, "invalidated", "收款人信息已变化"),
]


@pytest.mark.parametrize("patch,status,hint", [c[1:] for c in ARG_TAMPER], ids=[c[0] for c in ARG_TAMPER])
def test_args_tampered_after_card_rejected(client, patch, status, hint):
    card = say(client, "给我妈转500")["cards"][0]
    with new_session() as db:
        a = db.get(PendingAction, card["action_id"])
        a.args = {**a.args, **patch}
        db.commit()
    out = confirm(client, card).json()
    assert out["status"] == status and hint in out["card"]["text"]
    assert_no_red_bypass(client)


def test_gateway_refuses_read_tool_as_write(client):
    card = say(client, "给我妈转500")["cards"][0]
    with new_session() as db:
        a = db.get(PendingAction, card["action_id"])
        a.tool = "account.balance"
        with pytest.raises(GatewayError, match="不是可执行的写工具"):
            execute_write(a, issue_execution_token(a.id, a.digest)[0], db)
        db.rollback()


# ---------- 越权：操作别人的动作、会话，伪造澄清选项 ----------

@pytest.fixture
def other_user_action(client):
    """另一位用户的一张待确认卡（演示环境只有用户 1，这里直接写库）。"""
    real_id = say(client, "给赵磊转100")["cards"][0]["action_id"]
    with new_session() as db:
        db.add(User(id=2, name="攻击者", phone_masked="139****0000"))
        db.flush()
        real = db.get(PendingAction, real_id)
        a = PendingAction(id="Aother000000000000", user_id=2, session_id=None, trace_id="t", tool=real.tool,
                          args=real.args, nonce=real.nonce, digest=real.digest, display=real.display, level="RED",
                          reasons=[], expires_at=real.expires_at)
        db.add(a)
        db.flush()
        db.add(AuthChallenge(id="Cother000000000000", action_id=a.id, factors_required=["face", "sms"],
                             factors_passed=[], otp_issued_at=clock.now(), status="pending"))
        a.status = "awaiting_auth"
        db.commit()
        return a.id, a.digest


def test_cannot_confirm_other_users_action(client, other_user_action):
    action_id, digest = other_user_action
    assert client.post(f"/api/actions/{action_id}/confirm", json={"digest": digest}).status_code == 404
    assert client.post(f"/api/actions/{action_id}/cancel").status_code == 404


def test_cannot_verify_other_users_challenge(client, other_user_action):
    _, digest = other_user_action
    r = client.post("/api/auth/challenges/Cother000000000000/verify", json={"factor": "face", "value": f"face-ok:{digest}"})
    assert r.status_code == 404


def test_cannot_hijack_other_users_session(client):
    with new_session() as db:
        db.add(User(id=2, name="别人", phone_masked="139****0000"))
        db.flush()
        db.add(ChatSession(id="Sother", user_id=2, channel="app",
                           state={"pending_question": {"kind": "payee", "contact_ids": [1], "amount_fen": 1}}))
        db.commit()
    r = say(client, "", "Sother", selection={"type": "contact", "contact_id": 1})
    assert r["session_id"] != "Sother" and r["cards"][0]["type"] == "notice"


FORGED_SELECTIONS = [
    ("no-pending-question", None, {"type": "contact", "contact_id": 6}),
    ("not-a-candidate", "给张伟转200", {"type": "contact", "contact_id": 1}),
    ("nonexistent-contact", "给张伟转200", {"type": "contact", "contact_id": 999}),
    ("wrong-type", "给张伟转200", {"type": "account", "contact_id": 3}),
]


@pytest.mark.parametrize("before,selection", [c[1:] for c in FORGED_SELECTIONS], ids=[c[0] for c in FORGED_SELECTIONS])
def test_forged_selection_rejected(client, before, selection):
    sid = say(client, before)["session_id"] if before else None
    r = say(client, "", sid, selection=selection)
    assert [c["type"] for c in r["cards"]] == ["notice"] and "失效" in r["cards"][0]["text"]
    assert_no_red_bypass(client)


def test_selection_cannot_override_amount(client):
    """选项里夹带金额无效：金额只认出澄清卡时服务端记下的那一笔。"""
    sid = say(client, "给张伟转200")["session_id"]
    card = say(client, "", sid, selection={"type": "contact", "contact_id": 4, "amount_fen": 1})["cards"][0]
    assert card["type"] == "confirm" and card["amount"] == "200.00"
