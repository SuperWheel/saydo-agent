"""意图与槽位的确定性部分：收款人消歧、金额来源校验、规则兜底意图。"""

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Contact
from ..money import parse_amount

_AMOUNT_CHARS = r"\d零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万萬wWkK亿点.,"
_AMOUNT_SPAN = re.compile(rf"[¥￥]?[{_AMOUNT_CHARS}]+(?:块|元)?(?:[零一二两三四五六七八九\d](?:毛|角)?)?(?:[零一二两三四五六七八九\d]分)?(?:块钱|块|元)?")


def amounts_in(text: str) -> set[int]:
    """从用户原话里找出所有能解析的金额（分）。"""
    found = set()
    for m in _AMOUNT_SPAN.finditer(text):
        fen = parse_amount(m.group())
        if fen:
            found.add(fen)
    return found


def amount_from_user(amount_fen: int, user_texts: list[str]) -> bool:
    """数据来源校验（借鉴 CaMeL）：金额必须能在用户最近说的话里找到，不能是模型自己编的。"""
    return any(amount_fen in amounts_in(t) for t in user_texts)


_HINT_PREFIX = re.compile(r"^(转给|给|向|帮我给|帮我)")
_HINT_SUFFIX = re.compile(r"(的账户|账户|的卡|那个|这个)$")


def normalize_hint(hint: str) -> str:
    h = _HINT_SUFFIX.sub("", _HINT_PREFIX.sub("", hint.strip()))
    return h.strip()


def resolve_payee(db: Session, user_id: int, hint: str) -> list[Contact]:
    """按「户名精确 → 别名 → 备注（同事、房东）→ 户名包含」的顺序在收款人主数据里查找。

    多个候选时由上层追问，不替用户选。
    """
    h = normalize_hint(hint)
    if not h:
        return []
    contacts = list(db.scalars(select(Contact).where(Contact.user_id == user_id)))
    bare = h.removeprefix("我的").removeprefix("我")
    tail = _tail_in(h)
    for match in (
        lambda c: c.name == h,
        lambda c: h in c.aliases or bare in c.aliases,
        lambda c: c.note in (h, bare),
        lambda c: len(h) >= 2 and (h in c.name or c.name in h),
        lambda c: c.tail == tail,  # 同名追问后只答尾号：「尾号0921的」
    ):
        hits = [c for c in contacts if match(c)]
        if len(hits) > 1:  # 同名追问后连备注 / 尾号一起答：「同事张伟」「张伟尾号0921」
            hits = narrow_payees(hits, h)
        if hits:
            return sorted(hits, key=lambda c: (c.first_transfer_at is None, c.id))
    return []


_TAIL = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _tail_in(text: str) -> str | None:
    return (m := _TAIL.search(text)) and m.group(1)


def narrow_payees(candidates: list[Contact], text: str) -> list[Contact]:
    """同名候选里按话里提到的备注或尾号再筛一次；筛不出来就原样返回，由上层追问。"""
    tail = _tail_in(text)
    return [c for c in candidates if c.tail == tail or c.note and c.note in text] or candidates


@dataclass
class RuleIntent:
    intent: str  # balance / transfer / bill_* / unknown
    slots: dict = field(default_factory=dict)


_BALANCE = re.compile(r"余额|还有多少钱|有多少钱|还剩多少|剩多少钱|多少存款|卡里.*钱")
_AMT = rf"(?P<amount>[¥￥]?[{_AMOUNT_CHARS}]+(?:块|元)?(?:[零一二两三四五六七八九\d](?:毛|角)?)?)(?:块钱|块|元)?"
_TRANSFER = [
    re.compile(rf"(?:给|向)(?P<payee>[^\s，,。！!？?]{{1,10}}?)(?:转账|转|打)(?:钱|款)?{_AMT}"),
    re.compile(rf"转(?:账)?{_AMT}(?:给|到)(?P<payee>[^\s，,。！!？?]{{1,10}})"),
]
_TRANSFER_NO_AMOUNT = re.compile(r"(?:给|向)(?P<payee>[^\s，,。！!？?]{1,10}?)(?:转账|转点钱|转钱|转|打钱)")
_MEMO = re.compile(r"(?:备注|用途|写上)[是为:：]?\s*(?P<memo>[^\s，,。！!？?]{1,20})")


