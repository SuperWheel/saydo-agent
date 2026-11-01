"""演示日历流水：同样的日期总是同样的流水；今天及以后的不入库；快进 / 隔天补齐；每月转存定期配平。规则模式。"""

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app import clock
from app.analytics import engine
from app.analytics.periods import parse_period
from app.clock import now
from app.core_mock.seed import (CALENDAR_END, HISTORY_START, SUBSCRIPTIONS, SWEEP_DAY, SWEEP_KEEP_FEN, _mom, _signed,
                                calendar)
from app.db import new_session
from app.models import Account, Transaction

from .conftest import enter, say
from .helpers import BALANCE, after_travel, balance, ledger_ok


def rows() -> list[Transaction]:
    with new_session() as db:
        return list(db.scalars(select(Transaction).where(Transaction.channel != "system").order_by(Transaction.id)))


def at_date(client, monkeypatch, day: str) -> None:
    monkeypatch.setenv("DEMO_DATE", day)
    assert client.post("/dev/seed").status_code == 200
    enter(client)  # 重置演示数据后进入凭证作废，重新过一次人脸


def kpis(client, text: str) -> dict:
    return {k["label"]: k["value"] for k in say(client, text)["cards"][0]["kpis"]}


def test_same_month_same_numbers_whatever_day_seeded(client, monkeypatch):
    """3 月的账单只由日期决定：10-30 建库和 12-20 建库查到的是同一组数；两天建库当天的余额都是目标值。"""
    seen = []
    for day in ("2026-10-30", "2026-12-20"):
        at_date(client, monkeypatch, day)
        assert balance(client) == BALANCE and ledger_ok(client)
        seen.append((kpis(client, "2026年3月的账单报告"), say(client, "2026年3月花了多少")["cards"][0]["total"]))
    assert seen[0] == seen[1] and seen[0][0]["收入"] == "18,000.00"


def test_history_from_january_and_nothing_from_today_on(client, monkeypatch):
    at_date(client, monkeypatch, "2026-10-30")
    ts = [t.created_at for t in rows()]
    assert min(ts).date() == HISTORY_START
    assert max(ts) < datetime.combine(now().date(), time())  # 今天及以后的一笔都不在库里
    for month in range(1, 10):  # 1 到 9 月每个月都有工资和房租
        card = say(client, f"2026年{month}月的账单报告")["cards"][0]
        assert {k["label"]: k["value"] for k in card["kpis"]}["收入"] == "18,000.00", month


def test_sweep_keeps_calendar_balance_in_band_and_is_not_spending(client):
    with new_session() as db:
        mom = _mom(db)
    events = sorted(calendar(CALENDAR_END + timedelta(days=1), mom), key=lambda e: e["at"])
    virtual, low, high = SWEEP_KEEP_FEN, SWEEP_KEEP_FEN, SWEEP_KEEP_FEN
    for e in events:
        virtual += _signed(e)
        low, high = min(low, virtual), max(high, virtual)
    assert 1_000_000 < low and high < 5_000_000  # 两年里一直在 1 万到 5 万之间
    sweeps = [e for e in events if e["category"] == "储蓄"]
    assert len(sweeps) == 24 and {e["at"].day for e in sweeps} == {SWEEP_DAY}
    with new_session() as db:
        txns = engine.load(db, 1, datetime(2026, 3, 1), datetime(2026, 4, 1))
    saved = [t for t in txns if t.category == "储蓄"]
    assert len(saved) == 1 and saved[0].kind == engine.KIND_TRANSFER  # 转存定期不算消费


