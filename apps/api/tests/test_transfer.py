"""WS1 转账补全：手机号转账（F-TR-02）、AA 拆分（F-TR-06）、反诈冷静期（F-TR-08）、滚动 24 小时、拆单识别。

规则兜底模式为主；涉及「模型看到什么」的用例用脚本化的假模型。
"""

import json
from datetime import datetime, time, timedelta

import pytest

from app import clock
from app.db import new_session
from app.models import PendingAction
from app.nlu import fraud, resolve_payee
from app.policy import PolicyContext, Risk, evaluate
from app.tools.transfer import plan_aa, split_aa

from .conftest import confirm, say, text, tool
from .test_flows import balance, ledger_ok, pass_auth

ROLLING = "近 24 小时累计转出将超过 1000 元（跨零点累计，含待确认金额）"
SPLIT = "疑似拆单：1 小时内多笔小额转给同一收款人，合计超过 1000 元"
FRAUD = "对话中出现疑似诈骗话术，已启动冷静期，请先核实对方身份"


def audit_events(client, event: str) -> list[dict]:
    return [e["payload"] for e in client.get("/admin/audit?limit=500").json() if e["event"] == event]


def travel_to_next_day(minute: int = 1) -> None:
    t = clock.now()
    clock.travel(datetime.combine(t.date() + timedelta(days=1), time(0, minute)) - t)


# ---------- F-TR-02 手机号转账 ----------

def test_phone_of_saved_contact_is_yellow(client):
    card = say(client, "给13912345678转200")["cards"][0]
    assert card["type"] == "confirm" and card["level"] == "YELLOW"
    assert card["fields"]["payee"] == "王*英" and card["fields"]["phone"] == "139****5678"
    assert card["rows"][:3] == [["收款人", "王*英"], ["手机号", "139****5678"], ["收款银行", "工商银行 · 尾号 1234"]]


def test_phone_new_payee_red_then_saved(client):
    card = say(client, "给13687654321转200，备注还钱")["cards"][0]
    assert card["level"] == "RED" and "首次向该收款人转账" in card["reasons"]
    assert card["fields"]["payee"] == "*明" and card["fields"]["tail"] == "4321"  # 脱敏户名让用户核对
    assert ["说明", "按手机号查到的新收款人，请核对户名；转账成功后会保存到收款人"] in card["rows"]
    done = pass_auth(client, confirm(client, card).json()["card"])
    assert done["status"] == "succeeded" and balance(client) == "28,450.32"
    ledger_ok(client)
    # 保存为收款人后，按姓名能找到，而且不再是新收款人
    again = say(client, "给刘明转100")["cards"][0]
    assert again["level"] == "YELLOW" and again["fields"]["tail"] == "4321"


def test_phone_never_in_audit_in_clear(client):
    card = say(client, "给13687654321转200")["cards"][0]
    pass_auth(client, confirm(client, card).json()["card"])
    dump = json.dumps(client.get("/admin/audit?limit=500").json(), ensure_ascii=False)
    assert "13687654321" not in dump and "136****4321" in dump
    assert client.post("/admin/audit/verify").json()["ok"]


def test_masked_phone_matching_two_numbers_asks_again(llm_client, fake):
    fake(tool("transfer_propose", payee_hint="136****4321", amount_text="200"), text("请说完整号码。"))
    r = say(llm_client, "13687654321和13600004321哪个是小刘的？给136****4321转200")
    assert r["cards"][0]["type"] == "notice" and "尾号相同" in r["cards"][0]["text"]


def test_unknown_phone(client):
    c = say(client, "给13700000000转100")["cards"][0]
    assert c["type"] == "notice" and "137****0000 没有绑定" in c["text"]


def test_phone_amount_follow_up(client):
    r = say(client, "给13687654321转钱")
    assert r["reply"]["text"] == "要转多少钱？" and not r["cards"]
    card = say(client, "三百", r["session_id"])["cards"][0]
    assert card["type"] == "confirm" and card["amount"] == "300.00" and card["fields"]["payee"] == "*明"


