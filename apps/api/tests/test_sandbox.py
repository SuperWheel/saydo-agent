"""自由分析（WS8，F-BA-06）：模型写分析代码 → AST 白名单 → 子进程沙箱 → 输出契约 → 结果卡。

正常分析的数字一律和直接查库算出来的结果对账；逃逸用例在 tests/redteam/test_sandbox_escape.py。
"""

import hashlib
from collections import defaultdict
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import sandbox
from app.analytics import engine
from app.analytics.periods import parse_period
from app.clock import now
from app.db import new_session
from app.models import AuditLog, Transaction
from app.money import fmt_yuan
from app.nlu import rule_intent
from app.sandbox import OutputError, checker, validate_output
from app.tools.bill_analysis import DEFAULT_PERIOD, UNAVAILABLE

from .conftest import say, text, tool

WEEKEND_CODE = """
from collections import defaultdict
amt = defaultdict(int)
cnt = defaultdict(int)
for t in txns:
    if t["kind"] == "消费" and t["merchant"] == "美团外卖":
        k = "周末" if t["weekday"] >= 5 else "工作日"
        amt[k] += t["amount_fen"]
        cnt[k] += 1
rows = [{"label": k, "amount_fen": amt[k], "count": cnt[k]} for k in ("工作日", "周末")]
rows.append({"label": "工作日比周末多花", "amount_fen": amt["工作日"] - amt["周末"]})
result = {"title": "工作日与周末的外卖支出", "rows": rows}
"""

COFFEE_CODE = """
import statistics
from collections import Counter
by_month = Counter()
for t in txns:
    if t["kind"] == "消费" and t["merchant"] in ("星巴克", "瑞幸咖啡"):
        by_month[t["month"]] += t["amount_fen"]
months = sorted(by_month)
rows = [{"label": m, "amount_fen": by_month[m]} for m in months]
rows.append({"label": "月均", "amount_fen": round(statistics.mean(by_month.values()))})
result = {"title": "每月咖啡花费", "rows": rows}
"""


def _window():
    return parse_period(None, at=now(), default=DEFAULT_PERIOD)


def _db_spends(merchants: tuple[str, ...]) -> list[Transaction]:
    """独立对账：直接查库，不经过分析引擎和沙箱。"""
    p = _window()
    with new_session() as db:
        return list(db.scalars(select(Transaction).where(
            Transaction.merchant.in_(merchants), Transaction.direction == "out", Transaction.status == "succeeded",
            Transaction.created_at >= p.start, Transaction.created_at < p.end)))


def _sandbox_audits() -> list[dict]:
    with new_session() as db:
        return [a.payload for a in db.scalars(select(AuditLog).where(AuditLog.event_type == "sandbox_exec")
                                               .order_by(AuditLog.id))]


# ---------- 模型路径：结果和直接查库一致 ----------

def test_weekday_vs_weekend_takeout_matches_db(llm_client, fake):
    rows = defaultdict(lambda: [0, 0])
    for t in _db_spends(("美团外卖",)):
        k = "周末" if t.created_at.weekday() >= 5 else "工作日"
        rows[k][0] += t.amount_fen
        rows[k][1] += 1
    wd, we = rows["工作日"], rows["周末"]
    assert wd[1] and we[1]
    reply = f"近90天工作日外卖 {fmt_yuan(wd[0])} 元、周末 {fmt_yuan(we[0])} 元，工作日多花 {fmt_yuan(wd[0] - we[0])} 元。"
    fake(tool("bill_adhoc_analysis", question="工作日和周末的外卖支出比一比", code=WEEKEND_CODE), text(reply))

    r = say(llm_client, "工作日和周末的外卖支出比一比")
    assert r["reply"]["source"] == "llm" and r["reply"]["text"] == reply  # 数字都在事实集合里，校验通过
    card = r["cards"][0]
    assert card["type"] == "bill_analysis" and card["period"] == "近90天"
    got = {x["label"]: x for x in card["rows"]}
    assert got["工作日"]["amount_yuan"] == fmt_yuan(wd[0]) and got["工作日"]["count"] == wd[1]
    assert got["周末"]["amount_yuan"] == fmt_yuan(we[0]) and got["周末"]["count"] == we[1]
    assert got["工作日比周末多花"]["amount_yuan"] == fmt_yuan(wd[0] - we[0])
    assert got["工作日"]["bar_permille"] == 1000  # 金额最大的一行画满