def rule_intent(text: str) -> RuleIntent:
    """模型不可用时的兜底识别，只覆盖 M0 的两条链路。"""
    memo = (m := _MEMO.search(text)) and m.group("memo")
    from .transfer import asks_aa_status, parse_aa, phone_transfer_slots  # 转账补全：手机号、AA

    if slots := phone_transfer_slots(text):  # 槽位里是脱敏号码，提议工具再从原话还原
        return RuleIntent("transfer", {**slots, "memo": memo})
    if _faq_intent(text):  # 知识问答（WS9）排在 AA、理财、订阅、卡片之前：「AA 收款怎么操作」「信用卡挂失要钱吗」问的是规则，不是办理
        return RuleIntent("faq")
    if asks_aa_status(text):
        return RuleIntent("aa_status")
    if slots := parse_aa(text):
        return RuleIntent("aa", slots)
    for pat in _TRANSFER:
        if m := pat.search(text):
            return RuleIntent("transfer", {"payee_hint": m.group("payee"), "amount_text": m.group("amount"), "memo": memo})
    if wealth := _wealth_intent(text):  # 理财（WS5）：「理财这个月赚了多少」不能被账单的「赚了多少」抢走
        return wealth
    if sub := _sub_intent(text):  # 订阅（WS6）排在账单之前：「我每个月订阅花多少」不是查流水
        return sub
    if bill := next((name for name, pat in _BILL if pat.search(text)), None):  # 账单类问句排在「给我妈转了多少」之前
        return RuleIntent(bill)
    if m := _TRANSFER_NO_AMOUNT.search(text):
        return RuleIntent("transfer", {"payee_hint": m.group("payee"), "amount_text": "", "memo": memo})
    if _BALANCE.search(text):
        return RuleIntent("balance")
    if card := _card_intent(text):
        return card
    return RuleIntent("unknown")


# 账单分析（WS2）：只认问句，参数由 tools/bill.py 的 rule_turn 从原话里组织
_BILL = [
    ("bill_report", re.compile(r"报告|月报|年报|账单总结|收支(情况|总结)|总结一下.{0,6}(账单|消费|收支)")),
    ("bill_anomalies", re.compile(r"异常|可疑|盗刷|被刷|不是我(本人)?(花|消费|买)|重复扣|扣了两次|多扣")),
    ("bill_categories", re.compile(r"分类|类别|占比|饼图|花(在|到)哪|都花(在)?哪|钱去哪|消费结构|各类|哪类")),
    ("bill_query", re.compile(r"(花|消费|支出|转|付|收|赚|进账|入账)了?多少|(工资|收入|支出|消费)(是|有|发了)?多少"
                              r"|(最近|上|最后|最大|最贵)的?一笔|流水|账单|明细"
                              r"|(转账|交易|消费|支付)记录")),
]
# ---------- 卡片（WS3）：锁卡、解锁、挂失、「卡丢了」、交易限制、虚拟卡、查卡 ----------
_CARD_HINT = re.compile(r"尾号\s*\d{4}|\d{4}(?=\s*(?:的|那张)?(?:卡|借记卡|储蓄卡|信用卡|虚拟卡))|借记卡|储蓄卡|工资卡|信用卡|虚拟卡")
_CARD_UNLOCK = re.compile(r"解锁|解除.{0,6}(锁定|锁卡|冻结|挂失)|解挂|取消.{0,4}(锁卡|锁定|挂失)|恢复使用")
_CARD_REPORT = re.compile(r"挂失")
_CARD_LOST = re.compile(r"卡.{0,8}(丢了|丢失|掉了|不见了|找不到|被偷|被盗|被人捡)|(丢|掉)了.{0,4}卡|盗刷")
_CARD_LOCK = re.compile(r"锁.{0,10}卡|锁定|锁住|锁一下|锁上|冻结|(把|将).{0,12}锁")
_CARD_VIRTUAL = re.compile(r"(申请|办|开|要|来|弄|新建|生成).{0,12}虚拟卡")
_CARD_SETTINGS = [
    ("overseas", re.compile(r"境外|海外|国外|出境")),
    ("online", re.compile(r"无卡|线上|网上|网络支付|网购|在线支付|快捷支付")),
    ("atm", re.compile(r"ATM|atm|取现|取款")),
    ("per_txn", re.compile(r"单笔")),
    ("daily", re.compile(r"单日|每日|每天|日累计")),
]
_SWITCH_OFF = re.compile(r"关闭|关掉|关了|关上|停用|禁止|禁用|不允许|不让|屏蔽|停掉")
_SWITCH_ON = re.compile(r"开通|开启|打开|允许|启用|开放|放开|恢复")
_CARD_LIST = re.compile(r"我的卡|几张卡|哪些卡|查.{0,3}卡|卡.{0,4}(状态|情况|限额|额度)|限额|额度")
_MERCHANT = re.compile(r"(?:只在|仅在|仅限|只限|专门在|只能在)(?P<m>[^\s，,。！!？?的]{1,10}?)(?:上|里)?(?:用|使用|消费|购物|买|的|$)")
_COUNTER = re.compile(r"\s*(?:张|个|次|笔|天|日)")


