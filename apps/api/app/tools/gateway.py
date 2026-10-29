"""工具网关：模型只能调用只读工具和「提议」工具；写工具必须凭执行令牌，由确认流程调用。"""

from ..auth.tokens import TokenError, compute_digest, verify_execution_token
from ..core_mock.ledger import LedgerError
from ..flags import kill_switch_on
from ..models import PendingAction
from ..security import breaker
from . import REGISTRY, ToolContext, ToolResult, ToolSpec

_SCALAR = (str, int, float, bool, type(None))


class GatewayError(Exception):
    pass


class ToolViolation(GatewayError):
    """模型越权：调用写工具、夹带白名单外的参数、传非标量参数。熔断器按会话计数。"""


def run_read_tool(spec: ToolSpec, ctx: ToolContext, args: dict) -> ToolResult:
    if spec.side_effect:
        raise ToolViolation("写工具只能通过确认流程执行")
    props = spec.parameters.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:  # 严格字段白名单，防止模型在参数里夹带 raw_account 之类的字段
        raise ToolViolation(f"未知参数：{', '.join(sorted(unknown))}")
    missing = [k for k in spec.parameters.get("required", []) if k not in args]
    if missing:
        raise GatewayError(f"缺少参数：{', '.join(missing)}")
    if any(not isinstance(v, _SCALAR) for v in args.values()):
        raise ToolViolation("参数只能是标量")
    if (block := breaker.circuit_block(ctx.db, spec)) is not None:  # 断路器：下游异常过多时先不调
        raise GatewayError(block.text)
    try:
        with ctx.db.begin_nested():  # 工具内部出错时回滚它写了一半的东西
            result = spec.fn(ctx, **args)
    except GatewayError:
        raise
    except Exception as e:
        breaker.tool_failed(ctx.db, spec, e, trace_id=ctx.trace_id)
        raise GatewayError("服务暂时不可用，请稍后再试") from e
    breaker.tool_ok(ctx.db, spec)
    return result


def execute_write(action: PendingAction, token: str, db) -> dict:
    """逐项核对：令牌签名与有效期、令牌绑定的动作和 digest、参数未被篡改、熔断开关、一次性使用。"""
    spec = REGISTRY.get(action.tool)
    if spec is None or not spec.side_effect:
        raise GatewayError("不是可执行的写工具")
    try:
        claims = verify_execution_token(token)
    except TokenError as e:
        raise GatewayError(str(e)) from e
    if claims["action_id"] != action.id or claims["digest"] != action.digest or claims["jti"] != action.token_jti:
        raise GatewayError("令牌与动作不匹配")
    if compute_digest(action.tool, action.args, action.user_id, action.nonce) != action.digest:
        raise GatewayError("动作参数与确认内容不一致")
    if kill_switch_on(db) and not spec.stop_loss:
        raise GatewayError("系统处于只读保护状态")
    if (block := breaker.check(db, user_id=action.user_id, tool=action.tool, session_id=action.session_id)) is not None:
        raise GatewayError(block.text.rstrip("。"))  # 熔断状态在策略引擎和网关两处都检查
    action.token_jti = None  # 用过即作废
    try:
        with db.begin_nested():  # 下游异常时回滚本次写入，不留半笔账
            result = spec.fn(db, action.args, idempotency_key=action.id, user_id=action.user_id)
    except LedgerError as e:
        raise GatewayError(str(e)) from e
    except Exception as e:
        breaker.tool_failed(db, spec, e, trace_id=action.trace_id)
        raise GatewayError("核心系统暂时不可用，本次没有执行") from e
    breaker.tool_ok(db, spec)
    return result
