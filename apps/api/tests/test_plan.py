"""跨场景联动（WS7）端到端：生日用例的计划 DAG、心愿罐、时间快进触发临期询价下单、「蛋糕不要了」重新规划、
「撤销锁定」释放心愿罐、整体回退（补偿）、转人工。规则模式，不依赖模型。"""

from app.db import new_session
from app.models import Account, MerchantOrder, PendingAction, TaskPlan, WishPot

from .conftest import confirm, say
from .helpers import BALANCE, balance, events, ledger_ok, pass_auth

BIRTHDAY = "下周五我妈生日，帮我订束花和蛋糕，再存 500 进心愿罐"


def plan_of(r: dict) -> dict:
    return next(c for c in r["cards"] if c["type"] == "plan")


def card(r: dict, type_: str = "confirm") -> dict:
    return next(c for c in r["cards"] if c["type"] == type_)


def op(client, sid: str, plan: dict, name: str) -> dict:
    return say(client, "", sid, selection={"type": "plan", "plan_id": plan["plan_id"], "op": name})


def sync(client, sid: str) -> dict:
    r = client.post("/api/plans/sync", json={"session_id": sid})
    assert r.status_code == 200, r.text
    return r.json()


def fast_forward(client, to: str) -> list[dict]:
    r = client.post("/dev/fast-forward", json={"to": to})
    assert r.status_code == 200, r.text
    return r.json()["events"]


def statuses(plan: dict) -> dict[str, str]:
    return {s["title"]: s["status"] for s in plan["steps"]}


def ok(client, confirm_card: dict) -> dict:
    out = confirm(client, confirm_card).json()
    assert out["status"] == "succeeded", out
    return out


def pot_balance(pot_id: str | None = None) -> int:
    with new_session() as db:
        pot = db.get(WishPot, pot_id) if pot_id else db.query(WishPot).one()
        return db.get(Account, pot.account_id).balance_fen


def start_birthday(client) -> tuple[str, dict]:
    """说出生日请求 → 确认计划 → 确认存入心愿罐 → 同步：计划进入「等待临期执行」。"""
    r = say(client, BIRTHDAY)
    sid, plan = r["session_id"], plan_of(r)
    r = op(client, sid, plan, "confirm")
    ok(client, card(r))
    s = sync(client, sid)
    return sid, s["cards"][0]


# ---------- 主链路 ----------

def test_birthday_plan_card(client):
    r = say(client, BIRTHDAY)
    plan = plan_of(r)
    assert r["reply"]["source"] == "rule" and plan["status"] == "draft" and plan["ops"] == ["confirm", "cancel"]
    titles = [s["title"] for s in plan["steps"]]
    assert titles == ["存 ¥500.00 进心愿罐（妈妈生日）", "询价：鲜花（花漾花艺）", "下单鲜花，从心愿罐付款",
                      "询价：蛋糕（麦香烘焙）", "下单蛋糕，从心愿罐付款"]
    assert [s["risk"] for s in plan["steps"]] == ["🟡 需你确认", "🟢 自动", "🟡 需你确认", "🟢 自动", "🟡 需你确认"]
    assert all(s["at_label"].startswith("临期") for s in plan["steps"][1:])  # 询价下单在生日前 2 天
    assert plan["due_label"].endswith("10:00") and "确认计划不等于确认付款" in plan["hint"]
    assert balance(client) == BALANCE  # 出计划卡什么都不办
    assert "plan_created" in events(client, r["trace_id"])


