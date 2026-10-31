"""统计区间解析：把「上个月」「9月」「1月到3月」「一季度」「最近7天」这类说法变成确定的起止时间，以及环比用的上一期。

一律按北京时间、左闭右开 [start, end)。没识别出来时用调用方给的默认区间，并标记 recognized=False。
"""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

_CN = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0}
_UNIT_DAYS = {"天": 1, "日": 1, "周": 7, "星期": 7, "礼拜": 7, "月": 30, "年": 365}


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime
    label: str
    prev_start: datetime
    prev_end: datetime
    prev_label: str
    recognized: bool = True


def _int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if "百" in s:  # 「一百」「两百」「一百零五」「一百二」「一百二十五」
        h, _, rest = s.partition("百")
        hundreds = _CN.get(h) if h else 1
        if not rest:
            tail = 0
        elif rest[0] == "零":
            tail = _int(rest[1:])
        elif len(rest) == 1:
            tail = _CN.get(rest, 0) * 10 or None
        else:
            tail = _int(rest)
        return hundreds * 100 + tail if hundreds and tail is not None else None
    if s == "十":
        return 10
    if len(s) == 2 and s[0] == "十" and s[1] in _CN:
        return 10 + _CN[s[1]]
    if len(s) == 2 and s[1] == "十" and s[0] in _CN:
        return _CN[s[0]] * 10
    if len(s) == 3 and s[1] == "十" and s[0] in _CN and s[2] in _CN:
        return _CN[s[0]] * 10 + _CN[s[2]]
    return _CN.get(s) or None


def _month_start(y: int, m: int) -> datetime:
    return datetime(y, m, 1)


