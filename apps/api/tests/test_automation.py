"""跨场景工具（WS7）：定时转账（创建定级、到期执行前重新校验）、心愿罐到期释放、单步的心愿罐 / 订花 / 总览。规则模式。"""

from datetime import datetime, timedelta

from app import clock
from app.db import new_session
from app.models import PendingAction, ScheduledTransfer, Transaction, WishPot

from .conftest import confirm, say
from .helpers import BALANCE, balance, events, ledger_ok


def ok(client, card: dict) -> dict:
    out = confirm(client, card).json()
    assert out["status"] == "succeeded", out
    return out


def fast_forward(client, to: datetime) -> list[dict]:
    r = client.post("/dev/fast-forward", json={"to": to.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()["events"]


def schedule() -> ScheduledTransfer:
    with new_session() as db:
        st = db.query(ScheduledTransfer).one()
        db.expunge(st)
        return st


# ---------- 定时转账（F-TR-05） ----------

def test_monthly_transfer_created_and_runs(client):
    r = say(client, "每月15号给我妈转500，备注生活费")
    c = r["cards"][0]
    assert c["title"] == "定时转账确认" and c["level"] == "YELLOW" and c["amount"] == "500.00"
    rows = dict(c["rows"])
    assert rows["执行时间"] == "每月 15 日 10:00" and rows["备注"] == "生活费"
    assert balance(client) == BALANCE  # 创建授权不动钱
    ok(client, c)
    st = schedule()
    assert st.next_run_at.day == 15 and st.next_run_at.hour == 10 and st.level == "YELLOW"

    assert fast_forward(client, st.next_run_at - timedelta(hours=1)) == []  # 没到点不执行
    evs = fast_forward(client, st.next_run_at)
    assert len(evs) == 1 and "已按授权执行" in evs[0]["text"] and evs[0]["cards"][0]["type"] == "receipt"
    assert balance(client) == "28,150.32" and ledger_ok(client)
    nxt = schedule()
    assert nxt.next_run_at.day == 15 and nxt.next_run_at.month != st.next_run_at.month
    assert fast_forward(client, st.next_run_at + timedelta(hours=1)) == []  # 重复扫描不会重复转账
    assert balance(client) == "28,150.32"
    assert "execution" in events(client, evs[0]["trace_id"])  # 走的是网关执行，有审计


def test_large_or_new_payee_schedule_is_red(client):
    assert say(client, "每月15号给我妈转2000")["cards"][0]["level"] == "RED"
    c = say(client, "下周五给赵磊转100")["cards"][0]
    assert c["level"] == "RED" and any("从未转过账" in x for x in c["reasons"])


def test_schedule_not_immediate_transfer(client):
    """规则模式下「明天给我妈转500」不是立即转账。"""
    c = say(client, "明天给我妈转500")["cards"][0]
    assert c["title"] == "定时转账确认" and dict(c["rows"])["执行时间"].endswith("10:00")


def test_schedule_escalates_when_riskier(client):
    """到期时当日累计已超 1000：风险比授权时（🟡）高，转为普通确认卡，不自动执行。"""
    r = say(client, "每月15号给我妈转500")
    ok(client, r["cards"][0])
    st = schedule()
    fast_forward(client, st.next_run_at - timedelta(hours=1))
    ok(client, say(client, "给我妹转800", r["session_id"])["cards"][0])
    evs = fast_forward(client, st.next_run_at)
    c = evs[0]["cards"][0]
    assert c["type"] == "confirm" and c["level"] == "RED" and "重新确认" in evs[0]["text"]
    assert balance(client) == "27,850.32"  # 只有手动那笔 800
    assert "mandate_escalated" in events(client, evs[0]["trace_id"])


def test_schedule_blocked_by_kill_switch(client):
    r = say(client, "每月15号给我妈转500")
    ok(client, r["cards"][0])
    st = schedule()
    client.post("/admin/kill-switch", json={"enabled": True, "reason": "演练"})
    evs = fast_forward(client, st.next_run_at)
    assert "没有执行" in evs[0]["text"] and evs[0]["cards"][0]["breaker"]["level"] == "system"
    assert balance(client) == BALANCE
    assert schedule().runs[-1]["status"] == "blocked"


def test_schedule_insufficient_balance(client):
    with new_session() as db:  # 直接造一笔超过余额的授权：只测执行时的余额校验
        db.add(ScheduledTransfer(id="ST1", user_id=1, contact_id=1, account_no="6222021234561234", amount_fen=9_000_000,
                                 recurrence="once", next_run_at=clock.now() + timedelta(days=1), level="RED", runs=[]))
        db.commit()
    evs = fast_forward(client, clock.now() + timedelta(days=1))
    assert "余额不足" in evs[0]["text"] and balance(client) == BALANCE


def test_cancel_schedule(client):
    r = say(client, "每月15号给我妈转500")
    ok(client, r["cards"][0])
    c = say(client, "取消定时转账", r["session_id"])["cards"][0]
    assert c["title"] == "取消定时转账" and c["level"] == "YELLOW"
    ok(client, c)
    assert schedule().status == "cancelled"
    assert fast_forward(client, clock.now() + timedelta(days=40)) == []


# ---------- 心愿罐 ----------

def test_pot_lock_and_auto_release(client):
    r = say(client, "存500进心愿罐，下周五妈妈生日用")
    c = r["cards"][0]
    assert c["title"] == "存入心愿罐" and c["level"] == "YELLOW" and "到期日" in dict(c["rows"])
    ok(client, c)
    accts = client.get("/api/accounts").json()
    assert [a["balance"] for a in accts] == ["28,150.32", "500.00"]  # 钱还在名下，只是划到了子账户
    bal = say(client, "我还有多少钱", r["session_id"])["cards"][0]
    assert bal["total"] == BALANCE
    with new_session() as db:
        until = db.query(WishPot).one().until
    evs = fast_forward(client, datetime.combine(until + timedelta(days=1), datetime.min.time()))
    assert "已到期" in evs[0]["text"] and evs[0]["cards"][0]["type"] == "receipt"
    assert balance(client) == BALANCE and ledger_ok(client)


def test_pot_lock_amount_must_be_said(client):
    r = say(client, "把钱存进心愿罐")
    assert r["cards"][0]["type"] == "notice"


def test_pot_lock_insufficient(client):
    r = say(client, "存50000进心愿罐")
    assert r["cards"][0]["type"] == "notice" and "余额不足" in r["cards"][0]["text"]


def test_pot_not_in_bill(client):
    """心愿罐存入是内部划转，不算消费。"""
    r = say(client, "存500进心愿罐")
    ok(client, r["cards"][0])
    with new_session() as db:  # 账单分析不统计 channel = system 的流水
        rows = db.query(Transaction).filter_by(category="心愿罐").all()
        assert len(rows) == 1 and rows[0].channel == "system"


def test_single_order_from_pot(client):
    r = say(client, "存500进心愿罐")
    ok(client, r["cards"][0])
    r = say(client, "帮我订一束花送我妈", r["session_id"])
    c = r["cards"][0]
    assert c["title"] == "订单确认" and dict(c["rows"])["付款方式"] == "心愿罐「心愿」" and "康乃馨" in r["reply"]["text"]
    ok(client, c)
    r = say(client, "我设了哪些自动任务", r["session_id"])
    a = r["cards"][0]
    assert a["type"] == "automation" and a["pots"][0]["balance"] == "301.00" and len(a["orders"]) == 1
    c = say(client, "把花的订单取消", r["session_id"])["cards"][0]
    assert c["title"] == "取消订单"
    ok(client, c)
    assert ledger_ok(client)


def test_order_debit_is_red(client):
    """没有心愿罐时从活期付款：不在一键确认的范围内，升 🔴。"""
    c = say(client, "帮我订个蛋糕")["cards"][0]
    assert c["level"] == "RED" and any("心愿罐" in x for x in c["reasons"])


def test_handoff_without_plan(client):
    r = say(client, "转人工")
    assert r["cards"][0]["handoff"]["ticket"]
    blocked = say(client, "存500进心愿罐", r["session_id"])["cards"][0]
    assert blocked["breaker"]["level"] == "handoff"


def test_pending_actions_carry_session(client):
    r = say(client, "存500进心愿罐")
    with new_session() as db:
        assert db.query(PendingAction).one().session_id == r["session_id"]


def test_undo_lock_before_confirm_cancels_card(client):
    r = say(client, "存500进心愿罐")
    c = r["cards"][0]
    r = say(client, "撤销锁定", r["session_id"])
    assert r["cards"][0]["type"] == "notice" and "没有发生资金变动" in r["reply"]["text"]
    assert confirm(client, c).status_code == 409 and balance(client) == BALANCE