def test_birthday_full_flow(client):
    r = say(client, BIRTHDAY)
    sid, plan = r["session_id"], plan_of(r)
    r = op(client, sid, plan, "confirm")
    pot_card = card(r)
    assert plan_of(r)["status"] == "running" and pot_card["title"] == "存入心愿罐" and pot_card["level"] == "YELLOW"
    assert balance(client) == BALANCE  # 确认计划 ≠ 确认存入：点确认卡之前钱不动
    assert ok(client, pot_card)["card"]["rows"][-1] == ["活期余额", "¥28,150.32"]

    s = sync(client, sid)
    plan = s["cards"][0]
    assert plan["status"] == "waiting" and "11-0" in s["text"] and pot_balance() == 50_000
    assert sync(client, sid)["cards"] == []  # 没有新进展时不重复推送

    evs = fast_forward(client, plan["due_at"])  # 演示：快进到生日前 2 天 10:00
    assert len(evs) == 1 and "快到了" in evs[0]["text"]
    flower = card(evs[0])
    assert flower["title"] == "订单确认" and flower["level"] == "YELLOW" and flower["amount"] == "199.00"
    assert dict(flower["rows"])["付款方式"] == "心愿罐「妈妈生日」"
    assert "康乃馨" in dict(flower["rows"])["商品"]
    assert "花漾花艺" in plan_of(evs[0])["steps"][1]["detail"]
    ok(client, flower)

    s = sync(client, sid)
    cake = card(s)
    assert cake["amount"] == "168.00" and dict(cake["rows"])["付款方式"] == "心愿罐「妈妈生日」"
    ok(client, cake)
    s = sync(client, sid)
    assert s["cards"][0]["status"] == "done" and pot_balance() == 50_000 - 19_900 - 16_800
    with new_session() as db:
        assert [o.item for o in db.query(MerchantOrder).order_by(MerchantOrder.created_at)] == \
               ["康乃馨花束（19 枝）", "6 寸长寿桃蛋糕"]
    assert balance(client) == "28,150.32"  # 活期只少了存进心愿罐的 500
    assert ledger_ok(client)


def test_drop_cake_before_due(client):
    """计划还没确认时说「蛋糕不要了」：重新规划出第 2 版，蛋糕的询价和下单移出计划。"""
    r = say(client, BIRTHDAY)
    sid = r["session_id"]
    r = say(client, "蛋糕不要了", sid)
    plan = plan_of(r)
    assert plan["version"] == 2 and "去掉了蛋糕" in plan["note"] and plan["status"] == "draft"
    st = statuses(plan)
    assert st["询价：蛋糕（麦香烘焙）"] == st["下单蛋糕，从心愿罐付款"] == "removed"
    assert st["下单鲜花，从心愿罐付款"] == "pending"
    r = op(client, sid, plan, "confirm")
    ok(client, card(r))
    plan = sync(client, sid)["cards"][0]
    evs = fast_forward(client, plan["due_at"])
    ok(client, card(evs[0]))
    assert sync(client, sid)["cards"][0]["status"] == "done"
    assert pot_balance() == 50_000 - 19_900


def test_drop_cake_while_awaiting_confirm(client):
    """蛋糕的确认卡已经出了还没点：作废那张确认卡，计划结束，蛋糕卡再点也不会付款。"""
    sid, plan = start_birthday(client)
    ok(client, card(fast_forward(client, plan["due_at"])[0]))
    cake = card(sync(client, sid))
    r = say(client, "蛋糕不要了", sid)
    assert plan_of(r)["status"] == "done" and statuses(plan_of(r))["下单蛋糕，从心愿罐付款"] == "removed"
    assert plan_of(r)["void_action_ids"] == [cake["action_id"]]  # 前端据此把画面上那张蛋糕卡盖上遮罩
    assert confirm(client, cake).status_code == 409  # 已作废
    assert pot_balance() == 50_000 - 19_900


