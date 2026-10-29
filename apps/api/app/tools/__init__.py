"""工具注册表。风险元数据跟着工具定义走，不依赖提示词。"""

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..models import ChatSession
from ..policy import Risk


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
              description: str = "", parameters: dict | None = None):
    def deco(fn: Callable) -> Callable:
        REGISTRY[name] = ToolSpec(
            name=name, domain=domain, base_risk=base_risk, side_effect=side_effect, llm_visible=llm_visible,
            description=description, parameters=parameters or {"type": "object", "properties": {}}, fn=fn,
        )
        return fn

    return deco


def llm_tool_specs() -> list[dict]:
    return [
        {"type": "function", "function": {"name": s.llm_name, "description": s.description, "parameters": s.parameters}}
        for s in REGISTRY.values()
        if s.llm_visible and not s.side_effect
    ]


def by_llm_name(llm_name: str) -> ToolSpec | None:
    return next((s for s in REGISTRY.values() if s.llm_name == llm_name), None)
