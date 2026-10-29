"""转账领域的确定性解析：手机号（完整或脱敏形式）、AA 拆分的总额和参与人。全部是规则，不交给模型。"""

import re

from . import _AMOUNT_CHARS

# ---------- 手机号 ----------

_PHONE = re.compile(r"(?<![\d*])(1[3-9]\d)[\s-]?(\d{4})[\s-]?(\d{4})(?!\d)")
_MASKED_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)?\*{4,8}(\d{4})(?!\d)")  # 138****5678；带空格的号码会被脱敏成 *******5678
_TRANSFER_VERB = re.compile(r"转|打钱|打款|汇")
_AMOUNT_AFTER_VERB = re.compile(
    rf"(?:转账|转|打|汇)(?:钱|款)?(?:给|到)?\s*(?P<amount>[¥￥]?[{_AMOUNT_CHARS}]+(?:块钱|块|元)?)")


def phones_in(text: str) -> list[str]:
    """用户原话里的完整手机号（去掉空格和连字符），按出现顺序去重。"""
    return list(dict.fromkeys("".join(m.groups()) for m in _PHONE.finditer(text)))


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}"


def is_phone_hint(hint: str) -> bool:
    return bool(_PHONE.search(hint) or _MASKED_PHONE.search(hint))


def resolve_phone(hint: str, user_texts: list[str]) -> list[str]:
    """把模型传来的手机号（完整或脱敏形式）还原成用户原话里的完整号码。

    模型只看得到脱敏后的号码；完整号码只能从用户原话里取，模型编造的号码找不到出处，返回空列表。
    脱敏形式对上多个号码时全部返回，由上层追问。
    """
    said = list(dict.fromkeys(p for t in user_texts for p in phones_in(t)))
    if full := phones_in(hint):
        return [full[0]] if full[0] in said else []
    if m := _MASKED_PHONE.search(hint):
        head, tail = m.groups()
        return [p for p in said if p.endswith(tail) and (head is None or p.startswith(head))]
    return []


def phone_transfer_slots(text: str) -> dict | None:
    """「给 13912345678 转 200」→ {payee_hint: 139****5678, amount_text: 200}；不是按手机号转账时返回 None。

    槽位里只放脱敏号码（会写进审计），提议工具再从用户原话里还原。
    """
    phones = phones_in(text)
    if len(phones) != 1 or not _TRANSFER_VERB.search(text):
        return None
    m = _AMOUNT_AFTER_VERB.search(_PHONE.sub(" ", text))
    return {"payee_hint": mask_phone(phones[0]), "amount_text": m.group("amount") if m else ""}


# ---------- AA 拆分 ----------

_AA_WORD = re.compile(r"AA|aa|平摊|均摊|分摊|平分")
_AA_STATUS = re.compile(r"(?:AA|aa|平摊|分摊).{0,8}(?:进度|状态|怎么样|如何|情况|收齐|收到|付了|付清|谁)|谁(?:还)?没(?:付|给|转)")
_CLAUSE = re.compile(r"[，,。！!？?；;\s]+")
_NAME_SEP = re.compile(r"[、和跟与及/]")
_COUNT = re.compile(r"(?:我们)?[\d两二三四五六七八九十]+个?人|几个人|一起|大家|平均|一下")
_MONEY = re.compile(rf"[¥￥]?\d+(?:\.\d+)?(?:块钱|块|元)?|[¥￥]?[{_AMOUNT_CHARS}]+(?:块钱|块|元)")
_LEADING_TIME = re.compile(r"^(?:昨晚|昨天|前天|今晚|今天|刚才|刚刚|中午|晚上|上周|周末)+")
_TRAILING_FILLER = re.compile(r"(?:花了|一共|总共|共|是)+$")
_NOT_ME = re.compile(r"我不(?:参与|参加|算|出)|不算我|除了我")
_ABSORB = re.compile(r"尾差(?:我|由我|算我)|我(?:来)?(?:承担|出|付|补)(?:尾差|零头)|零头(?:我|算我|由我)|多的(?:我|算我)")
ME_WORDS = frozenset({"我", "我自己", "自己", "本人", "我们"})


def split_names(text: str) -> list[str]:
    """「小王、小李和小张」→ [小王, 小李, 小张]；保留「我」，由调用方判断用户本人是否参与。"""
    return [t.strip() for t in _NAME_SEP.split(text) if t.strip()]


def asks_aa_status(text: str) -> bool:
    return bool(_AA_STATUS.search(text))


def parse_aa(text: str) -> dict | None:
    """「昨晚火锅 856，我和小王、小李、小张 AA」→ aa.propose 的参数；没提 AA 时返回 None。"""
    clauses = [c for c in _CLAUSE.split(text) if c]
    idx = next((i for i, c in enumerate(clauses) if _AA_WORD.search(c)), None)
    if idx is None:
        return None
    who = _AA_WORD.split(clauses[idx])[0]
    if not _NAME_SEP.search(who) and idx > 0:  # 「我和小王、小李，AA 一下」：名单在前一个分句
        who = clauses[idx - 1]
    who = _COUNT.sub("", _MONEY.sub("", who))
    names = [n for n in split_names(who) if n not in ME_WORDS]

    total, title = "", None
    for m in _MONEY.finditer(text):
        if text[m.end():m.end() + 2].startswith(("个", "人", "位")) or phones_in(m.group()):
            continue
        total = m.group()
        before = _CLAUSE.split(text[:m.start()].rstrip())[-1]
        before = _TRAILING_FILLER.sub("", _LEADING_TIME.sub("", before))
        if before and not _AA_WORD.search(before) and not _NAME_SEP.search(before) and len(before) <= 8:
            title = before
        break
    return {"total_text": total, "participants": "、".join(names), "include_me": not _NOT_ME.search(text),
            "me_absorb_remainder": bool(_ABSORB.search(text)), "title": title}
