"""事实校验（Grounding Check）：模型回复里的每个数字都要能在本轮工具结果里找到；
没有成功回执时，不能出现「已转账 / 转账成功」之类的断言。"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_NUM = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?")
_CLAIM = re.compile(r"(已经?|成功)(为你|帮你|给你|向.{1,6})?(转账|转出|转了|到账|完成转账|支付|扣款|汇款|汇出)|转账(已)?成功|已到账")


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

    def describe(self) -> str:
        parts = []
        if self.unsupported:
            parts.append(f"数字 {', '.join(self.unsupported)} 在工具结果里找不到")
        if self.false_claim:
            parts.append("声称已经完成转账，但实际还没有执行")
        return "；".join(parts)


def check(text: str, facts: set[str], *, executed: bool = False) -> Grounding:
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
    false_claim = bool(_CLAIM.search(text)) and not executed
    return Grounding(ok=not unsupported and not false_claim, unsupported=unsupported, false_claim=false_claim)