def test_monthly_coffee_trend_matches_db(llm_client, fake):
    by_month: dict[str, int] = defaultdict(int)
    for t in _db_spends(("星巴克", "瑞幸咖啡")):
        by_month[f"{t.created_at:%Y-%m}"] += t.amount_fen
    mean = round(sum(by_month.values()) / len(by_month))
    fake(tool("bill_adhoc_analysis", question="每月咖啡花费的趋势", code=COFFEE_CODE), text("每月咖啡花费见卡片。"))

    r = say(llm_client, "每月咖啡花费的趋势")
    card = r["cards"][0]
    assert [(x["label"], x["amount_yuan"]) for x in card["rows"]] == (
        [(m, fmt_yuan(v)) for m, v in sorted(by_month.items())] + [("月均", fmt_yuan(mean))])
    assert card["txn_count"] > 0 and "沙箱" in card["note"]


def test_fabricated_number_is_caught_by_grounding(llm_client, fake):
    """模型编出结果里没有的数字（「周末多花 7,777.77 元」）：事实校验不过，重写仍不过就退回模板句。"""
    bad = "周末比工作日多花 7,777.77 元。"
    fake(tool("bill_adhoc_analysis", code=WEEKEND_CODE), text(bad), text(bad))
    r = say(llm_client, "工作日和周末的外卖支出比一比")
    assert r["reply"]["source"] == "template"
    assert r["reply"]["text"] == r["cards"][0]["reply"]  # 模板句只用卡片上的数字
    assert "7,777.77" not in r["reply"]["text"]


def test_model_can_fix_code_after_error(llm_client, fake):
    """第一次用了 print（不在白名单）：返回原因、不出卡；模型改好后再调用一次。"""
    f = fake(tool("bill_adhoc_analysis", code="print(len(txns))\nresult = 1"),
             tool("bill_adhoc_analysis", code=WEEKEND_CODE), text("对比结果见卡片。"))
    r = say(llm_client, "工作日和周末的外卖支出比一比")
    assert [c["type"] for c in r["cards"]] == ["bill_analysis"]
    first = next(m for m in f.calls[1]["messages"] if m["role"] == "tool")
    assert '"status": "error"' in first["content"] and "print" in first["content"]
    audits = _sandbox_audits()
    assert [a["validation"] for a in audits] == ["reject", "pass"]
    assert not any(a["escape"] for a in audits)  # 写错不算逃逸，不计入熔断


def test_sandbox_exec_is_audited(llm_client, fake):
    fake(tool("bill_adhoc_analysis", question="工作日和周末的外卖支出比一比", code=WEEKEND_CODE), text("见卡片。"))
    say(llm_client, "工作日和周末的外卖支出比一比")
    (a,) = _sandbox_audits()
    assert a["code_sha256"] == hashlib.sha256(WEEKEND_CODE.encode()).hexdigest()
    assert a["validation"] == "pass" and a["status"] == "ok" and a["exit_code"] == 0
    assert a["elapsed_ms"] > 0 and a["peak_kb"] > 0 and a["rows"] == 3 and a["txn_count"] > 0
    assert a["snapshot"] and a["code"] == WEEKEND_CODE
    assert llm_client.post("/admin/audit/verify").json()["ok"]


def test_period_follows_the_question(llm_client, fake):
    fake(tool("bill_adhoc_analysis", question="上个月工作日和周末的外卖支出比一比", code=WEEKEND_CODE), text("见卡片。"))
    r = say(llm_client, "上个月工作日和周末的外卖支出比一比")
    assert r["cards"][0]["period"] == parse_period("上个月", at=now(), default="近90天").label


# ---------- 快照：只读、脱敏 ----------

def test_snapshot_is_desensitized(client):
    with new_session() as db:
        txns = engine.load(db, 1, now() - timedelta(days=200), now())
    snap = sandbox.snapshot(txns)
    assert snap and all(set(s) == {"date", "time", "month", "weekday", "hour", "kind", "category", "merchant",
                                   "amount_fen", "direction", "auto", "cross_border"} for s in snap)
    blob = repr(snap)
    assert "忽略之前" not in blob and "6222" not in blob  # 注入备注、完整账号都不在快照里
    assert "Global Electronics" not in blob and "未登记商户" in blob  # 未登记商户只给可信标签
    assert "陈静" not in blob and "王秀英" not in blob  # 收款人是脱敏名
    assert all(isinstance(s["amount_fen"], int) and s["amount_fen"] > 0 for s in snap)