def test_drop_paid_cake_compensates(client):
    """蛋糕已经付款后说「蛋糕不要了」：追加补偿步骤，取消订单、退款回心愿罐（同样要确认）。"""
    sid, plan = start_birthday(client)
    ok(client, card(fast_forward(client, plan["due_at"])[0]))
    ok(client, card(sync(client, sid)))
    assert sync(client, sid)["cards"][0]["status"] == "done"
    r = say(client, "蛋糕不要了", sid)
    plan = plan_of(r)
    assert plan["version"] == 2 and "取消并退款" in plan["note"]
    cancel = card(r)
    assert cancel["title"] == "取消订单" and dict(cancel["rows"])["退款去向"] == "心愿罐「妈妈生日」"
    assert pot_balance() == 50_000 - 19_900 - 16_800
    ok(client, cancel)
    assert sync(client, sid)["cards"][0]["status"] == "done"
    assert pot_balance() == 50_000 - 19_900
    with new_session() as db:
        assert {o.item: o.status for o in db.query(MerchantOrder)} == {"康乃馨花束（19 枝）": "paid", "6 寸长寿桃蛋糕": "cancelled"}
    assert ledger_ok(client)


def test_undo_lock_while_waiting(client):
    """「撤销刚才的锁定」：释放心愿罐（确认卡），还没下的订单移出计划，计划结束；钱一分不差回到活期。"""
    sid, plan = start_birthday(client)
    assert balance(client) == "28,150.32"
    r = say(client, "撤销刚才的锁定", sid)
    release = card(r)
    assert release["title"] == "释放心愿罐" and release["amount"] == "500.00"
    assert plan_of(r)["status"] == "rolling_back"
    ok(client, release)
    plan = sync(client, sid)["cards"][0]
    assert plan["status"] == "cancelled"
    assert all(v in ("removed", "done", "cancelled") for v in statuses(plan).values())
    assert balance(client) == BALANCE and ledger_ok(client)
    with new_session() as db:
        assert db.query(WishPot).one().status == "released"
    assert fast_forward(client, plan["due_at"]) == []  # 计划已撤销，临期也不会再询价


def test_undo_lock_without_plan(client):
    r = say(client, "存500进心愿罐")
    ok(client, card(r))
    r = say(client, "撤销锁定", r["session_id"])
    ok(client, card(r))
    assert balance(client) == BALANCE


def test_cancel_whole_plan_rolls_back_in_reverse(client):
    """整体回退（Saga）：逆序补偿，先取消两笔订单（退款回心愿罐），再释放心愿罐。每一步补偿都单独确认。"""
    sid, plan = start_birthday(client)
    ok(client, card(fast_forward(client, plan["due_at"])[0]))
    ok(client, card(sync(client, sid)))
    sync(client, sid)
    r = say(client, "整个计划都取消吧", sid)
    titles = []
    c = card(r)
    for _ in range(3):
        titles.append(c["title"])
        ok(client, c)
        s = sync(client, sid)
        if s["cards"][0]["status"] == "cancelled":
            break
        c = card(s)
    assert titles == ["取消订单", "取消订单", "释放心愿罐"]
    assert balance(client) == BALANCE and ledger_ok(client)


def test_handoff_pauses_writes(client):
    r = say(client, BIRTHDAY)
    sid = r["session_id"]
    pot_card = card(op(client, sid, plan_of(r), "confirm"))
    r = say(client, "我要转人工", sid)
    notice = card(r, "notice")
    assert notice["handoff"]["ticket"].startswith("H") and "人工客服" in r["reply"]["text"]
    assert plan_of(r)["status"] == "handoff"
    out = confirm(client, pot_card).json()  # 转人工前出的确认卡，确认时作废
    assert out["status"] == "invalidated" and balance(client) == BALANCE
    blocked = say(client, "给我妈转100", sid)["cards"][0]
    assert blocked["type"] == "notice" and blocked["breaker"]["level"] == "handoff"
    assert "handoff_requested" in events(client, r["trace_id"])
    trip = client.get("/admin/risk-events").json()["trips"][0]
    assert trip["trigger"] == "user_handoff" and trip["status"] == "active"
    client.post(f"/admin/breaker/trips/{trip['id']}/release", json={"note": "坐席处理完毕"})
    assert say(client, "给我妈转100", sid)["cards"][0]["type"] == "confirm"  # 坐席交还后恢复


