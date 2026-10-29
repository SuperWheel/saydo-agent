"""金额工具：分与元的转换、中文大写、口语金额解析。全部是确定性代码，不交给模型。"""

import re
from decimal import Decimal, InvalidOperation

_CAP_DIGITS = "零壹贰叁肆伍陆柒捌玖"
_CAP_UNITS = ("", "拾", "佰", "仟")
_CAP_BIGS = ("", "万", "亿", "万亿")


def fmt_yuan(fen: int) -> str:
    """12000 → '120.00'；带千分位。"""
    sign = "-" if fen < 0 else ""
    yuan, rem = divmod(abs(fen), 100)
    return f"{sign}{yuan:,}.{rem:02d}"


def _cap_section(g: int) -> str:
    s, zero = "", False
    for pos in (3, 2, 1, 0):
        d = g // 10**pos % 10
        if d == 0:
            zero = bool(s)
        else:
            if zero:
                s += "零"
                zero = False
            s += _CAP_DIGITS[d] + _CAP_UNITS[pos]
    return s


def _cap_int(n: int) -> str:
    groups = []
    while n:
        n, g = divmod(n, 10000)
        groups.append(g)
    out, skipped = "", False
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            skipped = bool(out)
            continue
        if out and (g < 1000 or skipped):
            out += "零"
        out += _cap_section(g) + _CAP_BIGS[i]
        skipped = False
    return out


def to_capital(fen: int) -> str:
    """金额大写：120050 → '壹仟贰佰元伍角'。"""
    if fen < 0:
        raise ValueError("金额不能为负")
    if fen == 0:
        return "零元整"
    yuan, rem = divmod(fen, 100)
    jiao, fen_ = divmod(rem, 10)
    out = _cap_int(yuan) + "元" if yuan else ""
    if jiao:
        out += _CAP_DIGITS[jiao] + "角"
    elif yuan and fen_:
        out += "零"
    if fen_:
        out += _CAP_DIGITS[fen_] + "分"
    if not jiao and not fen_:
        out += "整"
    return out


_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2, "三": 3, "叁": 3, "四": 4, "肆": 4,
    "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_CN_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000, "k": 1000}
_CN_BIG = {"万": 10_000, "萬": 10_000, "w": 10_000, "亿": 100_000_000}
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖](?:点[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖]+)?|[十拾百佰千仟k万萬w亿]")
_STRIP = re.compile(r"[¥￥,，\s]|人民币|rmb|cny")


def _cn_number(token: str) -> Decimal:
    if token[0].isdigit():
        return Decimal(token)
    if "点" in token:
        head, tail = token.split("点")
        return Decimal(f"{_CN_DIGIT[head]}.{''.join(str(_CN_DIGIT[c]) for c in tail)}")
    return Decimal(_CN_DIGIT[token])


def _parse_integer_part(text: str) -> Decimal | None:
    tokens = _TOKEN.findall(text)
    if not tokens or "".join(tokens) != text:
        return None
    total = section = Decimal(0)
    num: Decimal | None = None
    last_unit: int | None = None
    after_zero = False
    for tok in tokens:
        if tok in _CN_UNIT:
            section += (num if num is not None else Decimal(1)) * _CN_UNIT[tok]
            last_unit, num, after_zero = _CN_UNIT[tok], None, False
        elif tok in _CN_BIG:
            section += num or 0
            total += (section or Decimal(1)) * _CN_BIG[tok]
            section, num, last_unit, after_zero = Decimal(0), None, _CN_BIG[tok], False
        else:
            value = _cn_number(tok)
            if num is not None:  # 「一二」这类连写数字不认
                return None
            if value == 0 and not tok[0].isdigit():
                after_zero = True
                continue
            num = value
    if num is not None:
        # 口语省略：「两千五」= 2500，「一万二」= 12000；「一千零五」= 1005
        if last_unit and not after_zero and num < 10 and num == num.to_integral_value():
            section += num * last_unit // 10
        else:
            section += num
    return total + section


def parse_amount(text: str) -> int | None:
    """把用户说的金额解析成分。解析不了或有歧义时返回 None，由上层追问。

    支持：500、1,200.50、1.5w、2k、两千五、一万二、一千零五、两块五、3块2毛5、五百元。
    """
    s = _STRIP.sub("", text.strip().lower()).removesuffix("元整").rstrip("元")
    if not s:
        return None
    jiao = fen = Decimal(0)
    m = re.fullmatch(r"(.+?)(?:块|元)(?:([零一二两三四五六七八九\d])(?:毛|角)?)?(?:([零一二两三四五六七八九\d])分?)?(?:钱)?", s)
    if m:
        s = m.group(1)
        if m.group(2):
            jiao = Decimal(_CN_DIGIT.get(m.group(2), m.group(2)))
        if m.group(3):
            fen = Decimal(_CN_DIGIT.get(m.group(3), m.group(3)))
    else:
        s = s.removesuffix("块钱").removesuffix("块")
    try:
        yuan = _parse_integer_part(s)
    except (InvalidOperation, KeyError, ValueError):
        return None
    if yuan is None:
        return None
    total = yuan * 100 + jiao * 10 + fen
    if total <= 0 or total != total.to_integral_value():
        return None  # 小数超过两位，不猜
    return int(total)
