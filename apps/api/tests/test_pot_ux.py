"""手机走查反馈：心愿罐单步存入、「心愿罐」直达管理卡、收支统计走工作流、模型重复查询只留一张卡。"""

import pytest

from app.agent.loop import _dedupe_cards
from app.nlu import rule_intent

from .conftest import confirm, say
from .helpers import pass_face


@pytest.mark.parametrize("text,amount", [
    ("往我的心愿罐里存2000", "2000"), ("帮我往心愿罐里存入500元", "500元"), ("心愿罐再存500", "500"),
    ("往心愿罐里转500", "500"), ("存800到心愿罐，给妈妈买礼物", "800"),
])
def test_single_step_deposit_is_not_taken_by_pot_overview(text, amount):
    assert rule_intent(text).intent == "pot"
    assert rule_intent(text).slots == {"op": "deposit", "amount_text": amount}


@pytest.mark.parametrize("text", [
    "下周五我妈生日，帮我订束花和蛋糕，再存 500 进心愿罐",
    "我爱人11月20号生日，当月锁定1000元活期，生日前2天订鲜花和蛋糕",
    "存1000到心愿罐，后天用它给我妈买鞋",
])
def test_multi_step_sentences_still_go_to_planner(text):
    assert rule_intent(text).slots.get("op") != "deposit"


def test_deposit_through_my_pot_wording_shows_red_confirm_then_one_factor(client):
    reply = say(client, "往我的心愿罐里存2000")
    assert reply["reply"]["source"] == "rule"
    card = reply["cards"][0]
    assert [c["type"] for c in reply["cards"]] == ["confirm"]
    assert card["title"] == "存入心愿罐" and card["level"] == "RED" and "2,000.00" in str(card)
    result = confirm(client, card).json()
    assert result["status"] == "awaiting_auth" and result["card"]["mode"] == "any"
    assert pass_face(client, result["card"])["status"] == "succeeded"


@pytest.mark.parametrize("text", ["心愿罐", "打开心愿罐", "查看我的心愿罐"])
def test_bare_pot_opens_management_card(client, text):
    reply = say(client, text)
    assert reply["reply"]["source"] == "rule"
    assert [c["type"] for c in reply["cards"]] == ["pots"]


@pytest.mark.parametrize("text,title", [
    ("近一年的收支统计", "近365天账单报告"), ("帮我统计一下收支", None), ("收支统计", None), ("2025年收支汇总", "2025年账单报告"),
])
def test_income_expense_statistics_is_rule_workflow(client, text, title):
    reply = say(client, text)
    assert reply["reply"]["source"] == "rule"
    assert [c["type"] for c in reply["cards"]] == ["bill_report"]
    if title:
        assert reply["cards"][0]["title"] == title


def test_duplicate_query_cards_from_model_are_collapsed():
    report = {"type": "bill_report", "title": "近365天账单报告", "summary": "x", "as_of": "2026-10-30 10:00"}
    cards = [report, {**report, "as_of": "2026-10-30 10:01"}, {"type": "balance", "title": "余额"},
             {"type": "confirm", "action_id": "A1"}, {"type": "confirm", "action_id": "A1"}]
    _dedupe_cards(cards)
    assert [c["type"] for c in cards] == ["bill_report", "balance", "confirm", "confirm"]
