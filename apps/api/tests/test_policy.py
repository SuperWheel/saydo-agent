import pytest

from app.policy import PolicyContext, Risk, evaluate, load_rules, validate_rules


def ctx(**kw):
    base = dict(tool="transfer.create", base_level=Risk.YELLOW, moves_money=True, amount_fen=50_000,
                balance_fen=10_000_000, outflow_today_fen=0, payee_known=True)
    return PolicyContext(**{**base, **kw})


@pytest.mark.parametrize("outflow,amount,level", [
    (0, 99_999, Risk.YELLOW),
    (0, 100_000, Risk.YELLOW),        # 恰好 1000 元仍是 🟡
    (0, 100_001, Risk.RED),           # 1000.01 元 → 🔴
    (50_000, 50_000, Risk.YELLOW),    # 累计恰好 1000
    (50_000, 80_000, Risk.RED),       # 累计 1300
    (99_999, 2, Risk.RED),
])
def test_cumulative_boundary(outflow, amount, level):
    d = evaluate(ctx(outflow_today_fen=outflow, amount_fen=amount))
    assert d.level == level and not d.block


def test_new_payee_red_at_any_amount():
    d = evaluate(ctx(amount_fen=100, payee_known=False))
    assert d.level == Risk.RED and "new_payee" in d.rule_hits
    assert d.required_factors == ["face", "sms"]


def test_risk_only_goes_up():
    d = evaluate(ctx(amount_fen=200_000, payee_known=False, injection_flag=True))
    assert d.level == Risk.RED
    assert set(d.rule_hits) == {"cumulative_outflow", "new_payee", "injection_suspected"}


def test_velocity():
    assert evaluate(ctx(recent_count=1)).level == Risk.YELLOW
    assert evaluate(ctx(recent_count=2)).level == Risk.RED


@pytest.mark.parametrize("kw,needle", [
    ({"kill_switch": True}, "只读保护"),
    ({"amount_fen": 0}, "大于 0"),
    ({"balance_fen": 100, "amount_fen": 101}, "余额不足"),
    ({"amount_fen": 4_000_000, "outflow_today_fen": 1_000_001}, "5 万元"),
])
def test_blocks(kw, needle):
    d = evaluate(ctx(**kw))
    assert d.block and needle in d.block_reason


# ---------- 通用规则：tool_in / when / 止损 ----------

RULES = {
    "rules": [
        {"id": "cumulative_outflow", "tool_in": ["transfer.create"], "yellow_max_fen": 100_000, "elevate_to": "RED",
         "reason": "累计 {after} 超过 {limit}"},
        {"id": "card_loosen", "tool_in": ["card.limit_set"], "when": {"direction": "loosen"}, "elevate_to": "RED",
         "reason": "放宽卡片限制"},
        {"id": "injection_suspected", "elevate_to": "RED", "reason": "疑似注入"},
    ],
    "bank_hard_limits": {"non_counter_daily_max_fen": 5_000_000, "reason": "超限"},
    "auth": {"YELLOW": [], "RED": ["face", "sms"]},
}


def card_ctx(**kw):
    return PolicyContext(**{"tool": "card.limit_set", "base_level": Risk.YELLOW, **kw})


@pytest.mark.parametrize("kw,level,hits", [
    ({"facts": {"direction": "tighten"}}, Risk.YELLOW, []),             # 收紧限制 🟡
    ({"facts": {"direction": "loosen"}}, Risk.RED, ["card_loosen"]),    # 放宽限制 🔴
    ({"facts": {}}, Risk.YELLOW, []),
    # 不写 tool_in 的规则对所有工具生效
    ({"facts": {"direction": "tighten"}, "injection_flag": True}, Risk.RED, ["injection_suspected"]),
    ({"facts": {"direction": "loosen"}, "outflow_today_fen": 999_999}, Risk.RED, ["card_loosen"]),  # 转账累计规则不管卡片
])
def test_rules_scoped_by_tool(kw, level, hits):
    d = evaluate(card_ctx(**kw), RULES)
    assert d.level == level and d.rule_hits == hits and not d.block


def test_non_money_tool_skips_amount_checks():
    d = evaluate(card_ctx(amount_fen=0, balance_fen=0), RULES)  # 不动钱：金额为 0、余额为 0 都不拦
    assert not d.block and d.reasons == ["需要你确认后才会执行"]


def test_tool_in_keeps_transfer_rules_for_transfers():
    d = evaluate(ctx(outflow_today_fen=90_000, amount_fen=20_000, facts={"direction": "loosen"}), RULES)
    assert d.rule_hits == ["cumulative_outflow"]  # 卡片规则不管转账


@pytest.mark.parametrize("stop_loss,blocked", [(False, True), (True, False)])
def test_kill_switch_and_stop_loss(stop_loss, blocked):
    d = evaluate(card_ctx(kill_switch=True, stop_loss=stop_loss), RULES)
    assert d.block is blocked


def test_context_block_reason():
    d = evaluate(ctx(block_reason="收款人信息已变化"))
    assert d.block and d.block_reason == "收款人信息已变化"


def test_rule_without_matcher_is_rejected():
    with pytest.raises(ValueError):
        validate_rules({"rules": [{"id": "no_such_rule", "elevate_to": "RED"}]})


def test_shipped_rules_are_valid():
    validate_rules(load_rules())
