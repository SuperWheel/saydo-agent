"""日期解析（技术文档 §7.6 时间解析的子集）：显式传入 now，按北京时间算。

支持：2026-11-20、11月20号、下个月20号、20号、下周五、这周六、周日、今天 / 明天 / 后天 / 大后天、每月15号。
解析不了返回 None，由上层追问，不猜。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"(\d{1,2}|[一二三四五六七八九十]{1,3})"
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "1": 0, "2": 1, "3": 2, "4": 3,
            "5": 4, "6": 5, "7": 6}

_ISO = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?")
_MONTH_DAY = re.compile(rf"(?<![\d年]){_NUM}月{_NUM}[日号]")
_NEXT_MONTH = re.compile(rf"下个?月{_NUM}[日号]")
_THIS_MONTH = re.compile(rf"(?:这|本)个?月{_NUM}[日号]")
_WEEK = re.compile(r"(下下|下|这|本)?(?:个)?(?:周|星期|礼拜)([一二三四五六日天1-7])")
_RELATIVE = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
_BARE_DAY = re.compile(rf"(?<![月\d]){_NUM}[日号]")
_MONTHLY = re.compile(rf"每个?月(?:的)?{_NUM}[日号]")


def cn_int(s: str) -> int | None:
    """1–31 的阿拉伯数字或中文数字。"""
    if s.isdigit():
        return int(s)
    if s in _CN:
        return _CN[s]
    if "十" in s:
        tens, _, ones = s.partition("十")
        t = _CN.get(tens, 1) if tens else 1
        o = _CN.get(ones, 0) if ones else 0
        return t * 10 + o
    return None


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _next_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def parse_date(text: str, at: datetime) -> date | None:
    today = at.date()
    if m := _ISO.search(text):
        return _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := _NEXT_MONTH.search(text):
        y, mo = _next_month(today.year, today.month)
        return _safe(y, mo, cn_int(m.group(1)) or 0)
    if m := _THIS_MONTH.search(text):
        return _safe(today.year, today.month, cn_int(m.group(1)) or 0)
    if m := _MONTH_DAY.search(text):
        mo, d = cn_int(m.group(1)) or 0, cn_int(m.group(2)) or 0
        got = _safe(today.year, mo, d)
        return got if got is None or got >= today else _safe(today.year + 1, mo, d)  # 已经过了就是明年
    if m := _WEEK.search(text):
        target = _WEEKDAY[m.group(2)]
        monday = today - timedelta(days=today.weekday())
        weeks = {"下下": 2, "下": 1}.get(m.group(1) or "", 0)
        got = monday + timedelta(days=7 * weeks + target)
        return got if got >= today or weeks else got + timedelta(days=7)  # 「周一」说在周五：指下周一
    for word in sorted(_RELATIVE, key=len, reverse=True):
        if word in text:
            return today + timedelta(days=_RELATIVE[word])
    if m := _BARE_DAY.search(text):
        d = cn_int(m.group(1)) or 0
        got = _safe(today.year, today.month, d)
        if got is not None and got >= today:
            return got
        y, mo = _next_month(today.year, today.month)
        return _safe(y, mo, d)
    return None


@dataclass
class When:
    recurrence: str  # once / monthly
    first: datetime  # 首次执行时间
    day_of_month: int | None = None

    @property
    def label(self) -> str:
        if self.recurrence == "monthly":
            return f"每月 {self.day_of_month} 日 {self.first:%H:%M}"
        return f"{self.first:%Y-%m-%d %H:%M}"


RUN_HOUR = 10  # 定时任务统一在当天 10:00 执行（演示参数）


def next_monthly(day: int, after: datetime) -> datetime:
    """after 之后（不含）第一个「每月 day 号 10:00」。29–31 号遇到小月按月末执行。"""
    y, m = after.year, after.month
    for _ in range(13):
        last = (_safe(*_next_month(y, m), 1) - timedelta(days=1)).day
        at = datetime(y, m, min(day, last), RUN_HOUR)
        if at > after:
            return at
        y, m = _next_month(y, m)
    raise ValueError(day)


def parse_when(text: str, at: datetime) -> When | None:
    """定时转账的执行时间：每月 N 号，或者某一天（只认今天以后）。"""
    if m := _MONTHLY.search(text):
        day = cn_int(m.group(1))
        if day is None or not 1 <= day <= 31:
            return None
        return When("monthly", next_monthly(day, at), day)
    d = parse_date(text, at)
    if d is None or d <= at.date():
        return None
    return When("once", datetime(d.year, d.month, d.day, RUN_HOUR))
