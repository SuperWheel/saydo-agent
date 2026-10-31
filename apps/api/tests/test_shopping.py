"""买东西（WS7 跨场景）：多品类候选卡 → 点选或说「要第二个」→ 订单确认卡（带订购平台）→ 修改换商品 → 下单。
规则模式为主；最后几条走模型路径（假模型），验证工具说明和「候选卡结束这一轮」。"""

from datetime import timedelta

import pytest

from app import clock
from app.agent.loop import _session
from app.channels import wants_write
from app.core_mock import shops
from app.db import new_session
from app.models import MerchantOrder, PendingAction
from app.tools import ToolContext, llm_tool_specs
from app.tools.automation import merchant_options, order_slots

from .conftest import confirm, say, text, tool
from .helpers import BALANCE, balance, events, ledger_ok, order, pass_auth
from .test_plan import card, fast_forward, plan_of, start_birthday


def options(client, sentence: str, sid=None) -> dict:
    r = say(client, sentence, sid)
    assert [c["type"] for c in r["cards"]] == ["shop_options"], r["cards"]
    return r


def pick(client, sid: str, sku: str) -> dict:
    return say(client, "", sid, selection={"type": "shop_pick", "sku": sku})


# ---------- 候选卡 ----------

@pytest.mark.parametrize("sentence,cat", [
    ("帮我买双鞋", "shoes"), ("想买个耳机", "digital"), ("帮我在超市买点纸巾", "daily"), ("买本书送孩子", "book"),
    ("帮我买一张购物卡", "giftcard"), ("帮我订一束花送我妈", "flower"), ("帮我订个蛋糕", "cake"),
])
def test_options_card_for_each_category(client, sentence, cat):
    r = options(client, sentence)
    c = r["cards"][0]
    assert c["category"] == cat and c["category_label"] == shops.CATEGORY_LABELS[cat] and len(c["items"]) == 3
    assert all(i["platform"] and i["shop_label"].startswith(i["platform"]) and i["delivery"] for i in c["items"])
    assert len({i["price"] for i in c["items"]}) == 3  # 三款不同价位
    assert r["reply"]["text"] == c["reply"] and "要第二个" in c["hint"]
    assert client.get("/admin/actions").json() == []  # 候选卡只是报价，不生成待确认动作
    assert "merchant.options" in str(client.get("/admin/audit").json())


def test_default_pick_keeps_old_choice(client):
    """候选卡第一款就是原来直接下单会挑的那件：生日计划、「要第一个」都还是康乃馨 199 / 鲜果蛋糕 238。"""
    flower = options(client, "帮我订一束花送我妈")["cards"][0]
    assert [i["sku"] for i in flower["items"]] == ["F101", "F103", "F102"] and flower["badge"] == "适合送我妈"
    assert flower["items"][0]["shop_label"] == "美团 · 花漾花艺" and flower["items"][0]["price"] == "199.00"
    cake = options(client, "帮我订个蛋糕")["cards"][0]
    assert cake["items"][0]["sku"] == "C201" and cake["badge"] == "推荐"


def test_shoes_differ_in_platform_and_delivery(client):
    c = options(client, "帮我买双鞋")["cards"][0]
    by_sku = {i["sku"]: i for i in c["items"]}
    assert [i["sku"] for i in c["items"]] == ["S301", "S303", "S302"]
    assert by_sku["S301"]["shop_label"] == "京东自营" and by_sku["S302"]["shop_label"] == "天猫 · 步云鞋服旗舰店"
    assert not by_sku["S302"]["whitelisted"] and by_sku["S301"]["whitelisted"]
    today = clock.now().date()
    assert by_sku["S301"]["date"] == f"{today + timedelta(days=1)}" and by_sku["S302"]["date"] == f"{today + timedelta(days=3)}"


def test_unsupported_goods_get_a_notice(client):
    with new_session() as db:
        ctx = ToolContext(db=db, user_id=1, session=_session(db, 1, None, "app"), trace_id="t", user_texts=[])
        r = merchant_options(ctx, "car")
    assert r.data["status"] == "unsupported" and "鞋服" in r.card["text"]