def test_model_only_sees_masked_phone(llm_client, fake):
    f = fake(tool("transfer_propose", payee_hint="136****4321", amount_text="200"), text("请核对确认卡，这是新收款人。"))
    r = say(llm_client, "给13687654321转200")
    assert r["cards"][0]["level"] == "RED" and r["cards"][0]["fields"]["payee"] == "*明"
    seen = json.dumps([c["messages"] for c in f.calls], ensure_ascii=False)
    assert "13687654321" not in seen and "136****4321" in seen  # 用户原话和工具结果都只有脱敏号码


def test_model_invented_phone_rejected(llm_client, fake):
    fake(tool("transfer_propose", payee_hint="13687654321", amount_text="200"), text("请再说一遍号码。"))
    r = say(llm_client, "给尾号4321的那个人转200")
    assert r["cards"][0]["type"] == "notice" and "完整手机号" in r["cards"][0]["text"]
    assert audit_events(llm_client, "provenance_reject")[0]["field"] == "phone"


# ---------- F-TR-06 AA 拆分 ----------

@pytest.mark.parametrize("total,n,absorb,expect", [
    (10_000, 3, False, [3334, 3333, 3333]),   # PRD 例子：100 元 3 人
    (10_000, 3, True, [3333, 3333, 3334]),    # 尾差算在最后一位（发起人）
    (85_600, 4, False, [21_400] * 4),          # 正好除尽
    (5, 3, False, [2, 2, 1]),
    (10_001, 4, False, [2501, 2500, 2500, 2500]),
])
def test_split_aa(total, n, absorb, expect):
    assert split_aa(total, n, absorb_last=absorb) == expect


def test_split_aa_always_sums_to_total():
    for total in range(1, 3000, 7):
        for n in range(1, 12):
            if total < n:
                continue
            for absorb in (False, True):
                parts = split_aa(total, n, absorb_last=absorb)
                assert sum(parts) == total and max(parts) - min(parts) <= (n - 1 if absorb else 1)


def test_plan_aa_remainder_text():
    shares, rule_text, rule = plan_aa(10_000, ["小王", "小李"], include_me=True, me_absorbs=False)
    assert shares == [["小王", 3334, False], ["小李", 3333, False], ["你", 3333, True]]
    assert rule == "front" and rule_text == "100.00 元 ÷ 3 人 = 每人 33.33 元，余 0.01 元按名单顺序由小王多付 0.01 元"
    _, rule_text, _ = plan_aa(10_002, ["小王", "小李", "小张"], include_me=True, me_absorbs=False)
    assert rule_text.endswith("余 0.02 元按名单顺序由小王、小李各多付 0.01 元")
    shares, rule_text, rule = plan_aa(10_000, ["小王", "小李"], include_me=True, me_absorbs=True)
    assert [a for _, a, _ in shares] == [3333, 3333, 3334] and rule == "initiator" and rule_text.endswith("余 0.01 元由你承担")
    shares, rule_text, _ = plan_aa(10_000, ["小王", "小李", "小张"], include_me=False, me_absorbs=True)
    assert [a for _, a, _ in shares] == [3333] * 3 and rule_text.endswith("由你承担")  # 本人不分摊：零头不向对方收


def test_aa_end_to_end(client):
    r = say(client, "昨晚火锅856，我和小王、小李、小张AA")
    card = r["cards"][0]
    assert card["type"] == "confirm" and card["level"] == "YELLOW" and card["title"] == "AA 收款确认"
    assert "amount" not in card and "today_after" not in card  # 收款不是资金转出
    assert card["rows"] == [
        ["事由", "火锅"], ["总额", "¥856.00"], ["小王", "¥214.00"], ["小李", "¥214.00"], ["小张", "¥214.00"],
        ["你自己", "¥214.00（自付，不发起收款）"], ["尾差", "856.00 元 ÷ 4 人 = 每人 214.00 元，正好除尽，没有尾差"],
        ["收款", "确认后向 3 人发送收款请求，共 ¥642.00"],
    ]
    out = confirm(client, card).json()
    assert out["status"] == "succeeded" and out["card"]["status_label"] == "发起 AA 收款成功"
    assert balance(client) == "28,650.32"

    st = say(client, "AA收款收齐了吗", r["session_id"])
    aa = st["cards"][0]
    assert st["reply"]["text"] == "「火锅」AA 收款：0/3 人已付清。"
    assert aa["type"] == "aa" and aa["status_label"] == "已发送" and aa["expected"] == "642.00"
    assert [(s["name"], s["amount"], s["status_label"]) for s in aa["shares"]] == [
        ("小王", "214.00", "已发送"), ("小李", "214.00", "已发送"), ("小张", "214.00", "已发送"), ("你自己", "214.00", "本人")]

    for s in aa["shares"][:3]:  # 模拟三位朋友付款
        last = client.post(f"/dev/aa/shares/{s['id']}/pay").json()["card"]
    assert last["status_label"] == "已付清" and last["received"] == "642.00"
    assert client.post(f"/dev/aa/shares/{aa['shares'][0]['id']}/pay").status_code == 200  # 重复付款不重复入账
    assert balance(client) == "29,292.32"
    ledger_ok(client)
    assert say(client, "火锅AA谁还没付", r["session_id"])["reply"]["text"] == "「火锅」AA 收款：3/3 人已付清。"


