"""策略引擎：确定性代码决定绿 / 黄 / 红，模型无法影响定级。"""

from dataclasses import dataclass, field
from enum import IntEnum
from functools import lru_cache
from pathlib import Path

import yaml

from ..money import fmt_yuan

RULES_PATH = Path(__file__).with_name("rules.yaml")


class Risk(IntEnum):
    GREEN = 0
    YELLOW = 1
    RED = 2

    @property
    def label(self) -> str:
        return {"GREEN": "自动执行", "YELLOW": "需要确认", "RED": "需要强验证"}[self.name]


@dataclass
class PolicyContext:
    tool: str
    base_level: Risk
    amount_fen: int
    balance_fen: int
    outflow_today_fen: int  # 已成功 + 已预占，不含本次
    payee_known: bool
    recent_transfer_count: int = 0
    channel: str = "app"
    kill_switch: bool = False
    injection_flag: bool = False


@dataclass
class Decision:
    level: Risk
    reasons: list[str] = field(default_factory=list)
    rule_hits: list[str] = field(default_factory=list)
    required_factors: list[str] = field(default_factory=list)
    block: bool = False
    block_reason: str | None = None
    cumulative_after_fen: int = 0

    @classmethod
    def blocked(cls, reason: str, after: int = 0) -> "Decision":
        return cls(level=Risk.RED, block=True, block_reason=reason, reasons=[reason], cumulative_after_fen=after)

    def as_dict(self) -> dict:
        return {
            "level": self.level.name, "reasons": self.reasons, "rule_hits": self.rule_hits,
            "required_factors": self.required_factors, "block": self.block, "block_reason": self.block_reason,
            "cumulative_after_fen": self.cumulative_after_fen,
        }


@lru_cache
def load_rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


def _cumulative(rule: dict, ctx: PolicyContext, after: int) -> str | None:
    if after > rule["yellow_max_fen"]:
        return rule["reason"].format(after=fmt_yuan(after), limit=fmt_yuan(rule["yellow_max_fen"]))
    return None


def _new_payee(rule: dict, ctx: PolicyContext, after: int) -> str | None:
    return None if ctx.payee_known else rule["reason"]


def _velocity(rule: dict, ctx: PolicyContext, after: int) -> str | None:
    return rule["reason"] if ctx.recent_transfer_count + 1 >= rule["max_count"] else None


def _injection(rule: dict, ctx: PolicyContext, after: int) -> str | None:
    return rule["reason"] if ctx.injection_flag else None


MATCHERS = {
    "cumulative_outflow": _cumulative,
    "new_payee": _new_payee,
    "velocity": _velocity,
    "injection_suspected": _injection,
}


def evaluate(ctx: PolicyContext, rules: dict | None = None) -> Decision:
    rules = rules or load_rules()
    after = ctx.outflow_today_fen + ctx.amount_fen
    if ctx.kill_switch:
        return Decision.blocked("系统处于只读保护状态，暂不受理资金操作", after)
    if ctx.amount_fen <= 0:
        return Decision.blocked("金额必须大于 0", after)
    if ctx.amount_fen > ctx.balance_fen:
        return Decision.blocked(f"可用余额不足（当前 {fmt_yuan(ctx.balance_fen)} 元）", after)
    hard = rules["bank_hard_limits"]
    if after > hard["non_counter_daily_max_fen"]:
        return Decision.blocked(hard["reason"], after)

    level, reasons, hits = ctx.base_level, [], []
    for rule in rules["rules"]:
        if not rule.get("enabled", True):
            continue
        reason = MATCHERS[rule["id"]](rule, ctx, after)
        if reason:
            level = max(level, Risk[rule["elevate_to"]])  # 只升不降
            reasons.append(reason)
            hits.append(rule["id"])
    if not reasons and level == Risk.YELLOW:
        reasons.append(f"资金转出，今日累计 {fmt_yuan(after)} 元，需要你确认")
    return Decision(
        level=level, reasons=reasons, rule_hits=hits,
        required_factors=list(rules["auth"].get(level.name, [])), cumulative_after_fen=after,
    )
