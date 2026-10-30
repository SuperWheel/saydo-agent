"""卡片管理（WS3）：不对称定级（收紧 🟡 / 放宽 🔴）、熔断时止损仍可用、解锁和挂失强验证、「卡丢了」隐含意图、
执行前重新校验、限额虚拟卡、卡号脱敏。默认走规则兜底模式，模型路径用脚本化的假模型。"""

import json

import pytest
from sqlalchemy import select

from app.auth.actions import PolicyEnv, decide
from app.db import new_session
from app.models import Card
from app.nlu import rule_intent
from app.policy import Risk

from .conftest import confirm, say, text, tool
from .helpers import is_breaker
from .test_flows import pass_auth

DEBIT_NO, CREDIT_NO = "6230581000006688", "6259651000003721"


def card(kind: str) -> Card:
    with new_session() as db:
        return db.scalars(select(Card).where(Card.kind == kind)).first()


def set_card(kind: str, *, status: str | None = None, **limits) -> None:
    with new_session() as db:
        c = db.scalars(select(Card).where(Card.kind == kind)).first()
        if status:
            c.status = status
        c.limits = {**c.limits, **limits}
        db.commit()


def kill_switch(client, on: bool = True):
    client.post("/admin/kill-switch", json={"enabled": on, "reason": "演练"})


def only_card(r: dict) -> dict:
    assert len(r["cards"]) == 1, r["cards"]
    return r["cards"][0]


# ---------- 定级：表驱动 ----------

LIMIT_KEY = {"overseas": "overseas", "online": "online", "atm": "atm", "per_txn": "per_txn_fen", "daily": "daily_fen"}


@pytest.mark.parametrize("kind,setting,before,value,level", [
    ("debit", "overseas", False, True, Risk.RED),            # 开通境外 → 放宽 🔴
    ("credit", "overseas", True, False, Risk.YELLOW),        # 关闭境外 → 收紧 🟡
    ("debit", "online", True, False, Risk.YELLOW),
    ("debit", "online", False, True, Risk.RED),
    ("debit", "atm", True, False, Risk.YELLOW),
    ("credit", "atm", False, True, Risk.RED),
    ("debit", "per_txn", 1_000_000, 200_000, Risk.YELLOW),   # 调低限额 → 收紧
    ("debit", "per_txn", 1_000_000, 2_000_000, Risk.RED),    # 调高限额 → 放宽
    ("debit", "daily", 3_000_000, 500_000, Risk.YELLOW),
    ("debit", "daily", 3_000_000, 5_000_000, Risk.RED),
    ("credit", "daily", 5_000_000, 4_999_999, Risk.YELLOW),  # 少 1 分也是收紧
    ("credit", "per_txn", 2_000_000, 2_000_001, Risk.RED),   # 多 1 分就是放宽
])
def test_limit_asymmetric_levels(client, kind, setting, before, value, level):
    set_card(kind, **{LIMIT_KEY[setting]: before})
    args = {"card_id": card(kind).id, "from_status": "active", "setting": setting, "before": before, "value": value}
    with new_session() as db:
        d = decide(db, "card.limit_set", args, PolicyEnv(1))
    assert d.level == level and not d.block
    assert d.rule_hits == (["card_loosen"] if level == Risk.RED else [])
    assert d.required_factors == (["face", "sms"] if level == Risk.RED else [])


@pytest.mark.parametrize("tool_name,status,level,hits", [
    ("card.lock", "active", Risk.YELLOW, []),                    # 锁卡 🟡
    ("card.unlock", "locked", Risk.RED, ["card_unlock"]),        # 解锁 🔴
    ("card.unlock", "lost", Risk.RED, ["card_unreport"]),        # 解除挂失 🔴
    ("card.report_loss", "active", Risk.RED, ["card_report_loss"]),  # 挂失 🔴
    ("card.report_loss", "locked", Risk.RED, ["card_report_loss"]),
])
def test_status_levels(client, tool_name, status, level, hits):
    set_card("debit", status=status)
    with new_session() as db:
        d = decide(db, tool_name, {"card_id": card("debit").id, "from_status": status}, PolicyEnv(1))
    assert (d.level, d.rule_hits, d.block) == (level, hits, False)


