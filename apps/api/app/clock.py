"""统一时钟。业务时间一律按北京时间计算，并支持演示用的时间快进（/dev/time-travel）。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
_offset = timedelta(0)


def now() -> datetime:
    """返回不带时区的北京时间，数据库里统一这样存。"""
    return (datetime.now(TZ) + _offset).replace(tzinfo=None, microsecond=0)


def travel(delta: timedelta) -> datetime:
    global _offset
    _offset += delta
    return now()


def reset() -> None:
    global _offset
    _offset = timedelta(0)