@pytest.mark.parametrize("sentence", ["上个月买衣服花了多少", "我每个月订阅花多少", "最近买的书", "买东西花的钱", "买第一个"])
def test_spending_questions_are_not_orders(sentence):
    assert order_slots(sentence) is None


def test_im_guides_shopping_back_to_app():
    assert wants_write("帮我买双鞋") and wants_write("想买个耳机") and not wants_write("最近买的书")


# ---------- 选一款 → 订单确认卡 ----------

def test_say_nth_gives_confirm_card_with_platform(client):
    r = options(client, "帮我买双鞋")
    r = say(client, "要第二个", r["session_id"])
    c = r["cards"][0]
    rows = dict(c["rows"])
    assert c["type"] == "confirm" and c["title"] == "订单确认" and c["amount"] == "269.00"
    assert rows["订购平台"] == "京东" and rows["商户"] == "京东自营（合作商户）" and rows["商品"] == "透气健步鞋（中老年款）"
    assert c["fields"]["platform"] == "京东" and "京东自营" in r["reply"]["text"] and r["reply"]["source"] == "rule"
    assert c["level"] == "RED"  # 没有心愿罐，从活期付款：照旧升 🔴
    assert "tool_call" in events(client, r["trace_id"])


def test_click_option_same_as_saying(client):
    r = options(client, "帮我买双鞋")
    c = pick(client, r["session_id"], "S302")["cards"][0]
    rows = dict(c["rows"])
    assert c["type"] == "confirm" and c["amount"] == "459.00"
    assert rows["订购平台"] == "天猫" and rows["商户"] == "步云鞋服旗舰店"  # 不是合作商户，不标「合作商户」
    assert rows["配送"].startswith(f"{clock.now().date() + timedelta(days=3):%m-%d}")


def test_repick_replaces_pending_card(client):
    """选了第一款还没确认，又说「换第三个」：旧卡作废，只挂着一张订单确认卡。"""
    sid = options(client, "帮我订个蛋糕")["session_id"]
    first = say(client, "要第一个", sid)["cards"][0]
    r = say(client, "换第三个", sid)
    new = r["cards"][0]
    assert new["type"] == "confirm" and new["amount"] == "188.00" and new["void_action_ids"] == [first["action_id"]]
    assert confirm(client, first).status_code == 409
    with new_session() as db:
        assert [a.status for a in db.query(PendingAction).order_by(PendingAction.created_at)] == ["cancelled", "pending"]


@pytest.mark.parametrize("sentence", ["要第五个", "最后一个"])
def test_ordinal_bounds(client, sentence):
    sid = options(client, "帮我订个蛋糕")["session_id"]
    c = say(client, sentence, sid)["cards"][0]
    if sentence == "最后一个":
        assert c["type"] == "confirm" and dict(c["rows"])["商品"] == "6 寸巧克力蛋糕"
    else:
        assert c["type"] == "notice" and "只列了 3 款" in c["text"]


def test_ordinal_without_options_is_not_a_pick(client):
    r = say(client, "要第二个")
    assert all(c["type"] != "confirm" for c in r["cards"])
    assert client.get("/admin/actions").json() == []


@pytest.mark.parametrize("sentence", ["买第一个理财", "锁第二张卡", "取消第一个"])
def test_other_ordinals_are_not_picks(client, sentence):
    sid = options(client, "帮我买双鞋")["session_id"]
    r = say(client, sentence, sid)
    assert all(c["type"] != "confirm" or c["title"] != "订单确认" for c in r["cards"])


def test_ordinal_answers_pending_question_not_pick(client):
    """出过候选卡以后又问到同名收款人：「第一个」是在回答追问，不是选商品。"""
    sid = options(client, "帮我买双鞋")["session_id"]
    assert say(client, "给张伟转200", sid)["cards"][0]["type"] == "clarify"
    r = say(client, "第一个", sid)
    assert all(c["type"] != "confirm" or c["title"] != "订单确认" for c in r["cards"])


