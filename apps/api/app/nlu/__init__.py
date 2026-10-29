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
    for match in (
        lambda c: c.name == h,
        lambda c: h in c.aliases or bare in c.aliases,
        lambda c: c.note in (h, bare),
        lambda c: len(h) >= 2 and (h in c.name or c.name in h),
    ):
        hits = [c for c in contacts if match(c)]
        if hits:
            return sorted(hits, key=lambda c: (c.first_transfer_at is None, c.id))
    return []


@dataclass
class RuleIntent:
    intent: str  # balance / transfer / unknown
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
    for pat in _TRANSFER:
        if m := pat.search(text):
            return RuleIntent("transfer", {"payee_hint": m.group("payee"), "amount_text": m.group("amount"), "memo": memo})
    if m := _TRANSFER_NO_AMOUNT.search(text):
        return RuleIntent("transfer", {"payee_hint": m.group("payee"), "amount_text": "", "memo": memo})
    if _BALANCE.search(text):
        return RuleIntent("balance")
    if card := _card_intent(text):
        return card
    return RuleIntent("unknown")


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
