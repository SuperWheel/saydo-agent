import pytest

from app.money import fmt_yuan, parse_amount, to_capital


@pytest.mark.parametrize("text,fen", [
    ("500", 50_000), ("500元", 50_000), ("500块", 50_000), ("500块钱", 50_000), ("¥1,200.50", 120_050),
    ("1.5w", 1_500_000), ("2k", 200_000), ("五百", 50_000), ("两千五", 250_000), ("一万二", 1_200_000),
    ("一千零五", 100_500), ("十五", 1_500), ("一百五", 15_000), ("两千五百", 250_000), ("1千2", 120_000),
    ("两块五", 250), ("3块2毛5", 325), ("一点五万", 1_500_000), ("壹仟贰佰元整", 120_000),
])
def test_parse_amount(text, fen):
    assert parse_amount(text) == fen


@pytest.mark.parametrize("text", ["", "点", "一二", "0", "0.001", "很多", "-5"])
def test_parse_amount_rejects(text):
    assert parse_amount(text) is None


@pytest.mark.parametrize("fen,cap", [
    (50_000, "伍佰元整"), (120_000, "壹仟贰佰元整"), (100_500, "壹仟零伍元整"), (120_050, "壹仟贰佰元伍角"),
    (5, "伍分"), (10_005, "壹佰元零伍分"), (1_000, "壹拾元整"), (10_000_500_000, "壹亿零伍仟元整"),
    (1_000_500_000, "壹仟万伍仟元整"), (1_000_050_000, "壹仟万零伍佰元整"),
])
def test_to_capital(fen, cap):
    assert to_capital(fen) == cap


def test_fmt_yuan():
    assert fmt_yuan(2_865_032) == "28,650.32"
    assert fmt_yuan(5) == "0.05"