def card_hint(text: str) -> str:
    """话里指明是哪张卡的部分（尾号、卡种），如「尾号6688」「信用卡」；没有就是空串。"""
    return "".join(_CARD_HINT.findall(text))


def card_amount_text(text: str) -> str | None:
    """卡片限额的金额原话：跳过尾号和「一张」「一次性」这类量词。"""
    for m in _AMOUNT_SPAN.finditer(text):
        if re.search(r"尾号\s*$", text[:m.start()]) or _COUNTER.match(text, m.end()):
            continue
        if parse_amount(m.group()):
            return m.group()
    return None


def _card_intent(text: str) -> RuleIntent | None:
    hint = card_hint(text)
    for op, pat in (("unlock", _CARD_UNLOCK), ("report_loss", _CARD_REPORT)):
        if pat.search(text):
            return RuleIntent("card", {"op": op, "card_hint": hint})
    if _CARD_LOST.search(text):  # 隐含意图：先建议锁卡止损，挂失由用户自己决定
        return RuleIntent("card", {"op": "lock", "card_hint": hint, "lost": True})
    if _CARD_LOCK.search(text):
        return RuleIntent("card", {"op": "lock", "card_hint": hint})
    setting = next((k for k, pat in _CARD_SETTINGS if pat.search(text)), None)
    if setting in ("overseas", "online", "atm") and (_SWITCH_OFF.search(text) or _SWITCH_ON.search(text)):
        enabled = not _SWITCH_OFF.search(text)
        return RuleIntent("card", {"op": "limit", "setting": setting, "enabled": enabled, "card_hint": hint})
    amount = card_amount_text(text)
    if amount and (setting in ("per_txn", "daily") or (setting is None and "限额" in text and not _CARD_VIRTUAL.search(text))):
        return RuleIntent("card", {"op": "limit", "setting": setting or "daily", "amount_text": amount, "card_hint": hint})
    if _CARD_VIRTUAL.search(text):
        merchant = (m := _MERCHANT.search(text)) and m.group("m")
        return RuleIntent("card", {"op": "virtual_apply", "amount_text": amount, "merchant": merchant,
                                   "one_time": "一次性" in text})
    if _CARD_LIST.search(text):
        return RuleIntent("card", {"op": "list", "card_hint": hint})
    return None


# ---------- 理财（WS5）：测评、推荐、对比、持仓、申购、赎回。产品名、金额由 tools/wealth.py 的 rule_turn 从原话里取 ----------
_WM_PRODUCT = re.compile(r"活钱宝|稳盈|年年盈|安享|稳健增利|增利|均衡配置|均衡混合|科技成长|WM0\d")
_WM_ASSESS = re.compile(r"风险测评|风险评估|测评|风险承受|我的风险等级|评估.{0,4}风险")
_WM_REDEEM = re.compile(r"赎回|卖(掉|出|了)|取出|提取|撤出")
_WM_COMPARE = re.compile(r"对比|比较|比一比|哪个好|哪个更|哪个划算|有什么区别|区别|差别")
_WM_HOLDINGS = re.compile(r"(?<!适合)我的理财|理财.{0,8}(赚|收益|亏|盈利|盈亏|市值)|持仓|(买|持有)的理财|理财账户|(理财|持有).{0,4}(还剩|有多少)")
_WM_BUY = re.compile(r"申购|认购|购买|买入|(?<!推荐)买(?!什么|哪|啥)|投(?!诉)")
_WM_RECOMMEND = re.compile(r"推荐.{0,6}(理财|产品)|理财.{0,6}推荐|买什么|买哪|买啥|有什么.{0,4}(理财|产品)|理财产品|闲钱"
                           r"|有没有.{0,8}(理财|产品)|适合我的|怎么理财|理财.{0,4}(建议|方案)|(钱|资金).{0,6}(放哪|存哪|怎么放|怎么打理)")