def test_plan_step_still_red_when_risky(client):
    """确认了计划，单步该强验证的照样要强验证：今日已转 900，再从活期付 199 → 🔴 人脸 + 短信。"""
    r = say(client, "给我妈转900")
    sid = r["session_id"]
    ok(client, card(r))
    r = say(client, "帮我订束花和蛋糕", sid)
    plan = plan_of(r)
    assert [s["title"] for s in plan["steps"]][1] == "下单鲜花"
    r = op(client, sid, plan, "confirm")
    flower = card(r)
    assert flower["level"] == "RED" and dict(flower["rows"])["付款方式"].startswith("Ⅰ类借记卡")
    assert "强验证" in plan_of(r)["steps"][1]["risk"] and "人脸" in r["reply"]["text"]
    out = confirm(client, flower).json()
    assert out["status"] == "awaiting_auth"
    assert pass_auth(client, out["card"])["status"] == "succeeded"
    assert card(sync(client, sid))["level"] == "RED"


def test_expired_step_can_retry(client):
    from datetime import timedelta

    from app import clock

    r = say(client, BIRTHDAY)
    sid = r["session_id"]
    op(client, sid, plan_of(r), "confirm")
    clock.travel(timedelta(minutes=10))  # 确认卡过期
    r = say(client, "继续", sid)
    pot_card = card(r)
    assert pot_card["title"] == "存入心愿罐"  # 重新出卡
    with new_session() as db:
        assert db.query(PendingAction).filter_by(status="expired").count() == 1


def test_plan_cancel_draft(client):
    r = say(client, BIRTHDAY)
    r = op(client, r["session_id"], plan_of(r), "cancel")
    assert plan_of(r)["status"] == "cancelled" and balance(client) == BALANCE
    with new_session() as db:
        assert db.query(TaskPlan).one().status == "cancelled"


def test_not_hijacked(client):
    """单步请求照旧走原来的链路，不进规划器。"""
    assert say(client, "给我妈转500再给我妹转300")["cards"][0]["type"] == "confirm"
    assert say(client, "我还有多少钱")["cards"][0]["type"] == "balance"
    assert say(client, "我的借记卡好像丢了")["cards"][0]["title"] == "锁卡确认"
    with new_session() as db:
        assert db.query(TaskPlan).count() == 0


def test_birthday_order_only(client):
    """只说「生日订花」也是计划：询价和下单放到临期执行。"""
    r = say(client, "11月20号我爱人生日，帮我订一束花")
    plan = plan_of(r)
    assert [s["title"] for s in plan["steps"]] == ["询价：鲜花（花漾花艺）", "下单鲜花"]
    sid = r["session_id"]
    r = op(client, sid, plan, "confirm")
    assert plan_of(r)["status"] == "waiting"
    evs = fast_forward(client, plan_of(r)["due_at"])
    flower = card(evs[0])
    assert "玫瑰" in dict(flower["rows"])["商品"]


def test_edit_cake_inside_plan_keeps_plan_going(client):
    """生日计划里蛋糕的确认卡点「修改」换成长寿桃：计划改认新卡，确认后计划照常办完。"""
    sid, plan = start_birthday(client)
    ok(client, card(fast_forward(client, plan["due_at"])[0]))
    cake = card(sync(client, sid))
    d = cake["edit"]["dates"][0]
    new = card(say(client, "", sid, selection={"type": "order_edit", "action_id": cake["action_id"], "sku": "C202", "date": d}))
    assert dict(new["rows"])["商品"] == "6 寸长寿桃蛋糕" and dict(new["rows"])["付款方式"].startswith("心愿罐")
    with new_session() as db:
        steps = db.query(TaskPlan).one().steps
        assert new["action_id"] in [s.get("action_id") for s in steps]
        assert cake["action_id"] not in [s.get("action_id") for s in steps]
    ok(client, new)
    done = sync(client, sid)["cards"][0]
    assert done["status"] == "done"
    assert pot_balance() == 50_000 - 19_900 - 16_800