def _add_months(d: datetime, n: int) -> datetime:
    idx = d.year * 12 + d.month - 1 + n
    return datetime(idx // 12, idx % 12 + 1, 1)


def _month(y: int, m: int, at: datetime) -> Period:
    start = _month_start(y, m)
    end = _add_months(start, 1)
    prev_start = _add_months(start, -1)
    if end <= at:
        return Period(start, end, f"{y}年{m}月", prev_start, start, f"{prev_start.month}月")
    # 当月还没过完：和上月同期比
    elapsed = at - start
    prev_end = min(prev_start + elapsed, start)
    return Period(start, at, f"{y}年{m}月（截至{at.day}日）", prev_start, prev_end, "上月同期")


def _months(y: int, m1: int, m2: int, at: datetime, label: str, prev_label: str) -> Period:
    """连续几个月（含首尾），和紧挨着的前一段同样长的月份比。还没过完时截至今天，和上一段同期比。"""
    start, n = _month_start(y, m1), m2 - m1 + 1
    end, prev_start = _add_months(start, n), _add_months(start, -n)
    if end <= at:
        return Period(start, end, label, prev_start, start, prev_label)
    return Period(start, at, f"{label}（截至{at.month}月{at.day}日）", prev_start, min(prev_start + (at - start), start),
                  f"{prev_label}同期")


def _year_of(prefix: str | None, first_month: int, at: datetime) -> int:
    """「去年」或者说的月份还没到（11 月问「12月」）就是去年。"""
    return at.year - 1 if prefix == "去年" or (prefix is None and first_month > at.month) else at.year


def _rolling(days: int, at: datetime, label: str) -> Period:
    start = at - timedelta(days=days)
    return Period(start, at, label, start - timedelta(days=days), start, f"前{days}天")


_M = r"(\d{1,2}|十[一二]?|[一二三四五六七八九])"
_MONTH_RANGE = re.compile(rf"(今年|去年)?{_M}月份?(?:到|至|~|～|-|—|－)+{_M}月")
_QUARTER = re.compile(r"(今年|去年)?第?([一二三四1-4])季度")
_YM = re.compile(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*月?")
_N = r"(\d{1,3}|[一两二三四五六七八九十百零半]{1,5})"
_UNIT = r"(天|日|周|星期|礼拜|月|年)"
# 「近30天」「最近的两周」「过去这三个月」「最近一百天」
_RECENT = re.compile(rf"(?:最近|近|过去|前)(?:的|这)?{_N}(?:个)?{_UNIT}")
# 「这30天」「这两周」「30天内」「三个月以内」：「这个月」是本月、「3月以来」是从 3 月起，所以月份要带「个」
_N_UNIT = rf"{_N}(?:(个)月|个?(天|周|星期|礼拜|年))"
_THIS_N = re.compile(rf"这{_N_UNIT}")
_WITHIN = re.compile(rf"{_N_UNIT}(?:内|以内|之内|以来)")
_MONTH = re.compile(r"(今年|去年)?\s*(\d{1,2}|十[一二]?|[一二三四五六七八九])\s*月份?(?!份额)")


def _recent(t: str, at: datetime) -> Period | None:
    """滚动区间：「近N天 / 周 / 个月 / 年」按 1 / 7 / 30 / 365 天折算，「半年」182 天，「半个月」15 天。"""
    if m := _RECENT.search(t):
        n, unit = m.group(1), m.group(2)
    elif m := _THIS_N.search(t) or _WITHIN.search(t):
        n, unit = m.group(1), "月" if m.group(2) else m.group(3)
    else:
        return None
    if n == "半":
        days = {"年": 182, "月": 15}.get(unit)
        return _rolling(days, at, {"年": "近半年", "月": "近半个月"}[unit]) if days else None
    if (k := _int(n)) and k > 0:
        days = k * _UNIT_DAYS[unit]
        if days <= 3650:  # 十年以内；再长就不是「最近」了，也会超出日期范围
            return _rolling(days, at, f"近{days}天")
    return None


def parse_period(text: str | None, *, at: datetime, default: str) -> Period:
    """识别 text 里的时间说法；识别不了就按 default（同样的写法，如「近30天」「上个月」）。"""
    p = _parse(text or "", at)
    if p is not None:
        return p
    p = _parse(default, at)
    assert p is not None, default
    return Period(**{**p.__dict__, "recognized": not (text or "").strip()})


def has_period(text: str | None) -> bool:
    return _parse(text or "", datetime(9000, 6, 15)) is not None


def _parse(text: str, at: datetime) -> Period | None:
    t = text.replace(" ", "")
    if m := _YM.search(t):
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and _month_start(y, mo) <= at:
            return _month(y, mo, at)
    if p := _recent(t, at):
        return p
    if "上上个月" in t or "上上月" in t:
        d = _add_months(_month_start(at.year, at.month), -2)
        return _month(d.year, d.month, at)
    if re.search(r"上个?月", t):
        d = _add_months(_month_start(at.year, at.month), -1)
        return _month(d.year, d.month, at)
    if re.search(r"(本|这个?|当)月", t):
        return _month(at.year, at.month, at)
    if m := _MONTH_RANGE.search(t):
        m1, m2 = _int(m.group(2)), _int(m.group(3))
        if m1 and m2 and 1 <= m1 < m2 <= 12:
            y = _year_of(m.group(1), m1, at)
            return _months(y, m1, m2, at, f"{y}年{m1}月—{m2}月", f"前{m2 - m1 + 1}个月")
    if m := _QUARTER.search(t):
        q = _int(m.group(2))
        y = _year_of(m.group(1), q * 3 - 2, at)
        return _months(y, q * 3 - 2, q * 3, at, f"{y}年第{q}季度", "上季度")
    if m := _MONTH.search(t):
        mo = _int(m.group(2))
        if mo and 1 <= mo <= 12:
            return _month(_year_of(m.group(1), mo, at), mo, at)
    today = datetime.combine(at.date(), time())
    if re.search(r"上(个)?(周|星期|礼拜)", t):
        start = today - timedelta(days=today.weekday() + 7)
        return Period(start, start + timedelta(days=7), "上周", start - timedelta(days=7), start, "上上周")
    if re.search(r"(本|这个?)(周|星期|礼拜)", t):
        start = today - timedelta(days=today.weekday())
        return Period(start, at, "本周", start - timedelta(days=7), start - timedelta(days=7) + (at - start), "上周同期")
    if "昨天" in t or "昨日" in t:
        y = today - timedelta(days=1)
        return Period(y, today, "昨天", y - timedelta(days=1), y, "前天")
    if "今天" in t or "今日" in t:
        return Period(today, at, "今天", today - timedelta(days=1), at - timedelta(days=1), "昨天同期")
    if "今年" in t or "本年" in t:
        start = datetime(at.year, 1, 1)
        return Period(start, at, f"{at.year}年（截至{at.month}月{at.day}日）", datetime(at.year - 1, 1, 1),
                      datetime(at.year - 1, at.month, 1) + (at - _month_start(at.year, at.month)), "去年同期")
    if "去年" in t:
        start = datetime(at.year - 1, 1, 1)
        return Period(start, datetime(at.year, 1, 1), f"{at.year - 1}年", datetime(at.year - 2, 1, 1), start,
                      f"{at.year - 2}年")
    return None
