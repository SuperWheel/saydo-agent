"""工具注册表。风险元数据跟着工具定义走，不依赖提示词。

新场景的写操作：注册一个 side_effect=True 的执行工具（带 policy_context 构造器），
再注册一个模型可见的提议工具，在里面调用 auth.actions.propose_action 生成确认卡。
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..models import ChatSession
from ..policy import Risk


# 业务域在帮助语里的叫法，也是帮助语的先后顺序。新业务域在这里加一行；没加的用该域第一个工具的 capability，排在最后
DOMAINS = {"account": "查余额", "transfer": "转账和 AA 收款", "bill": "分析账单", "card": "管理银行卡",
           "wealth": "推荐和买卖理财", "subscription": "管理订阅扣费", "automation": "订花订蛋糕、存心愿罐和定时转账",
           "knowledge": "解答业务规则和产品问题"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    domain: str
    base_risk: Risk
    side_effect: bool
    llm_visible: bool
    description: str
    parameters: dict
    fn: Callable
    moves_money: bool = False  # 资金转出：做余额 / 限额检查，生成确认卡时预占额度，args 里必须有 amount_fen
    stop_loss: bool = False  # 止损类操作（锁卡、挂失），只读保护下仍可执行
    stop_loss_if: Callable[[dict], bool] | None = None  # 按参数判断的止损（如收紧限额）：返回 True 时同 stop_loss
    policy_context: Callable | None = None  # (db, args, env) -> PolicyContext；不填用 base_context
    capability: str = ""  # 给用户看的能力短语，如「查余额」，用于提示词和帮助语
    examples: tuple[str, ...] = ()  # 示范说法，如「我还有多少钱」

    def is_stop_loss(self, args: dict | None) -> bool:
        """这次调用算不算止损。不知道参数时只认工具级的 stop_loss。"""
        return self.stop_loss or (self.stop_loss_if is not None and args is not None and self.stop_loss_if(args))

    @property
    def llm_name(self) -> str:  # OpenAI 函数名不允许出现点号
        return self.name.replace(".", "_")


@dataclass
class ToolContext:
    db: Session
    user_id: int
    session: ChatSession
    trace_id: str
    user_texts: list[str]  # 最近几轮用户原话，用于参数来源校验
    channel: str = "app"
    injection_flag: bool = False
    cards: list[dict] = field(default_factory=list)


@dataclass
class ToolResult:
    data: dict  # 交给模型看的内容（已脱敏）
    card: dict | None = None  # 直接给前端渲染，不经过模型


REGISTRY: dict[str, ToolSpec] = {}


def bank_tool(*, name: str, domain: str, base_risk: Risk, side_effect: bool = False, llm_visible: bool = True,
              description: str = "", parameters: dict | None = None, moves_money: bool = False, stop_loss: bool = False,
              stop_loss_if: Callable[[dict], bool] | None = None, policy_context: Callable | None = None,
              capability: str = "", examples: tuple[str, ...] = ()):
    def deco(fn: Callable) -> Callable:
        REGISTRY[name] = ToolSpec(
            name=name, domain=domain, base_risk=base_risk, side_effect=side_effect, llm_visible=llm_visible,
            description=description, parameters=parameters or {"type": "object", "properties": {}}, fn=fn,
            moves_money=moves_money, stop_loss=stop_loss, stop_loss_if=stop_loss_if, policy_context=policy_context,
            capability=capability, examples=examples,
        )
        return fn

    return deco


def model_tools() -> list[ToolSpec]:
    """模型能调用的工具：只读工具和提议工具，写工具永远不暴露给模型。"""
    return [s for s in REGISTRY.values() if s.llm_visible and not s.side_effect]


def llm_tool_specs(domains: set[str] | None = None, extra: tuple[str, ...] = ()) -> list[dict]:
    """发给模型的工具清单。domains 为 None 时发全部；否则只发这些业务域的工具，外加 extra 里点名的工具。"""
    return [
        {"type": "function", "function": {"name": s.llm_name, "description": s.description, "parameters": s.parameters}}
        for s in model_tools() if domains is None or s.domain in domains or s.name in extra
    ]


def by_llm_name(llm_name: str) -> ToolSpec | None:
    return next((s for s in REGISTRY.values() if s.llm_name == llm_name), None)