@pytest.mark.parametrize("kind,setting,value,needle", [
    ("debit", "per_txn", 4_000_000, "不能高于单日限额"),
    ("debit", "daily", 5_000_001, "最高 50,000.00 元"),
    ("debit", "daily", 0, "必须大于 0"),
    ("debit", "overseas", False, "设置没有变化"),
])
def test_limit_blocks(client, kind, setting, value, needle):
    c = card(kind)
    args = {"card_id": c.id, "from_status": "active", "setting": setting, "before": c.limits[LIMIT_KEY[setting]],
            "value": value}
    with new_session() as db:
        d = decide(db, "card.limit_set", args, PolicyEnv(1))
    assert d.block and needle in d.block_reason


def test_direction_comes_from_card_state_not_from_args(client):
    """args 里的 before 和卡片实际设置对不上（比如被篡改成「本来是开通的」），直接拒绝，不会按收紧定级。"""
    args = {"card_id": card("debit").id, "from_status": "active", "setting": "overseas", "before": True, "value": True}
    with new_session() as db:
        d = decide(db, "card.limit_set", args, PolicyEnv(1))
    assert d.block and d.block_reason == "卡片设置已变化"


# ---------- 端到端：收紧 / 放宽 ----------

def test_tighten_is_yellow_and_applies(client):
    c = only_card(say(client, "关闭信用卡的境外交易"))
    assert (c["type"], c["level"]) == ("confirm", "YELLOW")
    assert c["fields"] == {"op": "limit", "card_tail": "3721", "setting": "overseas", "direction": "tighten"}
    assert ["调整方向", "收紧保护"] in c["rows"] and "amount" not in c
    assert card("credit").limits["overseas"] is True  # 确认之前不会改
    out = confirm(client, c).json()
    assert out["status"] == "succeeded" and out["card"]["status_label"] == "修改交易限制成功"
    assert card("credit").limits["overseas"] is False


def test_loosen_is_red_and_needs_face_and_sms(client):
    c = only_card(say(client, "开通借记卡的境外交易"))
    assert c["level"] == "RED" and c["reasons"] == ["放宽卡片交易限制（开启交易渠道或调高限额），需要本人强验证"]
    out = confirm(client, c).json()
    assert out["status"] == "awaiting_auth"
    assert [f["factor"] for f in out["card"]["factors"]] == ["face", "sms"]
    assert card("debit").limits["overseas"] is False  # 验证之前不会改
    assert pass_auth(client, out["card"])["status"] == "succeeded"
    assert card("debit").limits["overseas"] is True


def test_lowering_daily_below_per_txn_lowers_both(client):
    c = only_card(say(client, "把借记卡单日限额调到5000"))
    assert c["level"] == "YELLOW" and ["同时", "单笔限额同步调为 ¥5,000.00"] in c["rows"]
    assert confirm(client, c).json()["status"] == "succeeded"
    assert {k: card("debit").limits[k] for k in ("per_txn_fen", "daily_fen")} == {"per_txn_fen": 500_000, "daily_fen": 500_000}


def test_limit_without_card_asks_which_then_follows_up(client):
    r = say(client, "单笔限额调到2000")
    assert [x["type"] for x in r["cards"]] == ["card_list"] and "哪张卡" in r["reply"]["text"]
    c = only_card(say(client, "6688", r["session_id"]))
    assert (c["type"], c["level"], c["fields"]["card_tail"]) == ("confirm", "YELLOW", "6688")


def test_no_change_needs_no_confirmation(client):
    r = say(client, "关闭借记卡的境外交易")  # 种子数据里本来就是关闭的
    assert only_card(r)["type"] == "notice" and "不用修改" in r["reply"]["text"]
    assert client.get("/admin/actions").json() == []


# ---------- 熔断：止损仍可用，放松保护被拦 ----------

def test_kill_switch_lock_still_works(client):
    kill_switch(client)
    c = only_card(say(client, "锁借记卡"))
    assert (c["type"], c["level"]) == ("confirm", "YELLOW")
    out = confirm(client, c).json()
    assert out["status"] == "succeeded" and card("debit").status == "locked"


def test_kill_switch_blocks_unlock(client):
    set_card("debit", status="locked")
    kill_switch(client)
    r = say(client, "解锁借记卡")
    assert only_card(r)["type"] == "notice" and "只读保护" in r["reply"]["text"]
    assert client.get("/admin/actions").json() == [] and card("debit").status == "locked"


def test_kill_switch_invalidates_pending_unlock(client):
    set_card("debit", status="locked")
    c = only_card(say(client, "解锁借记卡"))
    kill_switch(client)  # 确认卡发出之后才开熔断
    out = confirm(client, c).json()
    assert out["status"] == "invalidated" and "只读保护" in out["card"]["text"]
    assert card("debit").status == "locked"


