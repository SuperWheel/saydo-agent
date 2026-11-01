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
    contacts = list(db.scalars(select(Contact).where(Contact.user_id == user_id, Contact.status == "active")))
    bare = h.removeprefix("我的").removeprefix("我")
    tail = _tail_in(h)
    for match in (
        lambda c: c.phone == h,
        lambda c: c.name == h,
        lambda c: h in c.aliases or bare in c.aliases,
        lambda c: c.relation in (h, bare),
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


def pot_intent(text: str) -> RuleIntent | None:
    # 备注、引文等只作为数据；不能把里面的资金指令当作用户办理意图。
    text = re.split(r"备注|收款说明|引用|知识库|[「“\"]", text, maxsplit=1)[0].strip()
    if "心愿罐" not in text or re.search(r"不要|别|不能|怎么|如何|是否|能否", text):
        return None
    if re.search(r"心愿罐.*(明细|收支|记录|流水|花(?:到|在)?哪|用(?:到|在)?哪|去哪|进出)", text):
        return RuleIntent("pot", {"op": "detail"})
    if re.search(r"心愿罐.*(?:取出|转出|转回来|转回|取回)|从心愿罐.*取", text):
        m = re.search(rf"(?:取出|转出|转回来|转回|取回)\s*{_AMT}", text)
        return RuleIntent("pot", {"op": "withdraw", "amount_text": m.group("amount") if m else ""})
    # 单步存入（「往我的心愿罐里存2000」）：带金额的存钱话直接出存入确认卡，不能被「我的心愿罐」总览接走；
    # 同句还要订花、买东西、到期办的交给规划器
    if (re.search(r"(?:存|放|锁|攒|留|转|划|预留|锁定)[^，,。；;]{0,12}心愿罐|心愿罐[^，,。；;]{0,4}(?:存|放|锁|攒|留|转|划)", text)
            and (m := re.search(rf"(?:存入?|放|锁定?|攒|留|转入?|划|预留)\s*{_AMT}", text) or re.search(rf"{_AMT}(?:存|放|转)", text))
            and not re.search(r"订(?!阅)|[买送](?![^，,。]{0,4}礼物)|计划|提前|然后|到时|再(?!存)", text)):
        return RuleIntent("pot", {"op": "deposit", "amount_text": m.group("amount")})
    if re.search(r"我的心愿罐|管理心愿罐|心愿罐.{0,8}(?:余额|还有多少|还剩多少|多少|情况)"
                 r"|^(?:请|帮我)?(?:打开|查看|看看|看一下|查一下)?(?:我的)?心愿罐[？?。！!]*$", text):
        return RuleIntent("pot", {"op": "manage"})
    return None


def rule_intent(text: str) -> RuleIntent:
    """模型不可用时的兜底识别，只覆盖 M0 的两条链路。"""
    memo = (m := _MEMO.search(text)) and m.group("memo")
    from .transfer import asks_aa_status, parse_aa, phone_transfer_slots  # 转账补全：手机号、AA

    if pot := pot_intent(text):
        return pot
    if slots := phone_transfer_slots(text):  # 槽位里是脱敏号码，提议工具再从原话还原
        return RuleIntent("transfer", {**slots, "memo": memo})
    if _faq_intent(text) or asks_rule(text):  # 知识问答（WS9）排在 AA、理财、订阅、卡片之前：「AA 收款怎么操作」「信用卡挂失要钱吗」问的是规则，不是办理
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
    if re.search(r"心愿罐.*(明细|收支|记录|流水|花(?:到|在)?哪|用(?:到|在)?哪|去哪|进出)", text):
        return RuleIntent("pot", {"op": "detail"})
    if ("联系人" in text or re.search(r"(添加|新增|删除|删掉|修改).{0,12}收款人|^(?:请|帮我)?(?:删除|删掉)(?!.*(?:卡|订单|订阅|计划|心愿罐)).{2,10}$", text)):
        return RuleIntent("contact", {"op": "manage"})
    if mandate := mandate_intent(text):
        return mandate
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
# 「支出总览」「这个月的消费总结」：消费前面只能是时间或「我的」，「外卖支出分析」这种说了具体对象的交给自由分析
_SPENDING = r"(?:^|(?<=[的我月年天周近]))(?:消费|支出|开销|花销)"
_WHEN = r"(这个?月|上个?月|本月|今年|去年|最近|近期|\d{1,2}月份?|[一二三四五六七八九十]{1,2}月份?)"
_BILL = [
    ("bill_report", re.compile(r"报告|月报|年报|年度账单|账单总结|收支(情况|总结|统计|汇总|分析)|统计(一下)?.{0,8}收支|总结一下.{0,6}(账单|消费|收支)"
                               r"|(?:\d{4}年|去年|今年|本年)(?:全年|一年|以来)?的?每个?月的?(?:收入|支出|收支|消费)"
                               r"|^(?:请|帮我|给我|我|看看|看一下|查一下)*(?:\d{4}年|去年|今年|本年)(?:全年|一年|以来)?的?"
                               r"(?:收支|收入(?:和|与|及|、)?支出|花了?多少(?:钱)?)[？?。！!]*$"
                               rf"|{_SPENDING}的?(总结|分析)|分析(一下)?(我的?)?{_WHEN}?的?(消费|支出|开销|花销|收支|账单)")),
    ("bill_anomalies", re.compile(r"异常|可疑|盗刷|被刷|不是我(本人)?(花|消费|买)|重复扣|扣了两次|多扣")),
    ("bill_categories", re.compile(r"分类|类别|占比|饼图|花(在|到)哪|都花(在)?哪|钱去哪|消费结构|各类|哪类"
                                   rf"|{_SPENDING}的?(总览|概况|概览|构成|统计|汇总|分布|排行)")),
    ("bill_query", re.compile(r"(花|消费|支出|转|付|收|赚|进账|入账)了?多少|(工资|收入|支出|消费)(是|有|发了)?多少"
                              r"|(最近|上|最后|最大|最贵)的?一笔|流水|账单|明细"
                              r"|(转账|交易|消费|支付|支出)(记录|明细|情况|详情|列表|清单)|(最近|近期|这几天|这阵子)的?(支出|消费|开销|花销|交易)")),
]
# ---------- 卡片（WS3）：锁卡、解锁、挂失、「卡丢了」、交易限制、虚拟卡、查卡 ----------
_CARD_HINT = re.compile(r"(?:尾号|尾数|(?:卡号)?(?:末|后)\s*[四4]\s*位)\s*(?:是|为)?\s*\d{4}"
                        r"|\d{4}(?=\s*(?:的|那张)?(?:卡|借记卡|储蓄卡|信用卡|虚拟卡))|借记卡|储蓄卡|工资卡|信用卡|虚拟卡")
# 锁卡、解锁、挂失、查卡的话里没有金额，单独的 4 位数就是尾号：「把6688锁了」「锁卡6688」「冻结6688那张」
_BARE_TAIL = re.compile(r"(?<![\d.])\d{4}(?![\d.]|\s*(?:元|块|万|千|百|年|月|号|日|点|分|天|次|笔|%))")
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
_CARD_SETTINGS_ANY = re.compile(r"限额|额度|限制|境外|海外|线上|网上|无卡|ATM|取现|虚拟卡|单笔|单日|每天|每日")
# 「我的银行卡」「几张信用卡」：卡前面可以带卡种；「查看我的银行卡」「银行卡列表」也是查卡
_CARD_KIND_WORD = r"(?:银行|储蓄|借记|信用)?"
_CARD_LIST = re.compile(rf"我的{_CARD_KIND_WORD}卡|(?:几|多少)张{_CARD_KIND_WORD}卡|哪些{_CARD_KIND_WORD}卡|查.{{0,3}}卡"
                        r"|(?:看|查看|查询|列出|显示).{0,4}(?:银行|储蓄)卡|银行卡(?:列表|信息|情况|状态)?$"
                        r"|卡.{0,4}(状态|情况|限额|额度)|限额|额度")
# 解绑：「解绑尾号1554的卡」「把这张卡解绑」「删除银行卡」。排在绑卡前面：「解除绑定」「取消绑定」里也有「绑定」
_CARD_UNBIND = re.compile(r"解绑|解除.{0,2}绑定|取消.{0,2}绑定|(?:删除|删掉|移除|去掉).{0,10}卡")
# 绑卡：「我要绑一张新卡」「添加银行卡」「绑定尾号1234的卡」。「解绑」不是绑卡，申请虚拟卡另有流程
_CARD_BIND = re.compile(r"(?<!解)绑(?:定)?.{0,8}卡|(?:添加|新增|加一张|增加|关联|绑上).{0,6}(?:银行|储蓄|借记|信用|新)?卡")
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


def bare_tails(text: str) -> list[str]:
    """话里单独出现的 4 位数，只在锁卡、解锁、挂失、查卡这类不带金额的话里当尾号用。"""
    return _BARE_TAIL.findall(text)


def _card_intent(text: str) -> RuleIntent | None:
    hint = card_hint(text)
    if not re.search(r"\d{4}", hint) and not _CARD_SETTINGS_ANY.search(text):
        hint += "".join(f"尾号{t}" for t in bare_tails(text))
    if _CARD_UNBIND.search(text):
        return RuleIntent("card", {"op": "unbind", "card_hint": hint})
    if _CARD_BIND.search(text) and not _CARD_VIRTUAL.search(text) and "虚拟卡" not in text:
        tail = (m := re.search(r"\d{4}", hint)) and m.group()
        return RuleIntent("card", {"op": "bind", "tail": tail})
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


_ORDINAL = re.compile(r"第\s*([一二三四五1-5])\s*(?:个|款|只|支|种|项)?|(?:头|首)一?(?:个|款)|最后一?(?:个|款)")
_ORDINAL_N = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4}


