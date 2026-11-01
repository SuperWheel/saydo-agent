"""心愿罐管理卡、部分转出与内部额度隔离。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.auth.actions import PolicyEnv, decide
from app.clock import now
from app.core_mock.ledger import post
from app.db import new_session
from app.models import Account, LimitCounter, PendingAction, TaskPlan, Transaction, User, WishPot
from app.nlu import rule_intent
from app.tools import REGISTRY
from app.tools.pot import withdraw

from .conftest import confirm, say, text, tool
from .helpers import factors, ledger_ok, pass_auth, pass_face


def deposit(client, amount_fen=50_000, purpose="旅行"):
    card = say(client, "存入", selection={"type": "pot_manage", "op": "deposit", "pot_id": None,
               "purpose": purpose, "amount_fen": amount_fen})["cards"][0]
    assert card["type"] == "confirm"
    with new_session() as db:
        assert db.get(PendingAction, card["action_id"]).tool == "pot.lock"
    result = confirm(client, card).json()
    if result["status"] == "awaiting_auth":
        assert result["card"]["mode"] == "any" and factors(result["card"]) == ["face", "sms"]
        result = pass_face(client, result["card"])
    assert result["status"] == "succeeded", result
    with new_session() as db:
        return db.get(PendingAction, card["action_id"]).result["pot_id"]


def withdraw_card(client, pot_id, amount=20_000):
    return say(client, "转出到活期", selection={"type": "pot_manage", "op": "withdraw",
               "pot_id": pot_id, "amount_fen": amount})["cards"][0]


@pytest.mark.parametrize("sentence", ["我的心愿罐", "心愿罐余额", "心愿罐还有多少", "心愿罐情况", "管理心愿罐"])
def test_manage_routes_and_empty_fields(client, sentence):
    assert rule_intent(sentence).slots == {"op": "manage"}
    card = say(client, sentence)["cards"][0]
    assert card["type"] == "pots" and card["title"] == "我的心愿罐"
    assert card["pots"] == [] and card["empty_text"] and card["verify_note"]
    assert set(card["debit"]) == {"account_id", "label", "balance", "balance_fen"}
    assert "6688" in card["debit"]["label"]


def test_fields_spent_net_of_refund_and_bound_plan(client):
    pot_id = deposit(client)
    with new_session() as db:
        pot = db.get(WishPot, pot_id)
        account = db.get(Account, pot.account_id)
        post(db, account=account, direction="out", amount_fen=10_000, counterparty="测试花店", contra_code="SHOP:test", category="购物")
        post(db, account=account, direction="in", amount_fen=2_000, counterparty="测试退款", contra_code="SHOP:test", category="退款")
        db.add(TaskPlan(id="PL-card", user_id=1, goal="订花", source_text="订花", source="rule", status="waiting",
                        due_at=now() + timedelta(days=2), steps=[
                            {"id": "s1", "status": "done", "output": {"pot_id": pot_id}},
                            {"id": "s2", "status": "pending", "tool": "order.propose", "at": "due", "title": "用心愿罐订花",
                             "args": {"pot_id": "{{s1.pot_id}}", "amount_fen": 10_000}}]))
        db.commit()
    card = say(client, "我的心愿罐")["cards"][0]
    assert card["pots"] == [{"pot_id": pot_id, "purpose": "旅行", "until": None, "balance": "420.00",
                             "balance_fen": 42_000, "spent": "80.00", "spent_fen": 8_000,
                             "plans": [{"label": "用心愿罐订花", "run_at": "2026-11-01 12:00"}]}]
    refused = withdraw_card(client, pot_id, 33_000)
    assert refused["type"] == "notice" and "预占 100.00" in refused["text"]
    assert withdraw_card(client, pot_id, 32_000)["type"] == "confirm"
    assert ledger_ok(client)


@pytest.mark.parametrize("sentence", ["从心愿罐取出200", "心愿罐转出200到活期", "把心愿罐的钱转回来200"])
def test_withdraw_sentence_routes(client, sentence):
    pot_id = deposit(client)
    card = say(client, sentence)["cards"][0]
    assert card["type"] == "confirm" and card["level"] == "YELLOW"
    with new_session() as db:
        action = db.get(PendingAction, card["action_id"])
        assert action.tool == "pot.withdraw" and action.args["pot_id"] == pot_id and action.args["amount_fen"] == 20_000


def test_deposit_selection_existing_and_new(client):
    pot_id = deposit(client)
    card = say(client, "追加", selection={"type": "pot_manage", "op": "deposit", "pot_id": pot_id,
               "purpose": "伪造用途", "amount_fen": 1000})["cards"][0]
    assert card["type"] == "confirm"
    with new_session() as db:
        action = db.get(PendingAction, card["action_id"])
        assert action.tool == "pot.lock" and action.args["pot_id"] == pot_id and action.args["purpose"] == "旅行"
    assert say(client, "新罐", selection={"type": "pot_manage", "op": "deposit", "amount_fen": 1000})["cards"][0]["type"] == "notice"


def test_partial_withdraw_idempotency_and_zero_pot_retained(client):
    pot_id = deposit(client)
    card = withdraw_card(client, pot_id)
    result = confirm(client, card).json()
    assert result["status"] == "succeeded"
    detail = client.get(f"/api/pots/{pot_id}/txns").json()
    assert detail["items"][0]["kind"] == "withdraw"
    assert detail["items"][0]["label"] == "转出到活期"
    assert detail["items"][0]["amount_fen"] == -20_000
    assert say(client, "心愿罐明细")["cards"][0]["items"] == detail["items"]
    event = next(event for event in client.get("/admin/audit?limit=500").json() if event["event"] == "pot_withdrawn")
    assert {key: event["payload"][key] for key in ("purpose", "amount_fen", "balance_fen")} == {
        "purpose": "旅行", "amount_fen": 20_000, "balance_fen": 30_000}
    assert confirm(client, card).status_code == 409
    with new_session() as db:
        action = db.get(PendingAction, card["action_id"])
        account = db.get(Account, db.get(WishPot, pot_id).account_id)
        assert account.balance_fen == 30_000
        withdraw(db, action.args, idempotency_key=action.id, user_id=1)
        assert account.balance_fen == 30_000
        assert len(list(db.scalars(select(Transaction).where(Transaction.idempotency_key == action.id)))) == 1
        db.commit()
    card = withdraw_card(client, pot_id, 30_000)
    assert confirm(client, card).json()["status"] == "succeeded"
    pots = say(client, "我的心愿罐")["cards"][0]["pots"]
    assert pots[0]["balance_fen"] == 0 and pots[0]["spent_fen"] == 0
    with new_session() as db:
        assert db.get(WishPot, pot_id).status == "active"
    events = [event for event in client.get("/admin/audit?limit=500").json() if event["event"] == "pot_withdrawn"]
    assert len(events) == 2
    assert {key: events[0]["payload"][key] for key in ("purpose", "amount_fen", "balance_fen")} == {
        "purpose": "旅行", "amount_fen": 30_000, "balance_fen": 0}
    assert client.post("/admin/audit/verify").json()["ok"] and ledger_ok(client)


def test_overdraft_and_balance_change_invalidate_old_confirmation(client):
    pot_id = deposit(client)
    assert withdraw_card(client, pot_id, 50_001)["type"] == "notice"
    old, other = withdraw_card(client, pot_id), withdraw_card(client, pot_id, 1)
    assert confirm(client, other).json()["status"] == "succeeded"
    assert confirm(client, old).json()["status"] == "invalidated"


def test_withdraw_not_counted_in_natural_day_or_rolling_outflow(client):
    pot_id = deposit(client, 200_000)
    card = withdraw_card(client, pot_id, 150_000)
    assert card["level"] == "YELLOW" and not REGISTRY["pot.withdraw"].moves_money
    assert not REGISTRY["pot.withdraw"].llm_visible and REGISTRY["pot.withdraw"].side_effect
    assert confirm(client, card).json()["status"] == "succeeded"
    transfer = say(client, "给我妈转100")["cards"][0]
    assert transfer["level"] == "YELLOW"
    with new_session() as db:
        counter = db.get(LimitCounter, (1, now().date()))
        assert counter.used_fen == 0 and counter.reserved_fen == 10_000
        action = db.get(PendingAction, transfer["action_id"])
        decision = decide(db, action.tool, action.args, PolicyEnv(1, exclude=action))
        assert "rolling_24h_outflow" not in decision.rule_hits


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "20000", None, [], {}, 10**20])
def test_selection_amount_type_range(client, amount):
    pot_id = deposit(client)
    for op in ("withdraw", "deposit"):
        card = say(client, "操作", selection={"type": "pot_manage", "op": op, "pot_id": pot_id,
                   "amount_fen": amount})["cards"][0]
        assert card["type"] == "notice"


def test_manage_llm_enabled_deterministic_priority(llm_client, fake):
    model = fake(text("不应调模型"))
    assert say(llm_client, "我的心愿罐")["cards"][0]["type"] == "pots"
    assert model.calls == []


def test_model_withdraw_amount_must_be_from_current_turn(client):
    from app.models import ChatSession
    from app.tools import ToolContext
    from app.tools.gateway import run_read_tool

    deposit(client)
    response = say(client, "我的心愿罐")
    with new_session() as db:
        session = db.get(ChatSession, response["session_id"])
        ctx = ToolContext(db, 1, session, "source-check", ["从心愿罐取出100", "从心愿罐取出200"])
        result = run_read_tool(REGISTRY["pot.withdraw_propose"], ctx, {"amount_text": "200"})
        assert result.data["status"] == "amount_not_from_user" and result.card["type"] == "notice"
        result = run_read_tool(REGISTRY["pot.withdraw_propose"], ctx, {"amount_text": "100"})
        assert result.card["type"] == "confirm"


def test_purchase_authorized_budget_and_new_reservation_invalidate(client):
    pot_id = deposit(client)
    old = withdraw_card(client, pot_id, 30_000)
    with new_session() as db:
        db.add(TaskPlan(id="PL-purchase", user_id=1, goal="定时购书", source_text="定时购书", source="rule", status="waiting",
                        steps=[{"id": "s1", "status": "done", "output": {"pot_id": pot_id}},
                               {"id": "s2", "status": "pending", "tool": "order.propose", "title": "心愿罐购书",
                                "args": {"pot_id": "{{s1.pot_id}}"},
                                "purchase": {"run_at": "2026-11-18T10:00:00", "budget_fen": 25_000,
                                             "estimated_fen": 15_000}}]))
        db.commit()
    pot = say(client, "我的心愿罐")["cards"][0]["pots"][0]
    assert pot["plans"] == [{"label": "心愿罐购书", "run_at": "2026-11-18 10:00"}]
    assert confirm(client, old).json()["status"] == "invalidated"
    assert withdraw_card(client, pot_id, 25_000)["type"] == "confirm"
    assert withdraw_card(client, pot_id, 25_001)["type"] == "notice"


def test_model_manage_tool_ends_turn(llm_client, fake):
    model = fake(tool("pot_manage"), text("不应执行的第二次模型调用"))
    result = say(llm_client, "看看专项存钱的罐子")
    assert result["cards"][0]["type"] == "pots" and len(model.calls) == 1