def test_kill_switch_report_loss_still_works_with_strong_auth(client):
    kill_switch(client)
    c = only_card(say(client, "挂失借记卡"))
    assert c["level"] == "RED"
    out = confirm(client, c).json()
    assert out["status"] == "awaiting_auth"
    assert pass_auth(client, out["card"])["status"] == "succeeded" and card("debit").status == "lost"


def test_kill_switch_blocks_loosen_and_virtual(client):
    kill_switch(client)
    for t in ("开通借记卡的境外交易", "申请一张500元的虚拟卡"):
        assert "只读保护" in only_card(say(client, t))["text"], t


@pytest.mark.parametrize("utterance,kind,key,after", [
    ("关闭信用卡的境外交易", "credit", "overseas", False),
    ("把借记卡单笔限额调到2000", "debit", "per_txn_fen", 200_000),
])
def test_kill_switch_tighten_limit_still_works(client, utterance, kind, key, after):
    """收紧交易限制是止损：熔断开着照常可办；熔断前发出的收紧确认卡，确认时也不作废。"""
    kill_switch(client)
    c = only_card(say(client, utterance))
    assert (c["type"], c["level"]) == ("confirm", "YELLOW")
    assert confirm(client, c).json()["status"] == "succeeded" and card(kind).limits[key] == after
    kill_switch(client, False)
    set_card(kind, **{key: True if key == "overseas" else 1_000_000})
    c = only_card(say(client, utterance))
    kill_switch(client)
    assert confirm(client, c).json()["status"] == "succeeded" and card(kind).limits[key] == after


def test_breaker_freeze_allows_tighten_blocks_loosen(client):
    """放宽限额连续 3 次验证失败，限额工具被暂停：收紧照常可办，放宽被拦。"""
    out = confirm(client, only_card(say(client, "开通借记卡的境外交易"))).json()
    url = f"/api/auth/challenges/{out['card']['challenge_id']}/verify"
    for _ in range(3):
        client.post(url, json={"factor": "sms", "value": "000000"})
    c = only_card(say(client, "把借记卡的无卡支付关掉"))
    assert (c["type"], c["level"]) == ("confirm", "YELLOW")
    assert confirm(client, c).json()["status"] == "succeeded" and card("debit").limits["online"] is False
    assert is_breaker(only_card(say(client, "开通借记卡的境外交易")), "freeze")


# ---------- 解锁、挂失：强验证 ----------

@pytest.mark.parametrize("status,utterance,title,after", [
    ("locked", "解锁借记卡", "解锁确认", "active"),
    ("lost", "解除借记卡挂失", "解除挂失确认", "active"),
    ("active", "挂失借记卡", "挂失确认", "lost"),
])
def test_unlock_and_report_loss_need_face_and_sms(client, status, utterance, title, after):
    set_card("debit", status=status)
    c = only_card(say(client, utterance))
    assert (c["title"], c["level"]) == (title, "RED") and "人脸识别" in c["auth_hint"]
    out = confirm(client, c).json()
    assert out["status"] == "awaiting_auth" and [f["factor"] for f in out["card"]["factors"]] == ["face", "sms"]
    url = f"/api/auth/challenges/{out['card']['challenge_id']}/verify"
    face = client.post(url, json={"factor": "face", "value": f"face-ok:{out['card']['digest']}"}).json()
    assert face["status"] == "awaiting_auth" and card("debit").status == status  # 只过人脸不执行
    done = pass_auth(client, out["card"])
    assert done["status"] == "succeeded" and card("debit").status == after


def test_failed_sms_cancels_unlock(client):
    set_card("debit", status="locked")
    out = confirm(client, only_card(say(client, "解锁借记卡"))).json()
    url = f"/api/auth/challenges/{out['card']['challenge_id']}/verify"
    for _ in range(3):
        r = client.post(url, json={"factor": "sms", "value": "000000"}).json()
    # 连错 3 次：本次授权取消，同时触发「同一业务连续 3 次验证失败」熔断（WS4）
    assert r["status"] == "cancelled" and is_breaker(r["card"], "freeze")
    assert card("debit").status == "locked"


def test_virtual_card_cannot_be_reported_lost(client):
    assert "没有找到" in say(client, "挂失虚拟卡")["reply"]["text"]  # 还没有虚拟卡
    _apply_virtual(client)
    r = say(client, "挂失尾号" + card("virtual").tail)
    assert only_card(r)["type"] == "notice" and "虚拟卡不需要挂失" in r["reply"]["text"]