def test_snapshot_rows_cannot_be_modified():
    out = sandbox.analyze("txns[0]['amount_fen'] = 0\nresult = {'rows': [{'label': 'x', 'count': 1}]}",
                          [{"amount_fen": 100}])
    assert out.status == "runtime" and "mappingproxy" in out.detail


# ---------- 输出契约 ----------

@pytest.mark.parametrize("bad", [
    "hello",
    {"rows": []},
    {"rows": [{"label": "x"}]},
    {"rows": [{"label": "x", "amount_fen": 1.5}]},
    {"rows": [{"label": "x", "amount_fen": True}]},
    {"rows": [{"label": "x" * 25, "count": 1}]},
    {"rows": [{"label": "x", "count": -1}]},
    {"rows": [{"label": "x", "value": 1, "unit": "元"}]},
    {"rows": [{"label": "x", "count": 1, "unit": "%"}]},
    {"rows": [{"label": "x", "count": 1, "note": "额外字段"}]},
    {"rows": [{"label": "x", "count": 1}], "summary": "额外字段"},
    {"rows": [{"label": "x", "count": 1}] * 41},
    {"rows": [{"label": "​", "count": 1}]},
    {"rows": [{"label": "x", "amount_fen": 10**13}]},
])
def test_output_contract_rejects(bad):
    with pytest.raises(OutputError):
        validate_output(bad)


def test_output_contract_accepts_and_normalizes():
    title, rows = validate_output({"title": " 对比 ", "rows": [
        {"label": "工作日", "amount_fen": 12345, "count": 3}, {"label": "占比", "value": 33.3333, "unit": "%"}]})
    assert title == "对比" and rows[1] == {"label": "占比", "value": 33.33, "unit": "%"}


def test_allowed_code_passes_checker():
    for code in (WEEKEND_CODE, COFFEE_CODE,
                 "import datetime\nd = datetime.date.fromisoformat('2026-10-01')\n"
                 "result = {'rows': [{'label': d.strftime('%m-%d'), 'value': d.isoweekday()}]}",
                 "import itertools, math\nxs = sorted(t['hour'] for t in txns)\n"
                 "g = [(k, len(list(v))) for k, v in itertools.groupby(xs)]\n"
                 "result = {'rows': [{'label': f'{k:02d}点', 'count': n} for k, n in g][:24] or [{'label': '无', 'count': 0}]}"):
        assert checker.check(code).ok, checker.check(code).reasons


def test_strptime_works_inside_sandbox():
    code = ("import datetime\nw = datetime.datetime.strptime('2026-10-03', '%Y-%m-%d').weekday()\n"
            "result = {'rows': [{'label': '周几', 'value': w}]}")
    out = sandbox.analyze(code, [])
    assert out.ok and out.rows == [{"label": "周几", "value": 5}]


# ---------- 规则模式：没有模型就不生成代码 ----------

def test_rule_mode_replies_unavailable(client):
    r = say(client, "工作日和周末的外卖支出比一比")
    assert r["reply"]["source"] == "rule" and r["reply"]["text"] == UNAVAILABLE
    assert [c["type"] for c in r["cards"]] == ["notice"]
    assert _sandbox_audits()[-1]["status"] == "unavailable"


@pytest.mark.parametrize("sentence, intent", [
    ("工作日和周末的外卖支出比一比", "adhoc_analysis"),
    ("每月咖啡花费的趋势", "adhoc_analysis"),
    ("一天里哪个时段点外卖最多", "adhoc_analysis"),
    ("上个月在美团花了多少", "bill_query"),
    ("这个月钱都花哪了", "bill_categories"),
    ("把借记卡单日限额提高到5万", "card"),
    ("每天给我妈转500", "transfer"),
])
def test_rule_intent_does_not_steal_other_intents(sentence, intent):
    assert rule_intent(sentence).intent == intent


def test_non_string_code_is_rejected_not_crashing(llm_client, fake):
    fake(tool("bill_adhoc_analysis", code=12345), text("没能完成分析。"))
    r = say(llm_client, "工作日和周末的外卖支出比一比")
    assert r["cards"] == [] and _sandbox_audits()[-1]["validation"] == "reject"
