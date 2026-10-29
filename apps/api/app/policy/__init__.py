"""策略引擎：确定性代码决定绿 / 黄 / 红，模型无法影响定级。

上下文由各写工具的构造器提供（见 auth/actions.py 的 base_context）；规则用 tool_in 限定适用的工具，
用 when 匹配工具特有的事实（facts）。新场景加规则只改 rules.yaml。
"""

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
    moves_money: bool = False  # 资金转出类工具才做金额、余额、限额检查
    amount_fen: int = 0
    balance_fen: int = 0
    outflow_today_fen: int = 0  # 已成功 + 已预占，不含本次
    payee_known: bool = True
    recent_count: int = 0  # 同一工具在频率窗口内已发起的次数，不含本次
    channel: str = "app"
    kill_switch: bool = False
    stop_loss: bool = False  # 止损类操作（如锁卡）在只读保护下仍然放行
    injection_flag: bool = False
    block_reason: str | None = None  # 构造器发现的硬性问题，如收款人信息已变化
    facts: dict = field(default_factory=dict)  # 工具特有的事实，供规则的 when 条件匹配


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
    rules = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    validate_rules(rules)
    return rules


def validate_rules(rules: dict) -> None:
    for r in rules["rules"]:
        if "when" not in r and r.get("match", r["id"]) not in MATCHERS:
            raise ValueError(f"规则 {r['id']} 既没有 when 条件，也没有对应的匹配函数")
        Risk[r["elevate_to"]]


def _cumulative(rule: dict, ctx: PolicyContext, after: int) -> str | None:
    if after > rule["yellow_max_fen"]:
        return rule["reason"].format(after=fmt_yuan(after), limit=fmt_yuan(rule["yellow_max_fen"]))
    return None


def _new_payee(rule: dict, ctx: PolicyContext, after: int) -> str | None:
    return None if ctx.payee_known else rule["reason"]


def _velocity(rule: dict, ctx: PolicyContext, after: int) -> str | None:
    return rule["reason"] if ctx.recent_count + 1 >= rule["max_count"] else None


def _injection(rule: dict, ctx: PolicyContext, after: int) -> str | None:
    return rule["reason"] if ctx.injection_flag else None


def _when(rule: dict, ctx: PolicyContext, after: int) -> str | None:
    return rule["reason"] if all(ctx.facts.get(k) == v for k, v in rule["when"].items()) else None


MATCHERS = {
    "cumulative_outflow": _cumulative,
    "new_payee": _new_payee,
    "velocity": _velocity,
    "injection_suspected": _injection,
}


def _applies(rule: dict, tool: str) -> bool:
    return rule.get("enabled", True) and ("tool_in" not in rule or tool in rule["tool_in"])


def evaluate(ctx: PolicyContext, rules: dict | None = None) -> Decision:
    rules = rules or load_rules()
    after = ctx.outflow_today_fen + (ctx.amount_fen if ctx.moves_money else 0)
    if ctx.kill_switch and not ctx.stop_loss:
        return Decision.blocked("系统处于只读保护状态，暂不受理资金和账户变更操作", after)
    if ctx.block_reason:
        return Decision.blocked(ctx.block_reason, after)
    if ctx.moves_money:
        if ctx.amount_fen <= 0:
            return Decision.blocked("金额必须大于 0", after)
        if ctx.amount_fen > ctx.balance_fen:
            return Decision.blocked(f"可用余额不足（当前 {fmt_yuan(ctx.balance_fen)} 元）", after)
        hard = rules["bank_hard_limits"]
        if after > hard["non_counter_daily_max_fen"]:
            return Decision.blocked(hard["reason"], after)

    level, reasons, hits = ctx.base_level, [], []
    for rule in rules["rules"]:
        if not _applies(rule, ctx.tool):
            continue
        matcher = _when if "when" in rule else MATCHERS[rule.get("match", rule["id"])]
        reason = matcher(rule, ctx, after)
        if reason:
            level = max(level, Risk[rule["elevate_to"]])  # 只升不降
            reasons.append(reason)
            hits.append(rule["id"])
    if not reasons and level == Risk.YELLOW:
        reasons.append(f"资金转出，今日累计 {fmt_yuan(after)} 元，需要你确认" if ctx.moves_money else "需要你确认后才会执行")
    return Decision(
        level=level, reasons=reasons, rule_hits=hits,
        required_factors=list(rules["auth"].get(level.name, [])), cumulative_after_fen=after,
    )
