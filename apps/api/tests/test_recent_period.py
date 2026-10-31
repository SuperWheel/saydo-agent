"""「最近多少天 / 近多少个月 / 过去N周」这类说法：区间解析对，意图落到交易明细（bill.query），出逐笔的明细卡。规则模式。

数字和直接按同一区间从账务引擎取出来的交易对账。
"""

from datetime import datetime, timedelta

import pytest

from app.agent import loop
from app.analytics import engine
from app.analytics.periods import parse_period
from app.clock import now
from app.db import new_session
from app.money import fmt_yuan
from app.nlu import rule_intent

from .conftest import say

AT = datetime(2026, 10, 30, 12)

CASES = [
    ("近30天的消费记录", "近30天"),
    ("最近7天消费明细", "近7天"),
    ("最近3个月花了哪些钱", "近90天"),
    ("近两个月的消费记录", "近60天"),
    ("过去两周的消费", "近14天"),
    ("最近半年的消费明细", "近半年"),
    ("近一个月消费明细", "近30天"),
    ("最近100天的流水", "近100天"),
    ("我这一个月的消费记录", "近30天"),
    ("过去的30天都买了什么", "近30天"),
    ("30天内的消费", "近30天"),
    ("最近一百二十天的消费明细", "近120天"),
]


@pytest.mark.parametrize("utterance,label", CASES)
def test_recent_periods_list_spend_details(client, utterance, label):
    r = say(client, utterance)
    card = r["cards"][0]
    assert [c["type"] for c in r["cards"]] == ["bill_txns"]
    assert card["period"] == label and card["kind"] == "消费"
    assert "没识别" not in r["reply"]["text"] and r["reply"]["text"].startswith(label)
    p = parse_period(utterance, at=now() + timedelta(seconds=1), default="近90天")
    with new_session() as db:
        txns = [t for t in engine.load(db, 1, p.start, p.end) if t.kind in (engine.KIND_SPEND, engine.KIND_REFUND)]
    total = sum(t.amount_fen for t in txns if t.kind == engine.KIND_SPEND) - sum(
        t.amount_fen for t in txns if t.kind == engine.KIND_REFUND)
    assert card["count"] == len(txns) > 0 and card["total"] == fmt_yuan(total)
    assert len(card["items"]) == min(10, len(txns)) and card["more"] == len(txns) - len(card["items"])


@pytest.mark.parametrize("text,label", [
    ("最近的两周", "近14天"), ("过去这三个月", "近90天"), ("这30天", "近30天"), ("这两天", "近2天"),
    ("三个月以内", "近90天"), ("两周以来", "近14天"), ("最近一百天", "近100天"), ("近两百天", "近200天"),
    ("近一百零五天", "近105天"), ("近十二个月", "近360天"), ("前3周", "近21天"), ("近半个月", "近半个月"),
    # 原有含义不变：「这个月」是本月，「3月以来」「这10月」说的是月份，不是滚动区间
    ("这个月", "2026年10月（截至30日）"), ("这10月的消费", "2026年10月（截至30日）"), ("3月以来的消费", "2026年3月"),
    ("1月到3月", "2026年1月—3月"), ("上个月", "2026年9月"), ("这周", "本周"),
])
def test_recent_period_phrasings(text, label):
    p = parse_period(text, at=AT, default="近7天")
    assert p.recognized and p.label == label
    if label.startswith("近") and label[1:-1].isdigit():
        days = int(label[1:-1])
        assert (p.start, p.end, p.prev_start, p.prev_label) == (AT - timedelta(days=days), AT,
                                                                 AT - timedelta(days=2 * days), f"前{days}天")


def test_too_long_or_vague_recent_falls_back_to_default():
    for text in ("近99年", "最近几天", "最近一段时间"):
        p = parse_period(text, at=AT, default="近30天")
        assert p.label == "近30天" and not p.recognized


@pytest.mark.parametrize("utterance,intent", [
    ("开通境外消费", "card"), ("关闭境外消费", "card"), ("取消爱奇艺会员", "sub"),
    ("这个月钱都花哪了", "bill_categories"), ("近30天外卖消费趋势", "adhoc_analysis"),
    ("给我消费详情", "bill_query"), ("今天买了什么", "bill_query"),
])
def test_broader_detail_intent_leaves_other_intents_alone(utterance, intent):
    assert rule_intent(utterance).intent == intent


@pytest.mark.parametrize("utterance", [u for u, _ in CASES])
def test_model_path_gets_only_bill_tools(utterance):
    """模型路径上整句认得出是账单问句，只发账单工具：模型不会拿其他域的工具去答明细。"""
    assert loop._tool_domains(utterance, []) == {"bill"}