# ---------- 「卡好像丢了」：建议锁卡，不替用户决定 ----------

def test_lost_card_suggests_lock_and_asks_which(client):
    r = say(client, "我卡好像丢了")
    assert [x["type"] for x in r["cards"]] == ["card_list"]
    assert "锁卡" in r["reply"]["text"] and "挂失" in r["reply"]["text"] and "哪张卡" in r["reply"]["text"]
    assert client.get("/admin/actions").json() == []  # 没有替用户生成任何动作
    c = only_card(say(client, "借记卡", r["session_id"]))
    assert (c["title"], c["level"], c["fields"]["card_tail"]) == ("锁卡确认", "YELLOW", "6688")  # 锁卡，不是挂失


def test_lost_card_followup_can_choose_report_loss(client):
    sid = say(client, "我卡好像丢了")["session_id"]
    r = say(client, "挂失吧", sid)
    assert "要挂失哪张卡" in r["reply"]["text"]
    c = only_card(say(client, "信用卡", sid))
    assert (c["title"], c["level"], c["fields"]["card_tail"]) == ("挂失确认", "RED", "3721")


def test_lost_specific_card_goes_straight_to_lock(client):
    r = say(client, "我的借记卡好像丢了")
    c = only_card(r)
    assert (c["title"], c["level"]) == ("锁卡确认", "YELLOW")
    assert "随时解锁" in r["reply"]["text"] and "挂失" in r["reply"]["text"]



def test_rule_mode_fills_in_card_from_earlier_turn(client):
    sid = say(client, "我的信用卡被偷了")["session_id"]  # 先出锁卡确认卡
    c = only_card(say(client, "直接挂失吧", sid))  # 没说哪张卡：沿用上一句的「信用卡」
    assert (c["title"], c["level"], c["fields"]["card_tail"]) == ("挂失确认", "RED", "3721")


def test_lost_card_on_model_path(llm_client, fake):
    f = fake(tool("card_lock_propose"), text("别着急，建议先锁卡。要锁哪张卡？"),
             tool("card_lock_propose", card_hint="借记卡"))
    r = say(llm_client, "我卡好像丢了")
    assert r["reply"]["source"] == "llm" and [x["type"] for x in r["cards"]] == ["card_list"]
    seen = json.loads(f.calls[1]["messages"][-1]["content"])  # 模型拿到的工具结果
    assert seen["status"] == "need_card" and "不要替用户选" in seen["note"]
    assert [x["tail"] for x in seen["candidates"]] == ["6688", "3721"]
    c = only_card(say(llm_client, "借记卡", r["session_id"]))
    assert (c["type"], c["level"]) == ("confirm", "YELLOW")
    assert len(f.calls) == 3  # 出了确认卡就不再调模型写回复
    rows = dict(c["rows"])
    assert "随时解锁" in rows["恢复"] and "挂失" in rows["确定丢了"]  # 卡片上直接提醒还能挂失
    blob = json.dumps([m for call in f.calls for m in call["messages"]], ensure_ascii=False)
    assert DEBIT_NO not in blob and CREDIT_NO not in blob


def test_model_cannot_pick_card_for_user(llm_client, fake):
    """用户没说哪张卡，模型自己填了 card_hint：不认，照样展示卡片列表追问。"""
    fake(tool("card_limit_propose", setting="per_txn", amount_text="2000", card_hint="信用卡"), text("要调哪张卡？"))
    r = say(llm_client, "单笔限额调到2000")
    assert [x["type"] for x in r["cards"]] == ["card_list"]


@pytest.mark.parametrize("utterance,tail", [("把6688锁了", "6688"), ("锁卡3721", "3721"), ("锁定后四位3721的卡", "3721")])
def test_lock_by_tail_only(client, utterance, tail):
    c = only_card(say(client, utterance))
    assert c["type"] == "confirm" and dict(c["rows"])["卡片"].endswith(f"（尾号{tail}）")


def test_lock_unknown_tail_says_not_found(client):
    r = say(client, "把1234锁了")
    assert "没有找到" in json.dumps(r["cards"], ensure_ascii=False)


def test_model_tail_hint_counts_when_user_said_tail(llm_client, fake):
    """用户只说了「6688」，模型填 card_hint="6688"：用户说过这个尾号，算数，直接出确认卡。"""
    fake(tool("card_lock_propose", card_hint="6688"), text("好的"))
    c = only_card(say(llm_client, "帮我瞅瞅6688那张，先锁上"))
    assert c["type"] == "confirm" and "6688" in dict(c["rows"])["卡片"]