def test_fast_forward_books_the_days_in_between(client):
    seeded, n = now().date(), len(rows())
    to = datetime.combine(seeded + timedelta(days=3), time(12, 0))
    r = client.post("/dev/fast-forward", json={"to": to.isoformat()})
    assert r.status_code == 200
    with new_session() as db:
        expected = [e for e in calendar(to.date(), _mom(db)) if e["at"].date() >= seeded]
    new = rows()[n:]
    assert sorted((t.created_at, t.amount_fen) for t in new) == sorted((e["at"], e["amount_fen"]) for e in expected)
    assert balance(client) == after_travel(BALANCE) and ledger_ok(client)
    assert client.post("/dev/fast-forward", json={"to": to.isoformat()}).status_code == 200
    assert len(rows()) == n + len(new)  # 再扫一次不会重复入账


def test_next_day_request_catches_up_without_fast_forward(client):
    """后端一直开着、跨过零点后接着用：下一次请求先补齐昨天的流水。"""
    n = len(rows())
    clock.travel(timedelta(days=1))
    assert balance(client) == after_travel(BALANCE) and len(rows()) > n


@pytest.mark.parametrize("day", ["2026-10-31", "2026-11-30", "2026-12-31", "2027-02-28"])
def test_midnight_catches_up_once(client, monkeypatch, day):
    """从月末最后一秒跨到下月：只补昨天，余额和账本一起变，第二次请求不重复补。"""
    at_date(client, monkeypatch, day)
    at = now()
    clock.travel(at.replace(hour=23, minute=59, second=59) - at)
    before = rows()
    assert balance(client) == BALANCE
    assert clock.travel(timedelta(seconds=1)) == datetime.combine(at.date() + timedelta(days=1), time())
    with new_session() as db:
        expected = [e for e in calendar(now().date(), _mom(db)) if e["at"].date() == at.date()]
    assert balance(client) == after_travel(BALANCE) and ledger_ok(client)
    added = rows()[len(before):]
    assert sorted((t.created_at, t.amount_fen) for t in added) == sorted((e["at"], e["amount_fen"]) for e in expected)
    assert balance(client) == after_travel(BALANCE) and len(rows()) == len(before) + len(added)


def test_subscriptions_charge_after_fast_forward(client):
    seeded = now().date()
    clock.travel(timedelta(days=31))
    client.get("/api/accounts")
    charged = {t.merchant for t in rows() if t.channel == "auto" and t.created_at.date() >= seeded}
    assert charged == {m for m, *_ in SUBSCRIPTIONS}


def test_catch_up_skips_spending_when_money_is_gone(client):
    with new_session() as db:
        acct = db.scalar(select(Account).where(Account.user_id == 1, Account.kind == "debit"))
        acct.balance_fen = 0  # 模拟钱已经花光（不经记账，只测补齐时不报错）
        db.commit()
    clock.travel(timedelta(days=2))
    assert client.get("/api/accounts").status_code == 200


@pytest.mark.parametrize("text,label,start,end", [
    ("1月到3月收入多少", "2026年1月—3月", date(2026, 1, 1), date(2026, 4, 1)),
    ("今年一季度花了多少", "2026年第1季度", date(2026, 1, 1), date(2026, 4, 1)),
    ("第三季度的账单报告", "2026年第3季度", date(2026, 7, 1), date(2026, 10, 1)),
    ("6月至8月钱都花哪了", "2026年6月—8月", date(2026, 6, 1), date(2026, 9, 1)),
])
def test_month_range_and_quarter(text, label, start, end):
    p = parse_period(text, at=datetime(2026, 10, 30, 15), default="近30天")
    assert (p.label, p.start.date(), p.end.date()) == (label, start, end)


def test_quarter_total_is_three_months_added_up(client, monkeypatch):
    at_date(client, monkeypatch, "2026-10-30")
    yuan = lambda s: round(float(s.replace(",", "")) * 100)  # noqa: E731
    months = sum(yuan(say(client, f"2026年{m}月花了多少")["cards"][0]["total"]) for m in (1, 2, 3))
    card = say(client, "今年一季度花了多少")["cards"][0]
    assert card["period"] == "2026年第1季度" and yuan(card["total"]) == months
    assert say(client, "1月到3月收入多少")["cards"][0]["total"] == "54,000.00"