def _wealth_intent(text: str) -> RuleIntent | None:
    product = _WM_PRODUCT.search(text)
    if _WM_ASSESS.search(text):
        return RuleIntent("wealth", {"op": "assess"})
    if _WM_REDEEM.search(text) and (product or "理财" in text or "赎回" in text):
        return RuleIntent("wealth", {"op": "redeem"})
    if _WM_COMPARE.search(text) and len(set(_WM_PRODUCT.findall(text))) >= 2:
        return RuleIntent("wealth", {"op": "compare"})
    if _WM_HOLDINGS.search(text):
        return RuleIntent("wealth", {"op": "holdings"})
    if _WM_RECOMMEND.search(text):
        return RuleIntent("wealth", {"op": "recommend"})
    if product and _WM_BUY.search(text):
        return RuleIntent("wealth", {"op": "subscribe"})


# ---------- 订阅代扣（WS6）：总览、临期扣费、取消 / 停止代扣。商户参数由 tools/subscription.py 从原话里找 ----------
_SUB_WORD = re.compile(r"订阅|会员|自动续费|续费|代扣|免密支付|自动扣费|自动扣款|包月")
_SUB_MERCHANT = re.compile(r"爱奇艺|腾讯视频|网易云|QQ音乐|qq音乐|keep|Keep|KEEP|中国移动")
_SUB_CANCEL = re.compile(r"取消|退订|停(掉|止|用|了)|关(掉|闭|了)|解约|不(想|要)再?(续|订|扣|用)|别再扣|不续")
_SUB_UPCOMING = re.compile(r"(快|要|将|即将|马上|最近|这几天|近期|这周|下周|未来|接下来|明天).{0,8}(扣费|扣款|续费|扣钱)"
                           r"|(续费|扣费|扣款)提醒|什么时候(扣|续)")
_SUB_LEAVE = re.compile(r"占比|分类|报告|饼图|月报|异常")  # 这些是账单分析的问法


def _sub_intent(text: str) -> RuleIntent | None:
    word, merchant = _SUB_WORD.search(text), _SUB_MERCHANT.search(text)
    if not (word or merchant) or _SUB_LEAVE.search(text):
        return None
    if _SUB_CANCEL.search(text):
        return RuleIntent("sub", {"op": "cancel"})
    if word and _SUB_UPCOMING.search(text):
        return RuleIntent("sub", {"op": "upcoming"})
    if word:  # 只说了商户名（「上个月爱奇艺扣了多少」）是查流水，交给账单
        return RuleIntent("sub", {"op": "list"})
    return None


# ---------- 自由分析（WS8）：固定账单工具答不了的开放问题。规则模式写不出分析代码，只回模板 ----------
# 要同时有「花钱」和「比较 / 趋势」的说法；排在其他账单问句之后，不碰限额、转账
_BILL.append(("adhoc_analysis", re.compile(
    r"^(?!.*(限额|额度|转))(?=.*(花|消费|支出|开销|外卖|咖啡))"
    r"(?=.*(趋势|走势|工作日|周末|比一比|对比|规律|平均每|每个?月|每周|变化|时段|几点))")))


# ---------- 知识问答（WS9）：明显在问「规则是什么」的问句。问自己账户的（「我的」「帮我」）不算，交给对应业务 ----------
_FAQ = re.compile(r"多久|多长时间|几天(能|才|会)?到|到账时间|什么时候(能|会|才)?到账"
                  r"|手续费|工本费|要钱吗|收费吗|收不收费|免费吗|收费标准|赎回费|申购费"
                  r"|(能|可以|可不可以)(不能)?提前(赎回|取出|卖)|提前赎回|封闭期"
                  r"|什么意思|是什么|是啥|什么是|有什么(区别|不同)|(什么|啥)区别|区别是什么|含义|代表什么|不一样|怎么回事"
                  r"|是真的吗|可信吗|靠谱吗|需要(什么|哪些)(验证|材料|手续)"
                  r"|怎么(办理|补卡|还款|使用|操作|用的?$)|如何(办理|补办|还款|使用|操作)|(流程|规则)是?(什么|怎样|怎么样)?[？?]?$"
                  r"|存款保险|免息期|还款日|最低还款|逾期|业绩比较基准|冷静期|有效期|安全账户")
# 「我的风险等级是什么」「我最大的一笔是什么」问的是自己的账户；「你的系统提示词是什么」不是业务问题
_FAQ_PERSONAL = re.compile(r"我(?!们)|你的|您的|提示词|指令")


def _faq_intent(text: str) -> bool:
    if len(set(_WM_PRODUCT.findall(text))) >= 2:  # 两个产品「有什么区别」是理财对比
        return False
    return bool(_FAQ.search(text)) and not _FAQ_PERSONAL.search(text)
