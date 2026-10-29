"""工具网关：模型只能调用只读工具和「提议」工具；写工具必须凭执行令牌，由确认流程调用。"""

from ..auth.tokens import TokenError, compute_digest, verify_execution_token
from ..core_mock.ledger import LedgerError
from ..flags import kill_switch_on
from ..models import PendingAction
from . import REGISTRY, ToolContext, ToolResult, ToolSpec

_SCALAR = (str, int, float, bool, type(None))


class GatewayError(Exception):
    pass


def run_read_tool(spec: ToolSpec, ctx: ToolContext, args: dict) -> ToolResult:
    if spec.side_effect:
        raise GatewayError("写工具只能通过确认流程执行")
    props = spec.parameters.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:  # 严格字段白名单，防止模型在参数里夹带 raw_account 之类的字段
        raise GatewayError(f"未知参数：{', '.join(sorted(unknown))}")
    missing = [k for k in spec.parameters.get("required", []) if k not in args]
    if missing:
        raise GatewayError(f"缺少参数：{', '.join(missing)}")
    if any(not isinstance(v, _SCALAR) for v in args.values()):
        raise GatewayError("参数只能是标量")
    return spec.fn(ctx, **args)


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
    if kill_switch_on(db):
        raise GatewayError("系统处于只读保护状态")
    action.token_jti = None  # 用过即作废
    try:
        return spec.fn(db, action.args, idempotency_key=action.id, user_id=action.user_id)
    except LedgerError as e:
        raise GatewayError(str(e)) from e
