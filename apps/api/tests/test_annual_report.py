"""年度账单：独立流水对账、同期比较、历史种子和 2026 回归。"""

import hashlib
import json
from datetime import date, datetime, timedelta
from statistics import median
from time import perf_counter

import pytest
from sqlalchemy import func, select

from app import clock
from app.analytics import engine, recurring
from app.analytics.periods import parse_period
from app.core_mock.ledger import is_balanced, ledger_balance, post
from app.core_mock.seed import SUBSCRIPTIONS, _mom, calendar, historical_calendar, seed
from app.db import Base, init_engine, new_session
from app.models import Account, Contact, LedgerEntry, Transaction
from app.money import fmt_yuan
from app.nlu import rule_intent

from .conftest import enter, say

AT = datetime(2026, 11, 1, 12)
TOTALS = {2024: (22_929_523, 12_554_338), 2025: (24_103_761, 12_784_237)}


@pytest.mark.parametrize("text,year,months", [
    ("去年全年收支", 2025, 12), ("2024年收支", 2024, 12), ("去年一年收入支出", 2025, 12),
    ("2025年全年收入和支出", 2025, 12), ("今年以来的收支", 2026, 10),
    ("我去年一年花了多少钱", 2025, 12), ("2024年的年度报告", 2024, 12),
])
def test_annual_summary_phrasings_through_rule_chat(client, monkeypatch, text, year, months):
    monkeypatch.setenv("DEMO_DATE", "2026-11-01")
    clock.reset()
    assert client.post("/dev/seed").status_code == 200
    enter(client)
    response = say(client, text)
    assert response["reply"]["source"] == "rule"
    assert [c["type"] for c in response["cards"]] == ["bill_report"]
    card = response["cards"][0]
    assert card["period"].startswith(f"{year}年") and len(card["monthly"]) == months
    assert [m["label"] for m in card["monthly"]] == [f"{m}月" for m in range(1, months + 1)]
    kpis = {k["label"]: k["value"] for k in card["kpis"]}
    assert kpis["收入"] == fmt_yuan(sum(m["income_fen"] for m in card["monthly"]))
    assert kpis["消费"] == fmt_yuan(sum(m["expense_fen"] for m in card["monthly"]))


@pytest.mark.parametrize("text", ["上个月买鞋花了多少", "上个月在京东买鞋花了多少", "去年买衣服花了多少"])
def test_unrecognized_product_filter_never_returns_all_spending(client, text):
    response = say(client, text)
    assert response["reply"]["source"] == "rule"
    assert [c["type"] for c in response["cards"]] == ["notice"]
    assert "商品级明细" in response["reply"]["text"]
    assert "不能" in response["reply"]["text"] and "全部消费" in response["reply"]["text"]
    assert "合计" not in response["reply"]["text"]


def test_explicit_shoe_merchant_keyword_is_still_queryable(client):
    # 可以查明确的商户关键词；不能把电商平台的所有订单当成买鞋。
    with new_session() as db:
        acct = db.get(Account, 1)
        post(db, account=acct, direction="out", amount_fen=39900, counterparty="城市鞋店", merchant="城市鞋店",
             mcc="5661", category="购物", contra_code="CLEARING:UNIONPAY", at=datetime(2026, 9, 20, 12))
        db.commit()
    result = client.get("/api/bill/txns", params={"start": "2026-09-01", "end": "2026-10-01", "keyword": "鞋"})
    assert result.status_code == 200, result.text
    detail = result.json()
    assert detail["count"] == 1 and detail["total"] == "399.00"


@pytest.mark.parametrize("text", ["2025年餐饮花了多少", "2024年最大的一笔支出", "2025年3月花了多少"])
def test_annual_summary_intent_keeps_filtered_and_monthly_queries(text):
    assert rule_intent(text).intent == "bill_query"


@pytest.mark.parametrize("text,year", [
    ("2025年", 2025), ("2024年全年", 2024), ("去年全年", 2025), ("去年一年", 2025), ("去年", 2025),
    ("2025年账单报告", 2025), ("2025年年报", 2025), ("2025年年度报告", 2025), ("2025年年度账单", 2025),
    ("2024年每个月支出", 2024),
])
def test_annual_period(text, year):
    p = parse_period(text, at=AT, default="上个月")
    assert p.recognized and p.annual
    assert (p.start, p.end) == (datetime(year, 1, 1), datetime(year + 1, 1, 1))
    assert (p.prev_start, p.prev_end, p.prev_label) == (datetime(year - 1, 1, 1), p.start, f"{year - 1}年")


@pytest.mark.parametrize("text", ["2025年账单报告", "2025年年报", "2025年年度报告", "2025年年度账单", "2024年每个月支出"])
def test_annual_report_intent(text):
    assert rule_intent(text).intent == "bill_report"


