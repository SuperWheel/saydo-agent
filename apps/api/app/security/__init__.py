"""输入防护（注入防御 L1）与个人信息脱敏。

L1 只是第一道筛子，拦不住所有攻击；真正兜底的是权限分离（模型拿不到执行令牌）。
命中时不直接拒绝请求，而是打上标记交给策略引擎上浮风险，并写审计。
"""

import re
import unicodedata
from dataclasses import dataclass, field

_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")
_PATTERNS = [
    ("ignore_instructions", re.compile(r"忽略.{0,6}(指令|提示|规则|设定)|ignore\s+(all\s+)?(previous|above|prior)\s+instructions", re.I)),
    ("role_override", re.compile(r"你现在(是|扮演)|假装你是|开发者模式|developer\s*mode|jailbreak|\bDAN\b", re.I)),
    ("bypass_auth", re.compile(r"(跳过|绕过|免去|省略|不用|不需要|无需)(人脸|短信|验证|确认|认证|校验)")),
    ("authority_claim", re.compile(r"我是(银行|系统)?的?(管理员|客服|工作人员|开发者|行长|警察)")),
    ("prompt_probe", re.compile(r"system\s*prompt|系统提示词|你的(提示词|指令)是什么", re.I)),
]


@dataclass
class GuardResult:
    text: str
    flags: list[str] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        return bool(self.flags)


def inspect(raw: str) -> GuardResult:
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", raw)).strip()
    flags = [name for name, pat in _PATTERNS if pat.search(text)]
    if text != raw.strip() and _INVISIBLE.search(raw):
        flags.append("invisible_chars")
    return GuardResult(text=text, flags=flags)


_LONG_DIGITS = re.compile(r"\d[\d\s]{6,}\d")
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")


def mask_pii(text: str) -> str:
    """写审计和发给模型之前脱敏：手机号保留前三后四，长数字串（卡号、账号）只留后四位。"""
    text = _PHONE.sub(r"\1****\2", text)

    def _mask(m: re.Match) -> str:
        digits = re.sub(r"\s", "", m.group())
        return "*" * (len(digits) - 4) + digits[-4:] if len(digits) >= 8 else m.group()

    return _LONG_DIGITS.sub(_mask, text)