def ordinal_index(text: str) -> int | None:
    """「第一个」「第2款」「最后一个」指的是刚才推荐的第几款：0 起，最后一个是 -1。没说序号返回 None。"""
    if not (m := _ORDINAL.search(text or "")):
        return None
    if m.group(1):
        return _ORDINAL_N.get(m.group(1), int(m.group(1)) - 1 if m.group(1).isdigit() else 0)
    return -1 if m.group().startswith("最后") else 0


def strip_ordinal(text: str) -> str:
    """找金额前去掉序号：「买第一个，1万」里的「一」不是金额。"""
    return _ORDINAL.sub(" ", text)


def _wealth_intent(text: str) -> RuleIntent | None:
    product = _WM_PRODUCT.search(text)
    if ordinal_index(text) is not None and _WM_BUY.search(text) and not _WM_REDEEM.search(text):
        return RuleIntent("wealth", {"op": "subscribe"})  # 推荐之后「买第一个」「第二款买1万」
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


def names_merchant(text: str) -> bool:
    """话里点名了是哪个订阅商户（「取消爱奇艺」），而不是「刚才那个」。"""
    return bool(_SUB_MERCHANT.search(text))


def mandate_intent(text: str) -> RuleIntent | None:
    if names_merchant(text) or re.search(r"备注|知识库|引用|原文|收款说明|[「“\"]", text):
        return None
    if re.search(r"怎么|如何|怎样|(?:不要|暂不|不想|拒绝|别|不)(?:再|帮我|给我)?(?:开通|开启|设置|修改|调整)", text):
        return None
    if re.search(r"(我的|查看|查询|看看|管理).{0,4}支付授权", text):
        return RuleIntent("mandate", {"op": "overview"})
    if "免密" not in text:
        return None
    if re.search(r"关闭|关掉|停用|取消|撤销", text):
        return RuleIntent("mandate", {"op": "revoke"})
    if re.search(r"开通|开启|设置|修改|调整|调高|调低", text):
        return RuleIntent("mandate", {"op": "form"})
    return None


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
# 说了时间范围、要看「花了哪些钱」「买了什么」「近30天的消费」这类明细：排在自由分析之后，兜住上面没认出来的明细问句。
# 必须带时间说法，「开通境外消费」「取消消费提醒」这类卡片、订阅设置不算
_BILL.append(("bill_query", re.compile(
    r"^(?!.*(限额|额度|开通|关闭|打开|关掉|取消|退订|提醒))(?=.*(最近|近期|过去|这|前|天|周|星期|礼拜|月|年))"
    r".*((花了|买了)(哪些|什么|啥)|(消费|支出|开销|花销|花费)(情况|详情|列表|清单)?[吗呢呀啊吧？?。！!]*$)")))


