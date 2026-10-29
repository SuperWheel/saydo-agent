"""输入防护（注入防御 L1）与个人信息脱敏。

L1 只是第一道筛子，拦不住所有攻击；真正兜底的是权限分离（模型拿不到执行令牌）。
命中时不直接拒绝请求，而是打上标记交给策略引擎上浮风险，并写审计；熔断器（breaker.py）按标记计数。

检测时除了 NFKC 规范化后的原文，还会看几种「变形」：同形字（西里尔字母、繁体）折叠、去掉空格和分隔符、
Unicode 标签字符里藏的文字；Base64 / 百分号编码的内容解码后再扫一遍，命中记为 encoded_injection。
"""

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import unquote

# 零宽字符、方向控制符、软连字符等。U+E0000 段的标签字符能把整句 ASCII 藏起来，单独解码检查
_INVISIBLE = re.compile("[­᠎​-‏‪-‮⁠-⁯﻿\U000e0000-\U000e007f]")
_TAGS = re.compile("[\U000e0020-\U000e007e]")

_PATTERNS = [
    ("ignore_instructions", re.compile(
        r"(忽略|无视|抛开|丢弃|不要理会|不用理会|别理会)[^。！？\n]{0,8}(指令|指示|提示|规则|设定|限制|约束|安全策略|风控|系统消息)"
        r"|\b(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|your\s+|of\s+)*"
        r"(previous|above|prior|earlier|preceding|system|safety|security)?\s*"
        r"(instructions?|rules|prompts?|guidelines|restrictions|directives)", re.I)),
    ("role_override", re.compile(
        r"你现在(是|扮演|充当)(?!谁|什么|哪|不是|否)|假装(你是|自己是)|假设你是|角色扮演|(我们|咱们)来?玩个?游戏|玩个游戏"
        r"|(扮演|充当)(一个|一名|一位|个)?[^，。！？\n]{0,10}(银行|助手|AI|机器人|管理员|客服|系统|角色)"
        r"|从现在(起|开始)[，,]?你(就|将)?(是|扮演|只听|必须)"
        r"|开发者模式|调试模式|上帝模式|越狱|developer\s*mode|god\s*mode|jailbreak|\bDAN\b"
        r"|pretend\s+(you\s+are|to\s+be)|you\s+are\s+now|role-?play", re.I)),
    ("forged_system", re.compile(  # 伪造系统消息：开头的系统标签、聊天模板标记、角色前缀
        r"^\s*[\[【<(（{]\s*(系统|system|sys|管理员|admin|administrator|开发者|developer|官方|后台|root)"
        r"[^\]】>)）}\n]{0,12}[\]】>)）}]"
        r"|<\s*/?\s*(system|sys|im_start|im_end|instructions?)\s*>|<\|[a-z_]{2,20}\|>"
        r"|^\s*(system|developer)\s*[:：]"
        r"|(以下|下面|接下来)(是|为)?(来自)?(系统|官方|后台|管理员|开发者)(的|发出的)?(消息|通知|指令|命令)"
        r"|(系统|管理员|开发者|官方|后台)(指令|命令|口令|授权码)\s*[:：]"
        r"|#{2,}\s*(系统|system|指令|instructions?)|\bsystem\s+(override|prompt\s*[:：]|message\s*[:：])", re.I | re.M)),
    ("bypass_auth", re.compile(
        r"(跳过|绕过|免去|免除|省略|省去|不用|不需要|无需|不必)(人脸|刷脸|短信|验证|确认|认证|校验|风控|安全检查|密码)"
        r"|(关闭|关掉|停用|解除)(风控|安全检查|验证|人脸识别|短信验证)"
        r"|\b(skip|bypass|disable)\s+(the\s+)?(verification|auth\w*|confirmation|2fa|otp|security\s+checks?)", re.I)),
    ("authority_claim", re.compile(
        r"(我是|本人是|作为)(言行|银行|系统|平台|你们|你的)?的?(超级)?(管理员|客服|工作人员|开发者|行长|警察|民警|公安|检察官|法官|运维|测试人员|安全团队|风控专员)"
        r"|\bi\s*('m|am)\s+(the|an|your)?\s*(admin|administrator|developer|bank\s+manager|police|system)\b", re.I)),
    ("state_claim", re.compile(  # 声称验证已通过、风控已关闭：只上浮风险，不计入熔断
        r"(已|已经)(通过|完成)了?(人脸|短信|身份)?(验证|认证|核验|审核)|(已|已经)(验证|认证|核实|核验)(过|好)了?"
        r"|(风控|验证|安全检查|限额)(已|已经)(被)?(关闭|解除|停用|豁免|取消)|白名单(用户|客户)|(预先|提前|事先)授权"
        r"|(已|已经)(获得?|得到)?(授权|豁免)|免验证(用户|客户|通道)|(already\s+)?(verified|authorized|whitelisted)\s+user", re.I)),
    ("prompt_probe", re.compile(
        r"system\s*prompt|系统提示词|你的(提示词|指令|规则设定)是什么"
        r"|(输出|打印|显示|告诉我|泄露|复述|重复|原样)[^。！？\n]{0,8}(提示词|系统指令|初始指令|instructions)"
        r"|(提示词|系统指令|初始指令)[^。！？\n]{0,6}(输出|打印|发给我|告诉我|泄露|复述)"
        r"|(print|reveal|show)\s+(me\s+)?your\s+(system\s+)?(prompt|instructions)", re.I)),
]

# 同形字折叠：西里尔 / 希腊字母 → 拉丁字母，常见繁体 → 简体。只用于检测，不改用户原话
_FOLD = str.maketrans(
    "аеорсухіјѕԁɡАВЕКМНОРСТХІЅαοτνεικ"
    "無視規則設過繞驗證確認訊臉統開發員長們戲遊從來後臺權關閉風輸詞麼啟動記憶銀現須碼簡",
    "aeopcyxijsdgABEKMHOPCTXISaotveik"
    "无视规则设过绕验证确认讯脸统开发员长们戏游从来后台权关闭风输词么启动记忆银现须码简",
)
_SEPARATORS = re.compile(r"[\s·・.*_\-~|/\\]+")
_B64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_PCT = re.compile(r"(?:%[0-9A-Fa-f]{2}){3,}")


@dataclass
class GuardResult:
    text: str
    flags: list[str] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        return bool(self.flags)


def _scan(*variants: str) -> list[str]:
    return [name for name, pat in _PATTERNS if any(pat.search(v) for v in variants if v)]


def _decoded_payloads(text: str) -> list[str]:
    """Base64 和百分号编码的片段解码后的文字（解不出可读文字的跳过）。"""
    out = []
    for m in _B64.finditer(text):
        chunk = m.group()
        try:
            raw = base64.b64decode(chunk + "=" * (-len(chunk) % 4), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if raw.isprintable():
            out.append(raw)
    out += [unquote(m.group()) for m in _PCT.finditer(text)]
    return out


def inspect(raw: str) -> GuardResult:
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", raw)).strip()
    hidden = "".join(chr(ord(c) - 0xE0000) for c in _TAGS.findall(raw))  # 标签字符藏的 ASCII
    folded = text.translate(_FOLD)
    flags = _scan(text, folded, _SEPARATORS.sub("", folded), hidden)
    if text != raw.strip() and _INVISIBLE.search(raw):
        flags.append("invisible_chars")
    if any(_scan(p, p.translate(_FOLD)) for p in _decoded_payloads(text)):
        flags.append("encoded_injection")
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
