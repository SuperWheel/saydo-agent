"""事实校验（Grounding Check）：模型回复里的每个数字都要能在本轮工具结果里找到；
没有成功回执时，不能出现「已转账 / 转账成功 / 已帮你锁卡」之类的断言；
「已解约 / 已取消订阅」任何时候都不能说（本行只能停止代扣，商户侧订阅要用户自己取消）；本轮没出卡片时，不能说「已生成确认卡」；
任何回复都不能出现「保本」「稳赚」这类承诺本金或收益的说法。"""

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..money import parse_amount

_NUM = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?")
# 中文数字写的金额和百分比（「约三千元」「百分之三十」），防止绕过阿拉伯数字的校验
# 紧跟在阿拉伯数字后面的「万」是那个数的单位（「5 万元」），不单独当中文数字，见 _WAN
_CN_NUM = re.compile(r"(?<![\d.])(?<!\d )[零〇一二两三四五六七八九十百千万亿]+(?=元|块钱)|(?<=百分之)[零〇一二两三四五六七八九十点]+")
_WAN = re.compile(r" ?万")  # 「5 万元」按 50000 比对：知识库条文写「5 万元」，工具结果写 5000000 分，都记成 50000
_CLAIM = re.compile(r"(已经?|成功)(为你|帮你|给你|向.{1,6})?(转账|转出|转了|到账|完成转账|支付|扣款|汇款|汇出)|转账(已)?成功|已到账")
# 转账以外的办理（锁卡、挂失、改限额、虚拟卡、AA 收款）说成已经办好。「借记卡已锁定」「小王已付清」这类状态描述
# 不带「帮你 / 为你」，不算；「成功后」「完成后」是在讲流程，也不算
_ACTION_CLAIM = re.compile(
    r"(已经?|成功)(为你|帮你|给你)(把[^。！？\n]{0,12})?"
    r"(锁|冻结|挂失|解锁|解除|解挂|开通|打开|关闭|关掉|调整|调到|调高|调低|调为|调成|提高|降低|修改|申请|办理|开出|发送|发出"
    r"|存入|存进|存了|划入|划转|下单|下了单|订购|订好|订了|预订|预约|购买|买好|释放|退回|撤销|取消了|设置|设好|创建"
    r"|停止|停掉|停用|退订|解约|取消代扣|取消订阅|取消自动续费|取消免密)"
    r"|(锁卡|锁定|冻结|挂失|解锁|解挂|开通|关闭|调整|修改|申请|开卡|办理"
    r"|下单|订购|预订|存入|划转|释放|设置|创建|预约|停止代扣|关闭代扣|取消代扣|关闭免密|取消订阅|退订|解约)"
    r"(已经?)?(成功|完成)(?!后)"
    r"|收款(请求|通知)已经?(发送|发出|发给)"
    # 跨场景（WS7）：下单、存心愿罐、释放、定时转账也只能在用户确认后发生
    r"|已经?(下单|下好单|预订|订购|订好|存入|存进|划入|释放|退回到?活期)|订单已经?(提交|支付|付款|生成|下好)"
    # 理财（WS5）：「已为你申购」「赎回成功」「已买入了」「测评结果已保存」
    r"|(已经?|成功)(为你|帮你|给你)(把[^。！？\n]{0,12})?(申购|认购|购买|买入|买了|赎回|卖出|保存)"
    r"|(申购|认购|购买|买入|赎回|卖出)(已经?)?(成功|完成)(?!后)|已经?(申购|认购|买入|赎回|卖出)(了|成功)"
    r"|(测评|评估)结果已经?(保存|生效)"
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


def _add(out: set[str], s: str | None) -> None:
    """记一个事实数字。带小数的同时记下取整和保留一位小数的写法：「多花 2,899 元」说的就是 2,899.48。"""
    if s is None:
        return
    out.add(s)
    d = Decimal(s)
    if d != d.to_integral_value():
        for q in (Decimal(1), Decimal("0.1")):
            out.add(_norm(str(d.quantize(q, rounding=ROUND_HALF_UP))))


def _value(text: str, m: re.Match) -> str | None:
    """文字里一个数字的值；后面跟着「万」的乘上 10000。"""
    n = _norm(m.group())
    if n is not None and _WAN.match(text, m.end()):
        return _norm(str(Decimal(n) * 10_000))
    return n


def collect_facts(obj, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("_fen") and isinstance(v, int) and not isinstance(v, bool):
                _add(out, _norm(str(Decimal(v) / 100)))
            collect_facts(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            collect_facts(v, out)
    elif isinstance(obj, str):
        for m in _NUM.finditer(obj):
            _add(out, _value(obj, m))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        _add(out, _norm(str(obj)))
    return out


# 理财话术合规（WS5）：不承诺本金或收益（《商业银行理财业务监督管理办法》第二十六条）。
# 「不保本」「非保本」「不保证本金」是在提示风险，放行；其余说法不论语境一律拦，模板和产品资料里也不用这些词
_PROMISE = re.compile(r"(?<![不非])保本|稳赚|稳赢|包赚|只赚不赔|无风险|零风险|没有任何风险|没有风险|(?<!不)保证(收益|本金)"
                      r"|保收益|收益有保障|本金无忧|刚性兑付|刚兑|肯定赚|一定赚|绝对安全|有赚无赔")


def promise_words(text: str) -> list[str]:
    """文字里承诺本金或收益的说法，没有就是空列表。"""
    return [m.group() for m in _PROMISE.finditer(text or "")]


@dataclass
class Grounding:
    ok: bool
    unsupported: list[str] = field(default_factory=list)
    false_claim: bool = False
    phantom_card: bool = False  # 说出了卡片，但本轮没有生成任何卡片
    promises: list[str] = field(default_factory=list)  # 「保本」「稳赚」这类承诺收益的说法

    def describe(self) -> str:
        parts = []
        if self.unsupported:
            parts.append(f"数字 {', '.join(self.unsupported)} 在工具结果里找不到")
        if self.false_claim:
            parts.append("声称已经办理完成（转账、锁卡等），但实际还没有执行；停止代扣也不能说成已解约或已取消订阅")
        if self.phantom_card:
            parts.append("说生成了确认卡或选择卡，但本轮没有调用工具，没有任何卡片")
        if self.promises:
            parts.append(f"出现了「{'」「'.join(dict.fromkeys(self.promises))}」这类承诺本金或收益的说法，理财话术不能承诺保本或收益")
        return "；".join(parts)


def unsubscribe_claim(text: str) -> bool:
    """「已解约」「会员已取消」这类说法（前 4 个字里有否定词的除外，如「并不代表已解约」）。"""
    return any(not _NEGATION.search(text[max(0, m.start() - 4):m.start()]) for m in _UNSUB_CLAIM.finditer(text))


def check(text: str, facts: set[str], *, executed: bool = False, has_card: bool = True,
          status_only: bool = False) -> Grounding:
    """status_only：本轮只出了查询类卡片（查卡、AA 进度等），没有待确认的动作，「已锁定」「已发送」可能是在讲状态。"""
    unsupported = []
    for m in _NUM.finditer(text):
        n = _value(text, m)
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
    promises = promise_words(text)
    return Grounding(ok=not unsupported and not false_claim and not phantom_card and not promises,
                     unsupported=unsupported, false_claim=false_claim, phantom_card=phantom_card, promises=promises)