def test_card_tools_visible_to_model_but_not_write_tools(llm_client, fake):
    f = fake(text("你好"))
    say(llm_client, "你好")
    names = {t["function"]["name"] for t in f.calls[0]["tools"]}
    assert {"card_list", "card_lock_propose", "card_unlock_propose", "card_report_loss_propose", "card_limit_propose",
            "card_virtual_propose"} <= names
    assert not names & {"card_lock", "card_unlock", "card_report_loss", "card_limit_set", "card_virtual_create"}
    assert "- 临时锁卡（卡丢了先锁卡）：调用 card_lock_propose。" in f.calls[0]["messages"][0]["content"]


# ---------- 执行前重新校验 ----------

def test_pending_lock_invalidated_after_report_loss(client):
    lock = only_card(say(client, "锁借记卡"))
    loss = confirm(client, only_card(say(client, "挂失借记卡"))).json()
    assert pass_auth(client, loss["card"])["status"] == "succeeded"
    out = confirm(client, lock).json()
    assert out["status"] == "invalidated" and "卡片状态已变化" in out["card"]["text"]
    assert card("debit").status == "lost"


def test_pending_limit_invalidated_when_setting_changed(client):
    first = only_card(say(client, "把借记卡单笔限额调到2000"))
    second = only_card(say(client, "把借记卡单笔限额调到3000"))
    assert confirm(client, second).json()["status"] == "succeeded"
    out = confirm(client, first).json()
    assert out["status"] == "invalidated" and "卡片设置已变化" in out["card"]["text"]
    assert card("debit").limits["per_txn_fen"] == 300_000


# ---------- 限额虚拟卡 🟡 ----------

def _apply_virtual(client, utterance="申请一张500元限额的网购虚拟卡"):
    c = only_card(say(client, utterance))
    assert (c["title"], c["level"]) == ("虚拟卡申请确认", "YELLOW")
    out = confirm(client, c).json()
    assert out["status"] == "succeeded"
    return c, out


def test_virtual_card_apply_and_tighten(client):
    c, out = _apply_virtual(client, "申请一张只在淘宝用的一次性虚拟卡，限额两百")
    assert ["限定商户", "淘宝"] in c["rows"] and ["使用次数", "一次性，用一次后失效"] in c["rows"]
    v = card("virtual")
    assert (v.merchant, v.one_time, v.limits["per_txn_fen"], v.limits["atm"]) == ("淘宝", True, 20_000, False)
    assert ["虚拟卡", f"尾号 {v.tail}"] in out["card"]["rows"] and v.card_no not in json.dumps(out, ensure_ascii=False)
    assert confirm(client, c).status_code == 409  # 同一张卡不能用两次
    with new_session() as db:
        assert len(db.scalars(select(Card).where(Card.kind == "virtual")).all()) == 1
    lower = only_card(say(client, "把虚拟卡限额调到100"))
    assert (lower["level"], lower["fields"]["card_tail"]) == ("YELLOW", v.tail)
    assert only_card(say(client, "把虚拟卡限额调到800"))["level"] == "RED"


def test_virtual_card_asks_for_limit(client):
    r = say(client, "开一张虚拟卡")
    assert r["cards"] == [] and "限额" in r["reply"]["text"]
    c = only_card(say(client, "300", r["session_id"]))
    assert c["fields"]["limit"] == "300.00"


def test_virtual_card_limit_cap(client):
    r = say(client, "申请一张6000元的虚拟卡")
    assert only_card(r)["type"] == "notice" and "5,000.00" in r["reply"]["text"]


def test_virtual_card_needs_active_debit_card(client):
    set_card("debit", status="locked")
    assert "不能申请虚拟卡" in only_card(say(client, "申请一张500元的虚拟卡"))["text"]


@pytest.mark.parametrize("args,status", [
    ({"amount_text": "5000"}, "amount_not_from_user"),          # 用户说的是 500
    ({"amount_text": "500", "merchant": "京东"}, "merchant_not_from_user"),  # 用户说的是淘宝
])
def test_virtual_card_params_must_come_from_user(llm_client, fake, args, status):
    fake(tool("card_virtual_propose", **args), text("请再确认一下。"))
    r = say(llm_client, "申请一张500元的淘宝虚拟卡")
    calls = [e for e in llm_client.get("/admin/audit").json() if e["event"] == "tool_call"]
    assert calls[-1]["payload"]["status"] == status
    assert [x["type"] for x in r["cards"]] == ["notice"] and llm_client.get("/admin/actions").json() == []


