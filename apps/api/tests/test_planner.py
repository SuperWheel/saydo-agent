"""规划器校验器（确定性）、模型规划路径（脚本化假模型）、事实校验的新办理动词。"""

import json

import pytest

import app.main  # noqa: F401  注册全部工具
from app.agent import planner
from app.agent.grounding import check
from app.agent.llm import LLMError
from app.clock import now
from app.db import new_session
from app.models import TaskPlan
from app.nlu.when import parse_date

from .conftest import say, text
from .helpers import BALANCE, balance, events

SAID = "下周五我妈生日，帮我订束花和蛋糕，再存 500 进心愿罐"


def _date() -> str:
    return f"{parse_date('下周五', now())}"


def good_plan() -> dict:
    d = _date()
    return {"goal": "妈妈生日", "event_date": d, "steps": [
        {"id": "s1", "tool": "pot_lock_propose", "args": {"amount_text": "500", "purpose": "妈妈生日", "until": d},
         "depends_on": [], "at": "now"},
        {"id": "s2", "tool": "merchant_quote", "args": {"category": "flower", "for_whom": "我妈", "date": d},
         "depends_on": [], "at": "due"},
        {"id": "s3", "tool": "order_propose", "args": {"quote_id": "{{s2.quote_id}}", "pot_id": "{{s1.pot_id}}"},
         "depends_on": ["s1", "s2"], "at": "due"},
    ]}


def reasons(raw: dict, said: str = SAID) -> list[str]:
    with pytest.raises(planner.PlanRejected) as e:
        planner.validate(raw, source_text=said, at=now())
    return e.value.reasons


def with_step(i: int, **change) -> dict:
    raw = good_plan()
    raw["steps"][i] |= change
    return raw


# ---------- 校验器 ----------

def test_valid_plan_normalized():
    goal, event, steps = planner.validate(good_plan(), source_text=SAID, at=now())
    assert goal == "妈妈生日" and f"{event}" == _date()
    assert [s["tool"] for s in steps] == ["pot.lock_propose", "merchant.quote", "order.propose"]
    assert [s["kind"] for s in steps] == ["write", "read", "write"]


@pytest.mark.parametrize("tool", ["pot_lock", "pot.lock", "transfer_create", "order.pay", "shell_exec", "transfer.create"])
def test_rejects_write_or_unknown_tools(tool):
    """写工具（执行函数）和未注册的工具一律拒绝：写操作只能经提议工具出确认卡。"""
    assert any("不允许的工具" in r for r in reasons(with_step(0, tool=tool)))


def test_rejects_amount_not_from_user():
    raw = with_step(0, args={"amount_text": "5000", "purpose": "妈妈生日"})
    assert any("不是你说的" in r for r in reasons(raw))


def test_rejects_literal_ids():
    """报价单号、心愿罐编号只能引用前序步骤的结果，模型不能自己填。"""
    raw = with_step(2, args={"quote_id": "QF102-261104T1000-261106", "pot_id": "{{s1.pot_id}}"})
    assert any("只能引用前序步骤" in r for r in reasons(raw))


def test_rejects_cycle():
    raw = good_plan()
    raw["steps"][0]["depends_on"] = ["s3"]
    assert reasons(raw) == ["步骤之间存在循环依赖"]


def test_rejects_ref_to_non_ancestor():
    raw = with_step(2, depends_on=["s2"])  # 引用了 s1.pot_id 却不依赖 s1
    assert any("不是前序步骤" in r for r in reasons(raw))


def test_rejects_undeclared_output_field():
    raw = with_step(2, args={"quote_id": "{{s2.price_fen}}"})
    assert any("不是该步骤的输出" in r for r in reasons(raw))


def test_rejects_unknown_dependency_and_bad_ref_syntax():
    assert any("不存在的步骤" in r for r in reasons(with_step(2, depends_on=["s9"])))
    assert any("引用格式不对" in r for r in reasons(with_step(2, args={"quote_id": "{{s2.quote_id}} 加急"})))


def test_rejects_payee_not_said():
    raw = good_plan()
    raw["steps"].append({"id": "s4", "tool": "transfer_propose", "args": {"payee_hint": "赵磊", "amount_text": "500"},
                         "depends_on": [], "at": "now"})
    assert any("收款人「赵磊」不是你说的" in r for r in reasons(raw))


def test_rejects_unknown_and_non_scalar_args():
    assert any("未知参数" in r for r in reasons(with_step(0, args={"amount_text": "500", "raw_account": "6222"})))
    assert any("只能是标量" in r for r in reasons(with_step(0, args={"amount_text": ["500"]})))


def test_rejects_date_mismatch_and_bad_timing():
    raw = good_plan()
    raw["event_date"] = "2026-12-31"
    assert any("和你说的不一致" in r for r in reasons(raw))
    raw = with_step(1, at="tomorrow")
    assert any("执行时机" in r for r in reasons(raw))


