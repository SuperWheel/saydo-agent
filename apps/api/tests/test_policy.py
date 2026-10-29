import pytest

from app.policy import PolicyContext, Risk, evaluate


def ctx(**kw):
    base = dict(tool="transfer.create", base_level=Risk.YELLOW, amount_fen=50_000, balance_fen=10_000_000,
                outflow_today_fen=0, payee_known=True)
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
    assert evaluate(ctx(recent_transfer_count=1)).level == Risk.YELLOW
    assert evaluate(ctx(recent_transfer_count=2)).level == Risk.RED


@pytest.mark.parametrize("kw,needle", [
    ({"kill_switch": True}, "只读保护"),
    ({"amount_fen": 0}, "大于 0"),
    ({"balance_fen": 100, "amount_fen": 101}, "余额不足"),
    ({"amount_fen": 4_000_000, "outflow_today_fen": 1_000_001}, "5 万元"),
])
def test_blocks(kw, needle):
    d = evaluate(ctx(**kw))
    assert d.block and needle in d.block_reason
