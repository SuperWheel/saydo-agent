"""测试时钟固定墙上时间，但演示日期、重置、跨零点快进仍走真实业务实现。"""

from datetime import date, timedelta

from app import clock


def test_reset_and_travel_keep_the_test_anchor(monkeypatch):
    start = clock.now()
    assert start.tzinfo is None and start.microsecond == 0
    assert clock.travel(timedelta(days=1, seconds=2)) == start + timedelta(days=1, seconds=2)
    clock.reset()
    assert clock.now() == start
    monkeypatch.setenv("DEMO_DATE", "2027-02-28")
    clock.reset()
    assert clock.now().date() == date(2027, 2, 28) and clock.now().time() == start.time()
    assert clock.travel(timedelta(days=1)).date() == date(2027, 3, 1)
    clock.reset()
    assert clock.now().date() == date(2027, 2, 28)