# ---------- 知识问答（WS9）：明显在问「规则是什么」的问句。问自己账户的（「我的」「帮我」）不算，交给对应业务 ----------
_FAQ = re.compile(r"多久|多长时间|几天(能|才|会)?到|到账时间|什么时候(能|会|才)?到账"
                  r"|手续费|工本费|要钱吗|收费吗|收不收费|免费吗|收费标准|赎回费|申购费"
                  r"|(能|可以|可不可以)(不能)?提前(赎回|取出|卖)|提前赎回|封闭期"
                  r"|什么意思|是什么|是啥|什么是|有什么(区别|不同)|(什么|啥)区别|区别是什么|含义|代表什么|不一样|怎么回事"
                  r"|是真的吗|可信吗|靠谱吗|需要(什么|哪些)(验证|材料|手续)"
                  r"|怎么(办理|补卡|还款|使用|操作|用的?$)|如何(办理|补办|还款|使用|操作)|(流程|规则)是?(什么|怎样|怎么样)?[？?]?$"
                  r"|存款保险|免息期|还款日|最低还款|逾期|业绩比较基准|冷静期|有效期|安全账户"
                  # 监管 / 法规类问法（kb-2026-10b）：「卡被盗刷银行赔吗」「七天无理由退货的规定」「被骗转账了怎么办」
                  r"|赔吗|赔不赔|(谁|银行)(来)?(赔|承担|负责)|合法吗|违法吗|(有什么|法律|的)规定|无理由退货|七天无理由|7 ?天无理由"
                  r"|(被骗|受骗|上当|诈骗).{0,8}(怎么办|追回|要回)|(去|向|找)?(哪里|哪儿|谁|哪个部门)投诉|怎么投诉|退汇|不当得利|适当性|双录")
