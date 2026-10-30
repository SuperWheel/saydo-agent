"""统一时钟。业务时间一律按北京时间计算，并支持演示用的时间快进（/dev/time-travel）。

DEMO_DATE=2026-11-02：把「今天」整体挪到这一天（时分秒照真实时间走），种子流水、账单区间、订阅扣费日都跟着变。
用来提前彩排演示当天的样子，也用来在不同日期上跑测试（月初、月末这类边界）。
"""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def _base() -> timedelta:
    raw = os.environ.get("DEMO_DATE", "").strip()
    if not raw:
        return timedelta(0)
    try:
        target = date.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(f"DEMO_DATE 应形如 2026-11-02，现在是 {raw!r}") from e
    return timedelta(days=(target - datetime.now(TZ).date()).days)


_offset = _base()


def now() -> datetime:
    """返回不带时区的北京时间，数据库里统一这样存。"""
    return (datetime.now(TZ) + _offset).replace(tzinfo=None, microsecond=0)


def travel(delta: timedelta) -> datetime:
    global _offset
    _offset += delta
    return now()


def reset() -> None:
    """回到「今天」：没设 DEMO_DATE 就是真实日期。"""
    global _offset
    _offset = _base()