def test_aa_remainder_goes_to_first_and_card_matches_args(client):
    r = say(client, "100块我和小王、小李AA")
    card = r["cards"][0]
    assert card["rows"][:5] == [  # 没说事由就不显示事由行
        ["总额", "¥100.00"], ["小王", "¥33.34"], ["小李", "¥33.33"], ["你自己", "¥33.33（自付，不发起收款）"],
        ["尾差", "100.00 元 ÷ 3 人 = 每人 33.33 元，余 0.01 元按名单顺序由小王多付 0.01 元"],
    ]
    with new_session() as db:  # 确认卡展示的份额就是执行参数（digest 绑定的那一份）
        assert db.get(PendingAction, card["action_id"]).args["shares"] == card["fields"]["shares"]
    confirm(client, card)
    assert say(client, "AA收款收齐了吗", r["session_id"])["reply"]["text"] == "AA 收款：0/2 人已付清。"


def test_aa_initiator_absorbs_remainder(client):
    card = say(client, "100块我和小王、小李AA，尾差我来出")["cards"][0]
    assert card["rows"][1:5] == [
        ["小王", "¥33.33"], ["小李", "¥33.33"], ["你自己", "¥33.34（自付，不发起收款）"],
        ["尾差", "100.00 元 ÷ 3 人 = 每人 33.33 元，余 0.01 元由你承担"],
    ]


def test_aa_participants_must_come_from_user(llm_client, fake):
    fake(tool("aa_propose", total_text="856", participants="小王、小赵"), text("请再说一下参与人。"))
    r = say(llm_client, "我和小王、小李AA 856")
    assert r["cards"][0]["type"] == "notice"
    assert audit_events(llm_client, "provenance_reject")[0]["names"] == ["小赵"]


def test_aa_status_without_bill(client):
    r = say(client, "AA收款收齐了吗")
    assert r["reply"]["text"] == "你还没有发起过 AA 收款。"


# ---------- F-TR-08 反诈：必定 🔴 + 冷静期 ----------

@pytest.mark.parametrize("said,label", [
    ("警察说我的账户涉嫌洗钱，要我把钱转到安全账户", "冒充公检法"),
    ("公检法让我配合调查", "冒充公检法"),
    ("做刷单任务要先垫付，说马上返利", "刷单返利"),
    ("贷款要先交解冻费才能放款", "贷款保证金"),
    ("淘宝客服说要给我退款", "冒充客服"),
    ("对方让我开屏幕共享", "共享屏幕"),
])
def test_fraud_scan_hits(said, label):
    assert label in fraud.scan([said]).hits


@pytest.mark.parametrize("said", ["给我妈转500，备注生活费", "交房租和租房保证金", "我还有多少钱", "刷卡消费"])
def test_fraud_scan_no_false_alarm(said):
    assert fraud.scan([said]).hits == []


