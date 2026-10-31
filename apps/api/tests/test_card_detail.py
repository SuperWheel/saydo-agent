"""卡片详情（只读）、查看完整卡号（人脸验证后才返回）、模拟绑卡（🔴 人脸 + 短信、最多 20 张）。默认走规则兜底模式。"""

import json

import pytest
from sqlalchemy import func, select

from app.core_mock import cards as core
from app.db import new_session
from app.models import Card

from .conftest import confirm, say
from .test_flows import pass_auth

DEBIT_NO, CREDIT_NO = "6230581000006688", "6259651000003721"


def card_id(kind: str = "debit") -> int:
    with new_session() as db:
        return db.scalars(select(Card.id).where(Card.kind == kind).order_by(Card.id)).first()


def card_count() -> int:
    with new_session() as db:
        return db.scalar(select(func.count()).select_from(Card))


def fill_cards(n: int, start: int = 0) -> None:
    """直接在库里补绑卡池第 start 张起的 n 张，造出接近上限的用户。"""
    with new_session() as db:
        for p in core.BIND_POOL[start:start + n]:
            db.add(Card(user_id=1, kind=p.kind, name=p.name, card_no=p.card_no, limits={
                "overseas": False, "online": True, "atm": True, "per_txn_fen": 100, "daily_fen": 100}))
        db.commit()


def start_reveal(client, cid: int) -> dict:
    r = client.post(f"/api/cards/{cid}/reveal")
    assert r.status_code == 200, r.text
    return r.json()


def face(client, ch: dict, value: str | None = None):
    return client.post(f"/api/cards/reveals/{ch['challenge_id']}/verify",
                       json={"factor": "face", "value": value or f"face-ok:{ch['digest']}"})


# ---------- 卡片模拟核心数据 ----------

def test_bind_pool_is_twenty_valid_union_pay_cards():
    def luhn_ok(no: str) -> bool:
        total = 0
        for i, d in enumerate(reversed(no)):
            n = int(d) * (2 if i % 2 else 1)
            total += n - 9 if n > 9 else n
        return total % 10 == 0

    pool = core.BIND_POOL
    assert len(pool) == 20 and len({p.id for p in pool}) == 20
    assert len({p.bank for p in pool}) >= 10 and {p.kind for p in pool} == {"debit", "credit"}
    assert all(p.card_no.startswith("62") and 16 <= len(p.card_no) <= 19 and luhn_ok(p.card_no) for p in pool)
    tails = [p.tail for p in pool]
    assert len(set(tails)) == 20 and not {"6688", "3721"} & set(tails)  # 后四位能唯一匹配一张卡
    assert core.masked_no("6230581000006688") == "6230 **** **** 6688"
    assert core.masked_no("6222028385608944150") == "6222 **** **** *** 4150"


# ---------- 详情 ----------

def test_card_list_has_ids_and_bind_entry(client):
    c = say(client, "查看我的银行卡")["cards"][0]
    assert c["type"] == "card_list" and c["can_bind"] is True and all(isinstance(x["id"], int) for x in c["cards"])


def test_debit_detail_masks_number_and_balance_comes_from_ledger(client):
    r = client.get(f"/api/cards/{card_id('debit')}")
    assert r.status_code == 200
    d = r.json()
    assert d["card_no_masked"] == "6230 **** **** 6688" and DEBIT_NO not in json.dumps(d, ensure_ascii=False)
    rows = dict(d["rows"])
    assert rows["开户银行"] == "言行银行" and rows["卡种"] == "Ⅰ类借记卡" and rows["户名"] == "李*"
    assert rows["卡片状态"] == "正常" and rows["开户日期"] == "2019-03-15" and rows["预留手机号"] == "138****6688"
    assert "开户网点" in rows
    balance = client.get("/api/accounts").json()[0]["balance"]  # 账务引擎记账的余额，不是写死的数
    assert dict(d["money"]) == {"账户余额": f"¥{balance}"}
    assert ["单日限额", "¥30,000.00"] in d["limits"]


def test_credit_detail_shows_credit_line(client):
    d = client.get(f"/api/cards/{card_id('credit')}").json()
    assert dict(d["money"]) == {"信用额度": "¥50,000.00", "可用额度": "¥46,321.50"}
    assert dict(d["rows"])["卡种"] == "信用卡（金卡）" and CREDIT_NO not in json.dumps(d, ensure_ascii=False)


