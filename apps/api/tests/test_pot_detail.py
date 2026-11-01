"""心愿罐按分录读取收支、接口凭证与归属校验，以及内部划转额度隔离。"""

import pytest
from sqlalchemy import select

from app.clock import now
from app.db import new_session
from app.models import Account, LimitCounter, PendingAction, User, WishPot
from app.nlu import rule_intent
from app.payments import used_today
from app.tools import REGISTRY

from .conftest import confirm, say
from .helpers import ledger_ok, order, pass_auth
from .test_mandate import enable


def deposit(client, amount=500, purpose="心愿"):
    response = say(client, f"存{amount}进心愿罐，用途{purpose}")
    card = response["cards"][0]
    assert card["type"] == "confirm" and card["level"] == "RED", response
    result = confirm(client, card).json()
    assert result["status"] == "awaiting_auth" and result["card"]["mode"] == "any"
    result = pass_auth(client, result["card"])
    assert result["status"] == "succeeded", result
    with new_session() as db:
        pot_id = db.get(PendingAction, card["action_id"]).result["pot_id"]
    return response, pot_id


@pytest.mark.parametrize("sentence", ["心愿罐明细", "心愿罐收支", "心愿罐记录", "我的心愿罐明细", "心愿罐里的钱都花哪了"])
def test_detail_dialogue_and_empty_state(client, sentence):
    assert rule_intent(sentence).intent == "pot"
    assert rule_intent(sentence).slots == {"op": "detail"}
    assert say(client, sentence)["cards"][0]["type"] == "notice"
    _, pot_id = deposit(client)
    card = say(client, sentence)["cards"][0]
    assert card["type"] == "pot_detail"
    response = client.get(f"/api/pots/{pot_id}/txns")
    assert response.status_code == 200
    assert {key: value for key, value in card.items() if key != "type"} == response.json()
    assert card["pot"]["balance"] == "500.00" and card["pot"]["balance_fen"] == 50_000
    assert card["items"] == [{"at": "2026-10-30 12:00", "kind": "deposit", "label": "从活期存入",
                              "amount": "+500.00", "amount_fen": 50_000}]


def test_deposit_pay_refund_release_and_descending_order(client):
    r, pot_id = deposit(client)
    card = order(client, "帮我订一束花送我妈", r["session_id"])["cards"][0]
    assert confirm(client, card).json()["status"] == "succeeded"
    card = say(client, "把花的订单取消", r["session_id"])["cards"][0]
    assert confirm(client, card).json()["status"] == "succeeded"
    card = say(client, "释放心愿罐")["cards"][0]
    assert confirm(client, card).json()["status"] == "succeeded"
    detail = client.get(f"/api/pots/{pot_id}/txns").json()
    assert [item["kind"] for item in detail["items"]] == ["release", "refund", "pay", "deposit"]
    assert [item["amount_fen"] for item in detail["items"]] == [-50_000, 19_900, -19_900, 50_000]
    assert [item["amount"] for item in detail["items"]] == ["-500.00", "+199.00", "-199.00", "+500.00"]
    assert detail["pot"]["balance_fen"] == 0
    assert sum(item["amount_fen"] for item in detail["items"]) == 0
    assert ledger_ok(client)
    assert say(client, "心愿罐明细")["cards"][0]["items"] == detail["items"]


def test_multiple_pots_clarify_selection_and_isolation(client):
    _, first_id = deposit(client)
    # 两个用途不同的罐，验证选择回传中的 pot_id 和接口读取同一份数据。
    with new_session() as db:
        acct = Account(user_id=1, kind="pot", name="心愿罐·旅行", card_tail="", balance_fen=0)
        db.add(acct)
        db.flush()
        db.add(WishPot(id="P-travel", user_id=1, account_id=acct.id, purpose="旅行"))
        db.commit()
    r = say(client, "心愿罐明细")
    card = r["cards"][0]
    assert card["type"] == "clarify" and len(card["options"]) == 2
    for option in card["options"]:
        selected = say(client, "选择心愿罐", r["session_id"], selection=option["selection"])["cards"][0]
        assert selected["type"] == "pot_detail"
        assert selected["pot"]["pot_id"] == option["selection"]["pot_id"]
        assert len(selected["items"]) == (1 if selected["pot"]["pot_id"] == first_id else 0)
    assert say(client, "旅行心愿罐明细")["cards"][0]["pot"]["pot_id"] == "P-travel"


def test_requires_entry_and_owns_pot_for_http_and_selection(client):
    _, pot_id = deposit(client)
    token = client.headers.pop("X-Entry-Token")
    assert client.get(f"/api/pots/{pot_id}/txns").status_code == 401
    client.headers["X-Entry-Token"] = token
    with new_session() as db:
        db.add(User(id=2, name="他人", phone_masked="138****1111"))
        db.flush()
        acct = Account(user_id=2, kind="pot", name="别人的罐", card_tail="", balance_fen=0)
        db.add(acct)
        db.flush()
        db.add(WishPot(id="P-other", user_id=2, account_id=acct.id, purpose="他人的罐"))
        db.commit()
    for key in ("P-other", "missing"):
        assert client.get(f"/api/pots/{key}/txns").status_code == 404
        assert say(client, "选择", selection={"type": "pot_detail", "pot_id": key})["cards"][0]["type"] == "notice"


def test_pot_lock_red_release_yellow_no_outflow_or_nopwd_usage(client):
    enable(client)
    transfer = say(client, "给我妈转100")["cards"][0]
    assert confirm(client, transfer).json()["status"] == "succeeded"
    with new_session() as db:
        before = db.get(LimitCounter, (1, now().date()))
        counts = (before.used_fen, before.reserved_fen)
        assert used_today(db, 1) == 10_000
    _, pot_id = deposit(client, 1500)
    release = say(client, "释放心愿罐")["cards"][0]
    assert release["level"] == "YELLOW" and "nopwd" not in release
    assert confirm(client, release).json()["status"] == "succeeded"
    for name in ("pot.lock", "pot.release"):
        assert REGISTRY[name].base_risk.name == ("RED" if name == "pot.lock" else "YELLOW")
        assert not REGISTRY[name].moves_money
    with new_session() as db:
        after = db.get(LimitCounter, (1, now().date()))
        assert (after.used_fen, after.reserved_fen) == counts
        assert used_today(db, 1) == 10_000
        for action in db.scalars(select(PendingAction).where(PendingAction.tool.in_(("pot.lock", "pot.release")))):
            assert "nopwd" not in action.args and action.reserved_fen == 0
    assert ledger_ok(client)