@pytest.mark.parametrize("text,start,end,annual", [
    ("2025年3月", datetime(2025, 3, 1), datetime(2025, 4, 1), False),
    ("去年3月", datetime(2025, 3, 1), datetime(2025, 4, 1), False),
    ("今年", datetime(2026, 1, 1), AT, True),
    ("今年全年", datetime(2026, 1, 1), AT, True),
    ("今年以来", datetime(2026, 1, 1), datetime(2026, 11, 1), True),
    ("近一年", AT - timedelta(days=365), AT, False),
])
def test_existing_periods_and_year_to_date(text, start, end, annual):
    p = parse_period(text, at=AT, default="上个月")
    assert p.recognized and (p.start, p.end, p.annual) == (start, end, annual)


def test_leap_day_current_year_compares_to_february_end():
    p = parse_period("今年", at=datetime(2024, 2, 29, 12), default="上个月")
    assert p.prev_end == datetime(2023, 2, 28, 12)


def _raw_months(year):
    """直接按交易字段聚合，独立于账单引擎和报告模板。消费沿用 KPI，不含亲友转账。"""
    with new_session() as db:
        rows = db.execute(select(func.strftime("%m", Transaction.created_at), Transaction.direction,
                                 Transaction.category, func.sum(Transaction.amount_fen))
                          .where(Transaction.created_at >= datetime(year, 1, 1),
                                 Transaction.created_at < datetime(year + 1, 1, 1), Transaction.channel != "system")
                          .group_by(func.strftime("%m", Transaction.created_at), Transaction.direction,
                                    Transaction.category)).all()
    months = {m: [0, 0] for m in range(1, 13)}
    for month, direction, category, amount in rows:
        if direction == "in":
            months[int(month)][0] += amount
        elif category != "转账":
            months[int(month)][1] += amount
    return months


@pytest.mark.parametrize("year", [2024, 2025])
def test_annual_report_matches_raw_monthly_totals(client, year):
    card = say(client, f"{year}年账单报告")["cards"][0]
    assert card["type"] == "bill_report" and card["period"] == f"{year}年"
    monthly = card["monthly"]
    assert len(monthly) == 12 and [m["label"] for m in monthly] == [f"{m}月" for m in range(1, 13)]
    raw = _raw_months(year)
    for index, month in enumerate(monthly, 1):
        assert [month["income_fen"], month["expense_fen"]] == raw[index]
        assert (month["income"], month["expense"]) == tuple(fmt_yuan(n) for n in raw[index])
        assert min(raw[index]) > 0
    income = sum(m["income_fen"] for m in monthly)
    expense = sum(m["expense_fen"] for m in monthly)
    assert (income, expense) == TOTALS[year]
    kpis = {k["label"]: k["value"] for k in card["kpis"]}
    assert (kpis["收入"], kpis["消费"], kpis["转账转出"]) == (fmt_yuan(income), fmt_yuan(expense), "9,600.00")
    assert card["subscription"] == {"count": 72, "total": "2,412.00"}
    assert len(card["top_merchants"]) == 5
    assert sum(int(r["amount"].replace(",", "").replace(".", "")) for r in card["rows"]) == expense
    assert card["anomalies"] == []
    with new_session() as db:
        classified = engine.load(db, 1, datetime(year, 1, 1), datetime(year + 1, 1, 1))
        assert {t.category for t in classified if t.kind == engine.KIND_SPEND} >= {
            "餐饮", "购物", "交通", "居住", "娱乐", "医疗", "教育", "旅行", "订阅"}
    if year == 2024:
        assert card["year_compare"] is None
    else:
        assert card["year_compare"] == {"prev_label": "2024年", "income_change": "+5.1%", "expense_change": "+1.8%"}
        assert card["spend_change"] == card["year_compare"]["expense_change"]


def test_last_year_report_is_2025(client):
    a = say(client, "去年全年的账单报告")["cards"][0]
    b = say(client, "2025年年度账单")["cards"][0]
    for key in ("period", "kpis", "monthly", "year_compare", "rows", "top_merchants"):
        assert a[key] == b[key]


def test_year_to_date_ends_in_october_and_compares_same_months(client, monkeypatch):
    monkeypatch.setenv("DEMO_DATE", "2026-11-01")
    clock.reset()
    client.post("/dev/seed")
    enter(client)
    card = say(client, "今年以来的账单报告")["cards"][0]
    assert [m["label"] for m in card["monthly"]] == [f"{m}月" for m in range(1, 11)]
    kpis = {k["label"]: k["value"] for k in card["kpis"]}
    assert kpis["收入"] == fmt_yuan(sum(m["income_fen"] for m in card["monthly"]))
    assert kpis["消费"] == fmt_yuan(sum(m["expense_fen"] for m in card["monthly"]))
    prev = _raw_months(2025)
    for key, field, column in (("income_change", "income_fen", 0), ("expense_change", "expense_fen", 1)):
        assert card["year_compare"][key] == engine.change(
            sum(m[field] for m in card["monthly"]), sum(prev[m][column] for m in range(1, 11)), True)["change"]
    assert card["year_compare"]["prev_label"] == "2025年同期"
    full = say(client, "今年全年的账单报告")["cards"][0]
    assert len(full["monthly"]) == 12
    assert all(m["expense_fen"] == m["income_fen"] == 0 for m in full["monthly"][10:])


