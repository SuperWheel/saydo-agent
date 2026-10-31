"""解绑银行卡（🔴）：只能解绑绑定进来的卡，主账户卡不能解绑；有未完成事项（待确认的确认卡、信用卡欠款）时拦下；
解绑后从列表消失、卡池里那张能重新绑、20 张上限按解绑后的数量算。默认走规则兜底模式。"""

import pytest
from sqlalchemy import func, select

from app.core_mock import cards as core
from app.db import new_session
from app.models import Card

from .conftest import confirm, say
from .helpers import pass_auth

P01, P10, P14 = core.POOL_BY_ID["P01"], core.POOL_BY_ID["P10"], core.POOL_BY_ID["P14"]  # 工行借记卡 / 招行信用卡（有欠款）/ 中信信用卡（没欠款）


def add_pool_cards(*pool: core.PoolCard) -> None:
    """直接在库里放几张绑定进来的卡（和绑卡执行写入的一样，卡号在卡池里）。"""
    with new_session() as db:
        for p in pool:
            db.add(Card(user_id=1, kind=p.kind, name=p.name, card_no=p.card_no, credit_limit_fen=p.credit_limit_fen,
                        available_fen=p.available_fen, limits={"overseas": False, "online": True, "atm": True,
                                                              "per_txn_fen": 500_000, "daily_fen": 2_000_000}))
        db.commit()


def card_of(p: core.PoolCard) -> Card:
    with new_session() as db:
        return db.scalars(select(Card).where(Card.card_no == p.card_no).order_by(Card.id.desc())).first()


def listed_tails(client) -> list[str]:
    return [c["tail"] for c in say(client, "查看我的银行卡")["cards"][0]["cards"]]


def only(r: dict) -> dict:
    assert len(r["cards"]) == 1, r["cards"]
    return r["cards"][0]


# ---------- 能不能解绑 ----------

def test_main_account_card_cannot_be_unbound(client):
    c = only(say(client, "解绑尾号6688的卡"))
    assert c["type"] == "notice" and "主账户卡" in c["text"] and "网点" in c["text"]
    with new_session() as db:
        main_id = db.scalars(select(Card.id).where(Card.card_no == "6230581000006688")).first()
    d = client.get(f"/api/cards/{main_id}").json()
    assert d["unbind"]["allowed"] is False and "主账户卡" in d["unbind"]["reason"]
    r = say(client, "", selection={"type": "card_unbind", "card_id": main_id})  # 绕过前端按钮也不行
    assert only(r)["type"] == "notice" and "主账户卡" in r["cards"][0]["text"]


def test_home_bank_credit_card_cannot_be_unbound(client):
    c = only(say(client, "解绑尾号3721的卡"))
    assert c["type"] == "notice" and "不是绑定进来的" in c["text"]


def test_no_bound_card_yet(client):
    c = only(say(client, "删除银行卡"))
    assert c["type"] == "notice" and "还没有绑定进来的卡" in c["text"]


# ---------- 解绑主流程 ----------

def test_unbind_red_then_gone_and_can_rebind(client):
    add_pool_cards(P01)
    assert P01.tail in listed_tails(client)
    c = only(say(client, f"解绑尾号{P01.tail}的卡"))
    assert c["type"] == "confirm" and c["level"] == "RED" and c["fields"]["op"] == "unbind"
    rows = dict(c["rows"])
    assert rows["发卡银行"] == "中国工商银行" and rows["已绑定"].startswith("3 张 → 2 张")
    assert any("解绑后这张卡不能再在这里查看和管理" in x for x in c["reasons"])
    assert P01.card_no not in str(c)
    auth = confirm(client, c).json()
    assert auth["status"] == "awaiting_auth"
    assert P01.tail in listed_tails(client)  # 验证通过前不解绑
    done = pass_auth(client, auth["card"])
    assert done["status"] == "succeeded" and dict(done["card"]["rows"])["已解绑"].endswith(f"（尾号{P01.tail}）")
    assert P01.tail not in listed_tails(client)
    assert client.get(f"/api/cards/{card_of(P01).id}").status_code == 404  # 详情也打不开了
    assert client.post(f"/api/cards/{card_of(P01).id}/reveal").status_code == 404
    # 卡池里那张可以重新绑定
    r = say(client, f"绑定尾号{P01.tail}的卡")
    assert only(r)["type"] == "confirm" and only(r)["fields"]["op"] == "bind"


