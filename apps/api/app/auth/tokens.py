"""执行令牌与短信验证码（模拟）。

令牌只在用户确认 / 认证通过后由服务端签发，直接交给工具网关，模型和前端都拿不到。
"""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime

from ..audit import canonical
from ..clock import now
from ..config import get_settings


class TokenError(Exception):
    pass


def compute_digest(tool: str, args: dict, user_id: int, nonce: str) -> str:
    """digest = SHA256(规范化 JSON(tool, args, user_id, nonce))。确认卡、验证码、执行令牌都绑定它。"""
    return hashlib.sha256(canonical({"tool": tool, "args": args, "user_id": user_id, "nonce": nonce}).encode()).hexdigest()


def _sign(data: bytes) -> str:
    key = get_settings().app_secret.encode()
    return base64.urlsafe_b64encode(hmac.new(key, data, hashlib.sha256).digest()).decode().rstrip("=")


def issue_execution_token(action_id: str, digest: str) -> tuple[str, str]:
    """返回 (token, jti)。令牌绑定 action_id + digest，60 秒内有效，只能用一次。"""
    jti = secrets.token_hex(8)
    claims = {"action_id": action_id, "digest": digest, "exp": int(now().timestamp()) + get_settings().token_ttl_s, "jti": jti}
    body = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{body}.{_sign(body.encode())}", jti


def verify_execution_token(token: str) -> dict:
    try:
        body, sig = token.split(".")
    except ValueError as e:
        raise TokenError("令牌格式错误") from e
    if not hmac.compare_digest(sig, _sign(body.encode())):
        raise TokenError("令牌签名无效")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if claims["exp"] < int(now().timestamp()):
        raise TokenError("令牌已过期")
    return claims


def otp_for(digest: str, issued_at: datetime) -> str:
    """OTP = Truncate(HMAC(secret, digest ‖ 签发时间))。交易参数一改，digest 变，验证码随即失效。"""
    key = get_settings().app_secret.encode()
    mac = hmac.new(key, f"otp|{digest}|{int(issued_at.timestamp())}".encode(), hashlib.sha256).hexdigest()
    return f"{int(mac[:12], 16) % 10**6:06d}"