# 「我的风险等级是什么」「我最大的一笔是什么」问的是自己的账户；「你的系统提示词是什么」不是业务问题
_FAQ_PERSONAL = re.compile(r"我(?!们)|你的|您的|提示词|指令")


def _faq_intent(text: str) -> bool:
    if len(set(_WM_PRODUCT.findall(text))) >= 2:  # 两个产品「有什么区别」是理财对比
        return False
    return bool(_FAQ.search(text)) and not _FAQ_PERSONAL.search(text)


# ---------- 规则 / 条件类问句：「赎回理财的时候需要刷脸吗」「支付宝里的自动扣款服务怎么停」「把一张卡解除绑定会有什么影响」 ----------
# 话里有「赎回」「自动扣款」「解绑」，上面的业务正则会把它们当成办理接走。问法是在问规则或条件、话里没有金额、
# 手机号、尾号这类办理槽位、知识库也确实查得到时，按知识问答处理。「给我妈转500行吗」带金额，仍是办理；
# 「帮我…」「我要…」是请求办理；「我的理财怎么样」「我还有多少钱」问的是自己的账户
_RULE_Q = re.compile(
    r"(需要|需不需要|要不要|用不用|必须|得)[^，,。]{0,8}(刷脸|人脸|验证|短信|验证码|确认|材料|手续|密码|开通|本人)"
    r"|(能|可以|会|算|可|允许|还会|还能)[^，,。]{0,24}(吗|么)[？?]?$"
    r"|能不能|可不可以|会不会|算不算|是不是|行不行|有没有(上限|限制|规定|影响|风险|用)"
    r"|怎么|如何|怎样|为什么|为啥|凭什么"
    r"|(会|有)(什么|啥|哪些)(影响|后果|风险|区别|不同|权利|用处)|会怎样|会怎么样"
    r"|(在|去)?哪(里|儿)?(关|取消|停|办|退|投诉|开通|查看)"
    r"|(哪些|什么)(材料|权利|证明|功能|条件|规定|情况)|要做什么|几个等级|分几"
    r"|(扣|收|要)多少钱|起购|起投|(风险等级|费率)是?(多少|什么|几)|是真的吗|可信吗|靠谱吗|合理吗|合法吗")
# 「这个月钱怎么花这么快」是在看自己的账单，不是问规则
_RULE_Q_NOT = re.compile(r"^(帮我|请|麻烦|给我)|我(想|要)(?!问|知道|了解|咨询)|怎么(花|用)了?(这么|那么)|怎么样了?[？?]?$"
                         r"|我.{0,12}(多少|几张|几笔|哪些|是什么|是啥)"
                         r"|怎么理财|(钱|资金).{0,6}(放哪|存哪|怎么放|怎么打理)")
# 金额：带单位的数（「500元」「两千」「1万」），或者不接时间、等级、量词的裸数字（「转500」「尾号6688」）
_SLOT_MONEY = re.compile(r"[¥￥]\s*\d|\d+(?:\.\d+)?\s*(?:元|块|万|千|w|k)|[零一二两三四五六七八九十百]+(?:元|块|万|千)"
                         r"|(?<![\dA-Za-z.])\d{3,}(?![\d.]|\s*(?:天|个?月|年|日|号|级|位|点|%|次|笔|张|个|小时|分钟|秒))")


def has_action_slots(text: str) -> bool:
    """话里有没有办理用的槽位：金额、尾号、手机号。只有收款人没有金额不算（「能给家里定时转钱吗」）。"""
    from .transfer import phones_in

    return bool(_SLOT_MONEY.search(text) or phones_in(text))


def asks_rule(text: str) -> bool:
    """规则 / 条件类问句，且知识库查得到：两种模式都先走知识库，不让业务工作流接走。"""
    from ..knowledge import search

    if not _RULE_Q.search(text) or _RULE_Q_NOT.search(text) or has_action_slots(text) or _FAQ_SELF.search(text):
        return False
    if _CARD_LOST.search(text) and not re.search(r"材料|证明|赔|承担|责任|规定", text):
        return False  # 「卡丢了怎么办」先建议锁卡止损（隐含意图）；「证明盗刷要哪些材料」才是问规则
    if _SUB_MERCHANT.search(text) and _SUB_CANCEL.search(text) and not re.search(r"[吗么][？?]?$", text):
        return False  # 「怎么取消腾讯视频的自动续费」：用户订了的商户走取消流程，卡片上的商户指引同样来自知识库
    if len(set(_WM_PRODUCT.findall(text))) >= 2:  # 两个产品比较是理财对比
        return False
    return bool(search(text).hits)


_FAQ_SELF = re.compile(r"你的|您的|提示词|指令")
