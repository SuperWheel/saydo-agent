"""事实校验（Grounding Check）：模型回复里的每个数字都要能在本轮工具结果里找到；
没有成功回执时，不能出现「已转账 / 转账成功」之类的断言；本轮没出卡片时，不能说「已生成确认卡」。"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..money import parse_amount

_NUM = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?")
# 中文数字写的金额和百分比（「约三千元」「百分之三十」），防止绕过阿拉伯数字的校验
_CN_NUM = re.compile(r"[零〇一二两三四五六七八九十百千万亿]+(?=元|块钱)|(?<=百分之)[零〇一二两三四五六七八九十点]+")
_CLAIM = re.compile(r"(已经?|成功)(为你|帮你|给你|向.{1,6})?(转账|转出|转了|到账|完成转账|支付|扣款|汇款|汇出)|转账(已)?成功|已到账")
# 「已生成确认卡」「请在选择卡中确认」这类说法；单纯提到确认卡（如解释它是什么）不算
_CARD_CLAIM = re.compile(r"(生成|发起|创建|准备)[^。！？\n]{0,8}(确认卡|选择卡)|(确认卡|选择卡)[^。！？\n]{0,3}(已|如下|在下方)"
                         r"|请[^。！？\n]{0,8}(确认卡|选择卡)")


def _norm(s: str) -> str | None:
    try:
        return format(Decimal(s.replace(",", "")).normalize(), "f")
    except InvalidOperation:
        return None


def collect_facts(obj, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("_fen") and isinstance(v, int) and not isinstance(v, bool):
                out.add(_norm(str(Decimal(v) / 100)))
            collect_facts(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            collect_facts(v, out)
    elif isinstance(obj, str):
        out.update(n for n in (_norm(m) for m in _NUM.findall(obj)) if n)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.add(_norm(str(obj)))
    return out


@dataclass
class Grounding:
    ok: bool
    unsupported: list[str] = field(default_factory=list)
    false_claim: bool = False
    phantom_card: bool = False  # 说出了卡片，但本轮没有生成任何卡片

    def describe(self) -> str:
        parts = []
        if self.unsupported:
            parts.append(f"数字 {', '.join(self.unsupported)} 在工具结果里找不到")
        if self.false_claim:
            parts.append("声称已经完成转账，但实际还没有执行")
        if self.phantom_card:
            parts.append("说生成了确认卡或选择卡，但本轮没有调用工具，没有任何卡片")
        return "；".join(parts)


def check(text: str, facts: set[str], *, executed: bool = False, has_card: bool = True) -> Grounding:
    unsupported = []
    for m in _NUM.finditer(text):
        n = _norm(m.group())
        if n is None or n in facts:
            continue
        following = text[m.end():m.end() + 1]
        # 序号、笔数、「2 分钟」这类 10 以内的整数放行；带「元 / 块 / %」的不放行
        if n.isdigit() and int(n) <= 10 and following not in ("元", "块", "%"):
            continue
        unsupported.append(m.group())
    for m in _CN_NUM.finditer(text):
        fen = parse_amount(m.group())
        if fen is None or _norm(str(Decimal(fen) / 100)) not in facts:
            unsupported.append(m.group())
    false_claim = bool(_CLAIM.search(text)) and not executed
    phantom_card = not has_card and bool(_CARD_CLAIM.search(text))
    return Grounding(ok=not unsupported and not false_claim and not phantom_card, unsupported=unsupported,
                     false_claim=false_claim, phantom_card=phantom_card)
