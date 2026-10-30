"""事实校验（Grounding Check）：模型回复里的每个数字都要能在本轮工具结果里找到；
没有成功回执时，不能出现「已转账 / 转账成功 / 已帮你锁卡」之类的断言；
「已解约 / 已取消订阅」任何时候都不能说（本行只能停止代扣，商户侧订阅要用户自己取消）；本轮没出卡片时，不能说「已生成确认卡」。"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..money import parse_amount

_NUM = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?")
# 中文数字写的金额和百分比（「约三千元」「百分之三十」），防止绕过阿拉伯数字的校验
_CN_NUM = re.compile(r"[零〇一二两三四五六七八九十百千万亿]+(?=元|块钱)|(?<=百分之)[零〇一二两三四五六七八九十点]+")
_CLAIM = re.compile(r"(已经?|成功)(为你|帮你|给你|向.{1,6})?(转账|转出|转了|到账|完成转账|支付|扣款|汇款|汇出)|转账(已)?成功|已到账")
# 转账以外的办理（锁卡、挂失、改限额、虚拟卡、AA 收款）说成已经办好。「借记卡已锁定」「小王已付清」这类状态描述
# 不带「帮你 / 为你」，不算；「成功后」「完成后」是在讲流程，也不算
_ACTION_CLAIM = re.compile(
    r"(已经?|成功)(为你|帮你|给你)(把[^。！？\n]{0,12})?"
    r"(锁|冻结|挂失|解锁|解除|解挂|开通|打开|关闭|关掉|调整|调到|调高|调低|调为|调成|提高|降低|修改|申请|办理|开出|发送|发出"
    r"|停止|停掉|停用|退订|解约|取消代扣|取消订阅|取消自动续费|取消免密)"
    r"|(锁卡|锁定|冻结|挂失|解锁|解挂|开通|关闭|调整|修改|申请|开卡|办理|停止代扣|关闭代扣|取消代扣|关闭免密|取消订阅|退订|解约)"
    r"(已经?)?(成功|完成)(?!后)"
    r"|收款(请求|通知)已经?(发送|发出|发给)"
    # 订阅（WS6）：「已停止代扣」「代扣已停止」。查订阅时讲协议状态不算（status_only）
    r"|已经?(停止|停掉|停用|关闭|关掉|取消)了?[^。！？\n]{0,8}(代扣|免密|自动扣费|自动扣款)"
    r"|(代扣|免密支付|自动扣费|自动扣款)(授权|协议)?(也)?已经?(停止|停掉|停用|关闭|取消)")
# 订阅（WS6）：说成「已解约」「已取消订阅」。本行只能停止代扣授权、商户侧订阅要用户自己取消，
# 所以这类说法在任何时候都不成立：执行过停止代扣、或本轮只出了状态卡，也一样拦。前面带否定词的（「不等于已解约」）不算
_UNSUB_CLAIM = re.compile(
    r"(已经?|成功)(为你|帮你|给你)?(把[^。！？\n]{0,12})?(解约|退订|取消了?[^。！？\n]{0,8}?(订阅|会员|自动续费|连续包月))"
    r"|(解约|退订|取消订阅)(已经?)?(成功|完成)(?!后)"
    r"|(订阅|会员|自动续费|连续包月)(也)?已经?(取消|解约|退订|关闭)(?![^。！？\n]{0,4}(代扣|免密))")
_NEGATION = re.compile(r"[不没未非无别]")
# 「已生成确认卡」「请在选择卡中确认」这类说法，同一句之内都算（「已再生成一笔给王*英的300元转账确认卡」）；
# 单纯提到确认卡（如解释它是什么）不算
_CARD_CLAIM = re.compile(r"(生成|发起|创建|准备|出具|出示)[^。！？\n]{0,30}(确认卡|选择卡)"
                         r"|(确认卡|选择卡)[^。！？\n]{0,3}(已|如下|在下方)|(下方|下面)的?(确认卡|选择卡)"
                         r"|请[^。！？\n]{0,12}(确认卡|选择卡)")


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
            parts.append("声称已经办理完成（转账、锁卡等），但实际还没有执行；停止代扣也不能说成已解约或已取消订阅")
        if self.phantom_card:
            parts.append("说生成了确认卡或选择卡，但本轮没有调用工具，没有任何卡片")
        return "；".join(parts)


def unsubscribe_claim(text: str) -> bool:
    """「已解约」「会员已取消」这类说法（前 4 个字里有否定词的除外，如「并不代表已解约」）。"""
    return any(not _NEGATION.search(text[max(0, m.start() - 4):m.start()]) for m in _UNSUB_CLAIM.finditer(text))


def check(text: str, facts: set[str], *, executed: bool = False, has_card: bool = True,
          status_only: bool = False) -> Grounding:
    """status_only：本轮只出了查询类卡片（查卡、AA 进度等），没有待确认的动作，「已锁定」「已发送」可能是在讲状态。"""
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
    false_claim = not executed and bool(_CLAIM.search(text) or (not status_only and _ACTION_CLAIM.search(text)))
    false_claim = false_claim or unsubscribe_claim(text)
    phantom_card = not has_card and bool(_CARD_CLAIM.search(text))
    return Grounding(ok=not unsupported and not false_claim and not phantom_card, unsupported=unsupported,
                     false_claim=false_claim, phantom_card=phantom_card)