def test_annual_categories_queries_and_monthly_request(client):
    dining = say(client, "2025年餐饮花了多少")["cards"][0]
    assert dining["type"] == "bill_txns" and dining["period"] == "2025年" and dining["total"] == "17,147.05"
    largest = say(client, "2024年最大的一笔支出")["cards"][0]
    assert largest["type"] == "bill_txns" and largest["period"] == "2024年"
    assert largest["single"] == "金额最大" and largest["items"][0]["amount"] == "-4,200.00"
    cats = say(client, "2025年消费分类统计")["cards"][0]
    assert cats["type"] == "bill_categories" and cats["total"] == fmt_yuan(TOTALS[2025][1])
    months = say(client, "2024年每个月支出")["cards"][0]
    assert months["type"] == "bill_report" and len(months["monthly"]) == 12


def test_2026_transactions_balances_contacts_and_detection_unchanged(client):
    with new_session() as db:
        events = calendar(date(2027, 1, 1), _mom(db))
        serial = [{k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in e.items()} for e in events]
        digest = hashlib.sha256(json.dumps(serial, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        assert digest == "8d64a21787f74c2b0008218b25b23ebce0ab1ca13d9c4964b0f45b18b4878aab"
        account = db.get(Account, 1)
        assert account.balance_fen == ledger_balance(db, 1) == 2_865_032 and is_balanced(db)
        assert [str(c.first_transfer_at) for c in db.scalars(select(Contact).order_by(Contact.id))] == [
            "2026-01-01 20:15:00", "2026-09-20 19:20:00", "2026-09-27 21:05:00",
            "2026-09-08 12:40:00", "2026-10-15 22:00:00", "None"]
        for month, spend in ((1, 1_040_041), (3, 975_205), (9, 907_861)):
            tot = engine.totals(engine.load(db, 1, datetime(2026, month, 1), datetime(2026, month + 1, 1)))
            assert (tot["income_fen"], tot["spend_fen"]) == (1_800_000, spend)
        det = recurring.load_detection(db, 1, datetime(2026, 10, 30, 12))
        assert {r.party for r in det.subscriptions} == {m for m, *_ in SUBSCRIPTIONS}
        assert all(r.confidence == 1.0 and r.signals == {"p": 1.0, "a": 1.0, "n": 1.0, "m": 1.0}
                   for r in det.subscriptions)
        start = datetime(2026, 10, 1)
        txns = engine.load(db, 1, start - timedelta(days=90), datetime(2026, 10, 30, 12))
        found = engine.detect(txns, start, datetime(2026, 10, 30, 12), engine.coverage_start(db, 1))
        assert {a.txn.display for a in found} == {"瑞幸咖啡", "Global Electronics (境外)"}
        # 批量种子之后，正常 post 的主键分配与复式分录照常工作。
        txn = post(db, account=account, direction="out", amount_fen=100, counterparty="测试消费",
                   contra_code="CLEARING:UNIONPAY")
        db.flush()
        assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.txn_id == txn.id)) == 2
        assert ledger_balance(db, 1) == account.balance_fen == 2_864_932


def test_historical_calendar_is_deterministic_and_seasonal():
    assert historical_calendar(date(2026, 1, 1)) == historical_calendar(date(2027, 1, 1))
    for year, spring in ((2024, (2, 10)), (2025, (1, 29))):
        events = [e for e in historical_calendar(date(year + 1, 1, 1)) if e["at"].year == year]
        assert any((e["at"].month, e["at"].day) == spring and e.get("merchant") == "春节聚餐" for e in events)
        assert {e["at"].month for e in events if e["category"] == "旅行"} == {7, 8}
        assert len([e for e in events if e.get("merchant") == "京东" and e["at"].month == 11
                    and e["amount_fen"] >= 85000]) >= 3


def test_seed_rebuild_performance(tmp_path):
    samples = []
    for index in range(3):
        Base.metadata.create_all(init_engine(f"sqlite:///{tmp_path / f'seed-{index}.db'}"))
        with new_session() as db:
            start = perf_counter()
            seed(db)
            samples.append(perf_counter() - start)
            assert db.scalar(select(func.count()).select_from(Transaction)) > 3000
            assert is_balanced(db) and ledger_balance(db, 1) == 2_865_032
    assert median(samples) < 3.0, samples