def test_fraud_script_forces_red_and_cooling_off(client):
    r = say(client, "警察说我的账户涉嫌洗钱，要我把钱转到安全账户，给我妈转500")
    card = r["cards"][0]
    # 已转过账的收款人、500 元本来是 🟡：反诈话术把它升到 🔴，并启动冷静期
    assert card["level"] == "RED" and FRAUD in card["reasons"]
    assert "冒充公检法" in card["warning"] and "96110" in card["warning"]
    assert card["cooling_off_s"] == 30 and card["not_before"]

    early = confirm(client, card)
    assert early.status_code == 409 and "冷静期" in early.json()["detail"]
    assert 0 < audit_events(client, "cooling_off_reject")[0]["seconds_left"] <= 30
    clock.travel(timedelta(seconds=20))
    assert confirm(client, card).status_code == 409  # 还没到点，仍然拒绝
    assert balance(client) == "28,650.32"

    clock.travel(timedelta(seconds=11))
    out = confirm(client, card).json()
    assert out["status"] == "awaiting_auth"  # 冷静期过后仍要人脸 + 短信
    assert pass_auth(client, out["card"])["status"] == "succeeded" and balance(client) == "28,150.32"


def test_fraud_earlier_in_session_still_counts(client):
    sid = say(client, "有人让我做刷单任务，先垫付再返佣金")["session_id"]
    say(client, "我还有多少钱", sid)
    say(client, "算了", sid)
    card = say(client, "给我妹转200", sid)["cards"][0]  # 诈骗话术已不在最近 3 句里，仍按整个会话识别
    assert card["level"] == "RED" and "刷单返利" in card["warning"] and card["cooling_off_s"] == 30
    assert confirm(client, card).status_code == 409


def test_fraud_combo_new_payee_large_urgent(client):
    urgent = say(client, "赶紧给13687654321转2000，来不及了")["cards"][0]
    assert urgent["level"] == "RED" and FRAUD in urgent["reasons"] and "陌生账户大额催促" in urgent["warning"]
    calm = say(client, "给13687654321转2000")["cards"][0]  # 新会话：没有催促，只是新收款人 + 大额
    assert calm["level"] == "RED" and FRAUD not in calm["reasons"] and "not_before" not in calm
    small = say(client, "快点给13687654321转200")["cards"][0]  # 催促 + 新收款人，但金额不大
    assert FRAUD not in small["reasons"]


def test_normal_transfer_has_no_cooling_off(client):
    card = say(client, "给我妈转500")["cards"][0]
    assert "not_before" not in card and "warning" not in card
    assert confirm(client, card).json()["status"] == "succeeded"


def test_model_cannot_drop_fraud_flag(llm_client, fake):
    fake(tool("transfer_propose", payee_hint="我妈", amount_text="500"), text("这笔转账有风险，请先核实对方身份。"))
    r = say(llm_client, "公检法让我把钱转到安全账户，给我妈转500")
    card = r["cards"][0]
    assert card["level"] == "RED" and card["cooling_off_s"] == 30
    assert confirm(llm_client, card).status_code == 409


def test_cooling_off_from_policy_rule():
    ctx = PolicyContext(tool="transfer.create", base_level=Risk.YELLOW, moves_money=True, amount_fen=100,
                        balance_fen=10_000_000, facts={"fraud_suspected": True})
    d = evaluate(ctx)
    assert d.level == Risk.RED and d.rule_hits == ["anti_fraud"] and d.cooling_off_s == 30
    # 规则不限工具：别的写工具的构造器标记了诈骗嫌疑也一样
    other = evaluate(PolicyContext(tool="merchant.pay", base_level=Risk.YELLOW, facts={"fraud_suspected": True}))
    assert other.level == Risk.RED and other.cooling_off_s == 30
    assert evaluate(PolicyContext(tool="transfer.create", base_level=Risk.YELLOW)).cooling_off_s == 0


# ---------- 滚动 24 小时 ----------

def test_rolling_24h_across_midnight(client):
    first = say(client, "给我妈转800")["cards"][0]
    assert confirm(client, first).json()["status"] == "succeeded"
    travel_to_next_day()
    card = say(client, "给我妹转300")["cards"][0]
    # 自然日累计只有 300（还是 🟡 的范围），但 24 小时内累计 1100
    assert card["level"] == "RED" and card["reasons"] == [ROLLING] and card["today_after"] == "300.00"
    client.post(f"/api/actions/{card['action_id']}/cancel")
    clock.travel(timedelta(days=1))
    assert say(client, "给我妹转300")["cards"][0]["level"] == "YELLOW"  # 出了 24 小时窗口


