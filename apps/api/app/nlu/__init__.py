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
    if asks_aa_status(text):
        return RuleIntent("aa_status")
    if slots := parse_aa(text):
        return RuleIntent("aa", slots)
    for pat in _TRANSFER:
        if m := pat.search(text):
            return RuleIntent("transfer", {"payee_hint": m.group("payee"), "amount_text": m.group("amount"), "memo": memo})
    if bill := next((name for name, pat in _BILL if pat.search(text)), None):  # 账单类问句排在「给我妈转了多少」之前
        return RuleIntent(bill)
    if m := _TRANSFER_NO_AMOUNT.search(text):
        return RuleIntent("transfer", {"payee_hint": m.group("payee"), "amount_text": "", "memo": memo})
    if _BALANCE.search(text):
        return RuleIntent("balance")
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