# ---------- 查卡与脱敏 ----------

def test_card_list(client):
    r = say(client, "查一下我的卡")
    c = only_card(r)
    assert c["type"] == "card_list" and [(x["tail"], x["status_label"]) for x in c["cards"]] == [("6688", "正常"), ("3721", "正常")]
    debit, credit = c["cards"]
    assert ["境外交易", "已关闭"] in debit["rows"] and ["单日限额", "¥30,000.00"] in debit["rows"]
    assert ["可用额度", "¥46,321.50"] in credit["rows"]
    assert client.get("/api/accounts").json()[0]["balance"] == "28,650.32"  # 卡片种子不影响账户余额
    r = say(client, "我信用卡还有多少可用额度")
    assert [x["tail"] for x in only_card(r)["cards"]] == ["3721"] and r["reply"]["text"] == "查到了，状态和限额见卡片。"


def test_card_numbers_never_leave_the_backend(client):
    sid = say(client, "查一下我的卡")["session_id"]
    replies = [say(client, t, sid) for t in ("我卡好像丢了", "借记卡", "关闭信用卡的境外交易")]
    _, out = _apply_virtual(client)
    blob = json.dumps([replies, out, client.get("/admin/audit?limit=500").json(), client.get("/admin/actions").json()],
                      ensure_ascii=False)
    for no in (DEBIT_NO, CREDIT_NO, card("virtual").card_no):
        assert no not in blob and no[-4:] in blob
    assert client.post("/admin/audit/verify").json()["ok"]


# ---------- 规则兜底意图 ----------

@pytest.mark.parametrize("utterance,slots", [
    ("我卡好像丢了", {"op": "lock", "card_hint": "", "lost": True}),
    ("钱包丢了，里面有卡", {"op": "lock", "card_hint": "", "lost": True}),
    ("锁尾号6688的卡", {"op": "lock", "card_hint": "尾号6688"}),
    ("帮我把信用卡锁一下", {"op": "lock", "card_hint": "信用卡"}),
    ("解除挂失", {"op": "unlock", "card_hint": ""}),
    ("挂失借记卡", {"op": "report_loss", "card_hint": "借记卡"}),
    ("把无卡支付关了", {"op": "limit", "setting": "online", "enabled": False, "card_hint": ""}),
    ("恢复境外交易", {"op": "limit", "setting": "overseas", "enabled": True, "card_hint": ""}),
    ("把借记卡尾号6688的单笔限额调到2000",  # 尾号不是金额
     {"op": "limit", "setting": "per_txn", "amount_text": "2000", "card_hint": "借记卡尾号6688"}),
    ("申请一张只在淘宝用的一次性虚拟卡，限额两百",  # 「一张」「一次性」不是金额
     {"op": "virtual_apply", "amount_text": "两百", "merchant": "淘宝", "one_time": True}),
    ("信用卡额度还有多少", {"op": "list", "card_hint": "信用卡"}),
    # 只说尾号、不说卡种
    ("把6688锁了", {"op": "lock", "card_hint": "尾号6688"}),
    ("锁卡 3721", {"op": "lock", "card_hint": "尾号3721"}),
    ("冻结6688那张", {"op": "lock", "card_hint": "尾号6688"}),
    ("锁一下卡号末四位6688的", {"op": "lock", "card_hint": "卡号末四位6688"}),
    ("尾号是3721的卡挂失", {"op": "report_loss", "card_hint": "尾号是3721"}),
    ("解锁6688", {"op": "unlock", "card_hint": "尾号6688"}),
    ("2026年锁的卡帮我解锁", {"op": "unlock", "card_hint": ""}),  # 年份不是尾号
    ("单日限额改成5000", {"op": "limit", "setting": "daily", "amount_text": "5000", "card_hint": ""}),  # 金额不是尾号
])
def test_card_rule_intent(utterance, slots):
    i = rule_intent(utterance)
    assert (i.intent, i.slots) == ("card", slots)


@pytest.mark.parametrize("utterance,intent", [
    ("卡里还剩多少钱", "balance"), ("给我妈转500", "transfer"), ("卡被锁了怎么办", "unknown"), ("借记卡", "unknown"),
])
def test_card_rule_intent_leaves_other_intents_alone(utterance, intent):
    assert rule_intent(utterance).intent == intent