def test_rejects_too_many_steps():
    raw = good_plan()
    raw["steps"] = [dict(raw["steps"][1], id=f"s{i}") for i in range(1, 14)]
    assert reasons(raw) == ["步骤超过 12 个，请拆开办理"]


def test_rejects_garbage():
    for raw in ({}, {"steps": []}, {"steps": "s1"}, [1, 2]):
        assert reasons(raw) == ["计划格式不对"]


def test_template_plan_passes_validator():
    raw = planner.template_plan(SAID, now())
    goal, _, steps = planner.validate(raw, source_text=SAID, at=now())
    assert goal == "妈妈生日准备" and len(steps) == 5


@pytest.mark.parametrize("said,multi", [
    (SAID, True),
    ("帮我订束花和蛋糕", True),
    ("11月20号我爱人生日，帮我订一束花", True),
    ("存500进心愿罐，再给我妈转200", True),
    ("给我妈转500再给我妹转300", False),
    ("存500进心愿罐", False),
    ("帮我订一束花", False),
    ("上个月买东西花了多少", False),
    ("我的借记卡好像丢了", False),
    ("每月15号给我妈转2000", False),
])
def test_multi_step_gate(said, multi):
    assert planner.is_multi_step(said) is multi


# ---------- 模型规划（假模型） ----------

def test_llm_plan(llm_client, fake):
    f = fake(text(json.dumps(good_plan(), ensure_ascii=False)))
    r = say(llm_client, SAID)
    plan = next(c for c in r["cards"] if c["type"] == "plan")
    assert plan["source"] == "llm" and [s["title"] for s in plan["steps"]] == \
           ["存 ¥500.00 进心愿罐（妈妈生日）", "询价：鲜花（花漾花艺）", "下单鲜花，从心愿罐付款"]
    assert len(f.calls) == 1 and f.calls[0]["tools"] is None  # 规划器单独一次调用，不给工具
    prompt = f.calls[0]["messages"][0]["content"]
    assert "pot_lock_propose" in prompt and "pot_lock(" not in prompt and "transfer_create" not in prompt


def test_llm_plan_rejected_not_executed(llm_client, fake):
    """模型计划里夹带写工具（直接转账）：校验不过，不执行、不建计划，退回追问。"""
    raw = good_plan()
    raw["steps"].append({"id": "s4", "tool": "transfer_create",
                         "args": {"account_no": "6222000011112222", "amount_fen": 2865032}, "depends_on": [], "at": "now"})
    fake(text(json.dumps(raw)))
    r = say(llm_client, SAID)
    assert [c["type"] for c in r["cards"]] == ["notice"] and "安全校验" in r["reply"]["text"]
    assert "plan_rejected" in events(llm_client, r["trace_id"])
    with new_session() as db:
        assert db.query(TaskPlan).count() == 0
    assert balance(llm_client) == BALANCE


def test_llm_plan_injected_amount_rejected(llm_client, fake):
    raw = with_step(0, args={"amount_text": "28650", "purpose": "妈妈生日"})
    fake(text("好的，计划如下：\n```json\n" + json.dumps(raw, ensure_ascii=False) + "\n```"))
    r = say(llm_client, SAID)
    assert "不是你说的" in r["reply"]["text"]


@pytest.mark.parametrize("reply", [LLMError("timeout"), text("抱歉，我不太明白")])
def test_llm_failure_falls_back_to_template(llm_client, fake, reply):
    fake(reply)
    r = say(llm_client, SAID)
    plan = next(c for c in r["cards"] if c["type"] == "plan")
    assert plan["source"] == "rule" and len(plan["steps"]) == 5
    assert "planner_fallback" in events(llm_client, r["trace_id"])


def test_llm_mode_schedule_is_deterministic(llm_client, fake):
    """「每月15号给我妈转2000」不交给模型：不能被当成立即转账。"""
    f = fake()
    r = say(llm_client, "每月15号给我妈转2000")
    c = r["cards"][0]
    assert c["title"] == "定时转账确认" and c["level"] == "RED" and f.calls == []


# ---------- 事实校验：跨场景的办理动词 ----------

@pytest.mark.parametrize("claim", ["已帮你下单", "已为你存入心愿罐500元", "蛋糕已下单", "订单已提交", "已为你预订好鲜花",
                                   "定时转账设置成功", "已帮你释放心愿罐", "已为你设置每月转账"])
def test_grounding_blocks_fake_completion(claim):
    assert check(claim, {"500"}).false_claim


@pytest.mark.parametrize("ok", ["确认后从心愿罐付款", "已为你生成订单确认卡", "确认后会下单",
                                "请核对下方订单确认卡，确认后才会付款", "心愿罐已关闭", "借记卡已锁定"])
def test_grounding_allows_process_talk(ok):
    assert not check(ok, set()).false_claim