def test_detail_of_someone_elses_card_is_404(client):
    assert client.get("/api/cards/9999").status_code == 404
    assert client.post("/api/cards/9999/reveal").status_code == 404


# ---------- 完整卡号：人脸通过后才返回 ----------

def test_full_number_needs_face(client):
    cid = card_id("debit")
    ch = start_reveal(client, cid)
    assert [f["factor"] for f in ch["factors"]] == ["face"] and DEBIT_NO not in json.dumps(ch)  # 只要人脸，不要短信
    r = face(client, ch, "face-ok:伪造")
    assert r.status_code == 403 and DEBIT_NO not in r.text
    r = face(client, ch)
    assert r.status_code == 200 and r.json()["card_no"].replace(" ", "") == DEBIT_NO
    assert face(client, ch).status_code == 409  # 一次一用：关掉弹层再看要重新验证


def test_face_assertion_is_bound_to_its_challenge(client):
    """另一张卡的挑战通过的断言不能拿来看这张卡。"""
    a, b = start_reveal(client, card_id("debit")), start_reveal(client, card_id("credit"))
    assert face(client, b, f"face-ok:{a['digest']}").status_code == 403
    assert face(client, b).json()["card_no"].replace(" ", "") == CREDIT_NO


def test_audit_records_view_without_card_number(client):
    ch = start_reveal(client, card_id("debit"))
    face(client, ch)
    log = client.get(f"/admin/audit?trace_id={ch['challenge_id']}").json()
    events = [e["event"] for e in log]
    assert events == ["auth_challenge", "auth_passed", "card_number_viewed"]
    viewed = log[-1]["payload"]
    assert viewed["card_tail"] == "6688"
    blob = json.dumps(client.get("/admin/audit?limit=500").json(), ensure_ascii=False)
    assert DEBIT_NO not in blob and "6230 5810" not in blob
    assert client.post("/admin/audit/verify").json()["ok"]


def test_three_face_failures_freeze_reveal(client):
    cid = card_id("debit")
    ch = start_reveal(client, cid)
    codes = [face(client, ch, "face-ok:x").status_code for _ in range(3)]
    assert codes == [403, 403, 403] and face(client, ch).status_code == 409  # 第三次失败后这次挑战作废
    r = client.post(f"/api/cards/{cid}/reveal")
    assert r.status_code == 423 and "安全保护" in r.json()["detail"]  # 连续失败触发熔断，期间不能再看


def test_reveal_blocked_under_kill_switch(client):
    client.post("/admin/kill-switch", json={"enabled": True, "reason": "演练"})
    assert client.post(f"/api/cards/{card_id()}/reveal").status_code == 423


def test_full_number_never_reaches_chat_or_model_context(llm_client, fake):
    """看完卡号后再聊卡片：对话、模型上下文里都没有完整卡号。"""
    from .conftest import text, tool
    f = fake(tool("card_list"), text("你名下有 2 张卡。"))
    ch = start_reveal(llm_client, card_id("debit"))
    face(llm_client, ch)
    r = say(llm_client, "我的卡都是什么状态")
    blob = json.dumps([r, f.calls], ensure_ascii=False, default=str)
    assert DEBIT_NO not in blob and CREDIT_NO not in blob


# ---------- 绑卡 ----------

@pytest.mark.parametrize("words", ["我要绑一张新卡", "添加银行卡", "绑定新卡", "绑卡"])
def test_bind_intent_shows_bind_card(client, words):
    r = say(client, words)
    c = r["cards"][0]
    assert c["type"] == "card_bind" and len(c["candidates"]) == 20 and c["count"] == 2 and c["max"] == 20
    assert all(p["card_no_masked"].endswith(p["tail"]) for p in c["candidates"])
    blob = json.dumps(r, ensure_ascii=False)
    assert all(p.card_no not in blob for p in core.BIND_POOL)  # 卡池也只露掩码