def test_rolling_rule_does_not_repeat_same_day_cumulative(client):
    say(client, "给我妈转800")
    card = say(client, "给我妹转300")["cards"][0]
    assert card["level"] == "RED" and ROLLING not in card["reasons"] and "1,100.00" in card["reasons"][0]


# ---------- 拆单识别 ----------

def test_split_to_same_payee(client):
    for _ in range(2):
        assert confirm(client, say(client, "给我妈转400")["cards"][0]).json()["status"] == "succeeded"
    card = say(client, "给我妈转300")["cards"][0]
    assert card["level"] == "RED" and SPLIT in card["reasons"]
    assert "split_evasion" in audit_events(client, "policy_decision")[0]["rule_hits"]


def test_split_needs_same_payee(client):
    say(client, "给我妈转400")
    say(client, "给我妹转400")
    card = say(client, "给我妈转300")["cards"][0]
    assert card["level"] == "RED" and SPLIT not in card["reasons"]  # 累计规则照样升 🔴，但不算拆单


def test_split_across_midnight_caught(client):
    """23:40 起连转两笔给同一人，零点后再转：当日累计看不出来，滚动 24 小时和拆单规则都会命中。"""
    travel_to_next_day()
    clock.travel(timedelta(hours=23, minutes=39))
    for _ in range(2):
        assert confirm(client, say(client, "给我妈转450")["cards"][0]).json()["status"] == "succeeded"
    clock.travel(timedelta(minutes=25))
    card = say(client, "给我妈转200")["cards"][0]
    assert card["level"] == "RED" and card["reasons"] == [ROLLING, SPLIT] and card["today_after"] == "200.00"
    assert audit_events(client, "policy_decision")[0]["rule_hits"] == ["rolling_24h_outflow", "split_evasion"]


@pytest.mark.parametrize("facts,level", [
    ({"split_suspected": True}, Risk.RED),
    ({"rolling_24h_over": True}, Risk.RED),
    ({"split_suspected": False, "rolling_24h_over": False, "fraud_suspected": False}, Risk.YELLOW),
])
def test_new_rules_table(facts, level):
    ctx = PolicyContext(tool="transfer.create", base_level=Risk.YELLOW, moves_money=True, amount_fen=100,
                        balance_fen=10_000_000, facts=facts)
    assert evaluate(ctx).level == level


# ---------- 同名收款人：打字回答选择卡 ----------

@pytest.mark.parametrize(("said", "hint", "tail"), [
    ("同事那个", "同事张伟", "0921"), ("大学同学那个", "张伟（大学同学）", "3307"), ("尾号0921的", "尾号0921", "0921"),
    ("尾号3307的", "张伟尾号3307的", "3307"),
    ("同事那个", "张伟", "0921"), ("尾号3307的", "张伟", "3307"),  # 模型只传「张伟」：按用户这句原话再筛
])
def test_same_name_typed_answer(llm_client, fake, said, hint, tail):
    # 同名选择卡后用户打字回答，不点按钮：真实模型有时把备注 / 尾号拼进 payee_hint，有时只传「张伟」
    fake(tool("transfer_propose", payee_hint="张伟", amount_text="200"),
         tool("transfer_propose", payee_hint=hint, amount_text="200"))
    r = say(llm_client, "给张伟转200")
    assert r["cards"][0]["type"] == "clarify"
    card = say(llm_client, said, r["session_id"])["cards"][0]
    assert card["type"] == "confirm" and card["fields"]["tail"] == tail


def test_same_name_answer_without_detail_asks_again(llm_client, fake):
    fake(tool("transfer_propose", payee_hint="张伟", amount_text="200"),
         tool("transfer_propose", payee_hint="张伟", amount_text="200"))
    r = say(llm_client, "给张伟转200")
    assert say(llm_client, "就那个张伟", r["session_id"])["cards"][0]["type"] == "clarify"  # 没说是哪个：不替用户选


def test_same_name_without_detail_still_asks(client):
    with new_session() as db:
        assert len(resolve_payee(db, 1, "张伟")) == 2
        assert len(resolve_payee(db, 1, "张伟尾号9999")) == 2  # 尾号对不上：照旧追问，不替用户选
        assert resolve_payee(db, 1, "尾号9999") == []