# ---------- 付款、风险、修改 ----------

def test_whitelisted_shop_from_pot_is_yellow_and_edit_to_unknown_shop_is_red(client):
    """合作商户 + 心愿罐 + 小额 🟡；「修改」换成非合作商户（天猫旗舰店）升 🔴，规则和订花蛋糕一样。"""
    r = say(client, "存500进心愿罐")
    assert confirm(client, r["cards"][0]).json()["status"] == "succeeded"
    r = order(client, "帮我买双鞋", r["session_id"])
    c = r["cards"][0]
    assert c["level"] == "YELLOW" and dict(c["rows"])["付款方式"] == "心愿罐「心愿」"
    e = c["edit"]
    assert [i["shop"] for i in e["items"]] == ["京东自营", "天猫 · 步云鞋服旗舰店", "京东自营"] and len(e["dates"]) == 14
    first = {i["sku"]: i["first"] for i in e["items"]}  # 修改面板里快递 3 天的那款，日期从第 3 天起
    assert first["S302"] == f"{clock.now().date() + timedelta(days=3)}" and first["S301"] == e["dates"][0]
    d = (clock.now().date() + timedelta(days=4)).isoformat()
    new = say(client, "", r["session_id"], selection={"type": "order_edit", "action_id": c["action_id"],
                                                       "sku": "S302", "date": d})["cards"][0]
    assert new["level"] == "RED" and dict(new["rows"])["订购平台"] == "天猫" and new["void_action_ids"] == [c["action_id"]]


def test_buy_shoes_end_to_end(client):
    r = order(client, "帮我买双鞋")
    c = r["cards"][0]
    out = confirm(client, c).json()
    done = pass_auth(client, out["card"])
    assert done["status"] == "succeeded"
    rows = dict(done["card"]["rows"])
    assert rows["商户"] == "京东自营" and "18:00 前送达" in rows["配送"]
    assert balance(client) != BALANCE and ledger_ok(client)
    with new_session() as db:
        o = db.query(MerchantOrder).one()
        assert (o.shop_id, o.sku, o.amount_fen) == ("SH01", "S301", 32_900)
    cancel = say(client, "把鞋的订单取消", r["session_id"])["cards"][0]
    assert cancel["title"] == "取消订单" and dict(cancel["rows"])["商户"] == "京东自营"


def _paid_shoes(client) -> str:
    r = order(client, "帮我买双鞋")
    pass_auth(client, confirm(client, r["cards"][0]).json()["card"])
    return r["session_id"]


@pytest.mark.parametrize("sentence", ["帮我退款鞋子", "鞋子退款", "帮我把鞋子退了", "鞋退货", "我要退货", "申请退款",
                                      "退掉刚才买的鞋", "把鞋的订单取消"])
def test_refund_said_many_ways_gives_cancel_card(client, sentence):
    """下单后「帮我退款鞋子」之类：出取消订单确认卡，不是「没有这个功能」。"""
    c = say(client, sentence, _paid_shoes(client))["cards"][0]
    assert c["title"] == "取消订单" and dict(c["rows"])["商品"] == "轻跑运动鞋（男女同款）"


@pytest.mark.parametrize("sentence", ["上个月退款有多少", "鞋的退款到账了吗", "这个月的退款记录"])
def test_refund_questions_are_not_cancels(client, sentence):
    r = say(client, sentence, _paid_shoes(client))
    assert not any(c.get("title") == "取消订单" for c in r["cards"])


def test_llm_refund_is_deterministic(llm_client, fake):
    """模型路径：「帮我退款鞋子」按规则会被裁成账单工具，模型看不到取消订单、会说没下过单，所以确定性处理、不调模型。"""
    r = order(llm_client, "帮我买双鞋")
    pass_auth(llm_client, confirm(llm_client, r["cards"][0]).json()["card"])
    f = fake(text("你刚才的鞋子没有下单"))
    c = say(llm_client, "帮我退款鞋子", r["session_id"])["cards"][0]
    assert c["title"] == "取消订单" and not f.calls