def test_bind_by_tail_red_face_and_sms_then_listed(client):
    pool = core.POOL_BY_ID["P10"]  # 招商银行信用卡
    r = say(client, f"绑定尾号{pool.tail}的卡")
    c = r["cards"][0]
    assert c["type"] == "confirm" and c["level"] == "RED" and c["fields"]["op"] == "bind"
    rows = dict(c["rows"])
    assert rows["发卡银行"] == "招商银行" and rows["预留手机号"] == "138****6688" and rows["户名"] == "李*"
    assert pool.card_no not in json.dumps(r, ensure_ascii=False)
    assert any("强验证" in reason for reason in c["reasons"])
    auth = confirm(client, c).json()
    assert auth["status"] == "awaiting_auth" and [f["factor"] for f in auth["card"]["factors"]] == ["face", "sms"]
    assert card_count() == 2  # 验证前不绑
    done = pass_auth(client, auth["card"])
    assert done["status"] == "succeeded" and card_count() == 3
    listed = say(client, "查看我的银行卡")["cards"][0]["cards"]
    assert [x["tail"] for x in listed][-1] == pool.tail and listed[-1]["name"] == "招商银行信用卡"
    d = client.get(f"/api/cards/{listed[-1]['id']}").json()  # 绑上的卡也能看详情
    assert dict(d["rows"])["开户银行"] == "招商银行" and dict(d["money"])["信用额度"] == "¥50,000.00" and d["bound"]
    ch = start_reveal(client, listed[-1]["id"])
    assert face(client, ch).json()["card_no"].replace(" ", "") == pool.card_no
    # 已经绑过：不再出确认卡
    again = say(client, f"绑定尾号{pool.tail}的卡")["cards"][0]
    assert again["type"] == "notice" and "已经绑定" in again["text"]


def test_bind_by_selection_and_typed_tail(client):
    sid = say(client, "我要绑一张新卡")["session_id"]
    r = say(client, "", sid, selection={"type": "card_bind", "pool_id": "P01"})
    assert r["cards"][0]["type"] == "confirm" and r["cards"][0]["level"] == "RED"
    tail = core.POOL_BY_ID["P05"].tail
    r = say(client, "", sid, selection={"type": "card_bind", "tail": tail})  # 卡片上输入的后四位
    assert r["cards"][0]["type"] == "confirm" and dict(r["cards"][0]["rows"])["发卡银行"] == "中国农业银行"


def test_bind_typed_tail_in_chat_after_bind_card(client):
    sid = say(client, "添加银行卡")["session_id"]
    r = say(client, core.POOL_BY_ID["P09"].tail, sid)
    assert r["cards"][0]["type"] == "confirm" and dict(r["cards"][0]["rows"])["发卡银行"] == "招商银行"


def test_bind_unknown_tail(client):
    c = say(client, "绑定尾号0000的卡")["cards"][0]
    assert c["type"] == "notice" and "没有找到尾号0000" in c["text"]


def test_twenty_first_card_is_refused(client):
    fill_cards(18)  # 原有 2 张 + 18 张 = 20 张
    r = say(client, "我要绑一张新卡")
    assert [c["type"] for c in r["cards"]] == ["notice"] and r["cards"][0]["text"] == "目前最多支持绑定 20 张卡。"
    r = say(client, f"绑定尾号{core.BIND_POOL[19].tail}的卡")
    assert [c["type"] for c in r["cards"]] == ["notice"] and "最多支持绑定 20 张卡" in r["reply"]["text"]
    r = say(client, "", r["session_id"], selection={"type": "card_bind", "pool_id": "P20"})
    assert [c["type"] for c in r["cards"]] == ["notice"]
    assert card_count() == 20


def test_limit_rechecked_before_execution(client):
    """出确认卡时 19 张，确认前又多了一张：执行前重新校验，授权作废，不会绑成第 21 张。"""
    fill_cards(17)
    c = say(client, f"绑定尾号{core.BIND_POOL[19].tail}的卡")["cards"][0]
    assert c["type"] == "confirm"
    fill_cards(1, start=17)  # P18 从别处绑上了，凑满 20 张
    assert card_count() == 20
    out = confirm(client, c).json()
    assert out["status"] == "invalidated" and card_count() == 20


def test_bind_blocked_by_breaker_kill_switch(client):
    """绑卡不是止损操作：全局只读时出不了确认卡。"""
    client.post("/admin/kill-switch", json={"enabled": True, "reason": "演练"})
    c = say(client, f"绑定尾号{core.BIND_POOL[0].tail}的卡")["cards"][0]
    assert c["type"] == "notice" and c.get("breaker")