def test_unbind_from_detail_button(client):
    add_pool_cards(P14)
    cid = card_of(P14).id
    d = client.get(f"/api/cards/{cid}").json()
    assert d["unbind"] == {"allowed": True, "reason": None}
    r = say(client, "", selection={"type": "card_unbind", "card_id": cid})
    assert only(r)["type"] == "confirm" and dict(r["cards"][0]["rows"])["发卡银行"] == "中信银行"
    assert r["reply"]["text"].startswith("请核对确认卡")


@pytest.mark.parametrize("words", ["解绑中信银行那张卡", "把中信的信用卡解绑", "删掉我的中信信用卡"])
def test_unbind_by_bank_name(client, words):
    add_pool_cards(P01, P14)
    c = only(say(client, words))
    assert c["type"] == "confirm" and c["fields"]["bank"] == "中信银行"


def test_several_bound_cards_asks_which(client):
    add_pool_cards(P01, P14)
    r = say(client, "解绑银行卡")
    c = only(r)
    assert c["type"] == "card_list" and {x["tail"] for x in c["cards"]} == {P01.tail, P14.tail}  # 只列能解绑的
    assert "要解绑哪张卡" in r["reply"]["text"]
    r = say(client, f"尾号{P14.tail}", r["session_id"])
    assert only(r)["type"] == "confirm" and only(r)["fields"]["card_tail"] == P14.tail


# ---------- 未完成事项 ----------

def test_credit_card_with_debt_is_blocked(client):
    add_pool_cards(P10)
    owed = P10.credit_limit_fen - P10.available_fen
    c = only(say(client, f"解绑尾号{P10.tail}的卡"))
    assert c["type"] == "notice" and "未还清" in c["text"] and f"{owed // 100:,}" in c["text"]
    d = client.get(f"/api/cards/{card_of(P10).id}").json()
    assert d["unbind"]["allowed"] is False and "未还清" in d["unbind"]["reason"]


def test_pending_confirm_card_on_this_card_blocks(client):
    add_pool_cards(P01)
    sid = say(client, f"把尾号{P01.tail}的卡锁了")["session_id"]
    c = only(say(client, f"解绑尾号{P01.tail}的卡", sid))
    assert c["type"] == "notice" and "待确认的锁卡" in c["text"]


def test_new_pending_item_invalidates_unbind_before_execution(client):
    """出了解绑确认卡后又发起了锁这张卡：执行前重新校验，解绑授权作废，卡还在。"""
    add_pool_cards(P01)
    sid = say(client, "查看我的银行卡")["session_id"]
    c = only(say(client, f"解绑尾号{P01.tail}的卡", sid))
    assert only(say(client, f"把尾号{P01.tail}的卡锁了", sid))["type"] == "confirm"
    out = confirm(client, c).json()
    assert out["status"] == "invalidated" and "待确认的锁卡" in out["card"]["text"]
    assert P01.tail in listed_tails(client)


def test_old_confirm_cards_on_unbound_card_are_void(client):
    """解绑之前发出的锁卡确认卡，解绑后再点确认：执行前重新校验作废。"""
    add_pool_cards(P01)
    lock = only(say(client, f"把尾号{P01.tail}的卡锁了"))
    with new_session() as db:  # 模拟另一处已经把这张卡解绑了
        db.get(Card, card_of(P01).id).status = "unbound"
        db.commit()
    assert confirm(client, lock).json()["status"] == "invalidated"


# ---------- 上限与熔断 ----------

def test_limit_counts_after_unbind(client):
    add_pool_cards(*core.BIND_POOL[:18])  # 2 + 18 = 20 张
    assert only(say(client, "我要绑一张新卡"))["text"] == "目前最多支持绑定 20 张卡。"
    c = only(say(client, f"解绑尾号{P01.tail}的卡"))
    pass_auth(client, confirm(client, c).json()["card"])
    bind = only(say(client, "我要绑一张新卡"))
    assert bind["type"] == "card_bind" and bind["count"] == 19 and P01.tail in [p["tail"] for p in bind["candidates"]]
    with new_session() as db:
        assert db.scalar(select(func.count()).select_from(Card).where(Card.status != "unbound")) == 19


def test_unbind_blocked_under_kill_switch(client):
    """解绑不是止损操作：全局只读时出不了确认卡。"""
    add_pool_cards(P01)
    client.post("/admin/kill-switch", json={"enabled": True, "reason": "演练"})
    c = only(say(client, f"解绑尾号{P01.tail}的卡"))
    assert c["type"] == "notice" and c.get("breaker")