def test_giftcard_is_instant_and_edit_has_no_dates(client):
    r = order(client, "帮我买一张购物卡")
    c = r["cards"][0]
    assert dict(c["rows"])["配送"] == shops.INSTANT and c["edit"]["dates"] == [] and c["amount"] == "200.00"
    new = say(client, "", r["session_id"], selection={"type": "order_edit", "action_id": c["action_id"],
                                                       "sku": "K702", "date": ""})["cards"][0]
    assert new["type"] == "confirm" and new["amount"] == "500.00"


def test_large_digital_order_is_red_even_from_pot(client):
    """超额升 🔴：心愿罐付款也一样（今日累计超过 1000）。"""
    r = say(client, "存5000进心愿罐")
    out = confirm(client, r["cards"][0]).json()
    if out.get("card", {}).get("type") == "auth":
        pass_auth(client, out["card"])
    sid = options(client, "想买个耳机", r["session_id"])["session_id"]
    c = say(client, "要第三个", sid)["cards"][0]
    assert c["amount"] == "2,199.00" and c["level"] == "RED"


# ---------- 生日计划主线不变 ----------

def test_birthday_plan_keeps_default_items_and_shows_platform(client):
    sid, plan = start_birthday(client)
    evs = fast_forward(client, plan["due_at"])
    flower = card(evs[0])
    rows = dict(flower["rows"])
    assert flower["amount"] == "199.00" and flower["level"] == "YELLOW"
    assert rows["订购平台"] == "美团" and rows["商户"] == "花漾花艺（合作商户）" and rows["商品"] == "康乃馨花束（19 枝）"
    assert plan_of(evs[0])["steps"][1]["detail"].startswith("美团 · 花漾花艺 · 康乃馨花束（19 枝） ¥199.00 · ")
    assert plan_of(evs[0])["steps"][1]["title"] == "询价：鲜花（花漾花艺）"


# ---------- 模型路径 ----------

def test_llm_tools_cover_all_categories():
    spec = next(t for t in llm_tool_specs(None) if t["function"]["name"] == "merchant_options")
    assert set(spec["function"]["parameters"]["properties"]["category"]["enum"]) == set(shops.CATEGORY_LABELS)
    quote = next(t for t in llm_tool_specs(None) if t["function"]["name"] == "merchant_quote")
    assert "shoes" in quote["function"]["parameters"]["properties"]["category"]["enum"]


def test_llm_options_card_ends_turn_then_pick_is_deterministic(llm_client, fake):
    f = fake(tool("merchant_options", category="shoes"), text("不该再调用模型"))
    r = say(llm_client, "帮我买双鞋")
    assert [c["type"] for c in r["cards"]] == ["shop_options"] and len(f.calls) == 1  # 候选卡结束这一轮
    assert "要第二个" in r["reply"]["text"]
    r = say(llm_client, "要第二个", r["session_id"])
    assert r["cards"][0]["type"] == "confirm" and len(f.calls) == 1  # 选第几个不调模型
    assert dict(r["cards"][0]["rows"])["商品"] == "透气健步鞋（中老年款）"


def test_llm_can_order_by_quote_id_from_options(llm_client, fake):
    """用户点名某一款（「要天猫那双」）交给模型：模型照抄候选结果里的 quote_id 调用 order_propose。"""
    fake(tool("merchant_options", category="shoes"))
    r = say(llm_client, "帮我买双鞋")
    qid = next(q for q in shops.options("shoes", delivery_date=None, at=clock.now()) if q.item.sku == "S302").quote_id
    f = fake(tool("order_propose", quote_id=qid))
    r = say(llm_client, "要天猫那双", r["session_id"])
    assert r["cards"][0]["type"] == "confirm" and dict(r["cards"][0]["rows"])["订购平台"] == "天猫"
    assert any(m["role"] == "tool" and "S302" in m["content"] for m in f.calls[0]["messages"])  # 历史里有候选结果
