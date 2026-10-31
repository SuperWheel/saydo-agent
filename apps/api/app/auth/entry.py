"""进入前人脸核身（模拟）：打开言行 App 先过一次人脸，服务端签发短期有效的进入凭证，之后 /api 的请求都带上它。

凭证是 HMAC 签名的无状态令牌，声明里带：用户、到期时间、jti、核身批次（epoch）。
- 到期按真实时间算，不按演示时钟：演示里的时间快进（/dev/fast-forward）不该让人脸提前过期，也不该续命。
- 批次存在系统开关表（flags）里：重置演示数据会清空这张表，之前签发的凭证全部作废，App 回到人脸页。
人脸本身是模拟的：前端提交固定的活体断言，服务端只认这一个值。不采集、不上传任何图像，审计里也没有个人信息。
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..flags import get_flag, set_flag

LIVENESS_OK = "face-ok:entry"  # 模拟活体检测通过的断言
_EPOCH_FLAG = "entry_epoch"


class EntryError(Exception):
    pass


def _sign(data: bytes) -> str:
    key = f"entry|{get_settings().app_secret}".encode()  # 和执行令牌分开用途，互相冒充不了
    return base64.urlsafe_b64encode(hmac.new(key, data, hashlib.sha256).digest()).decode().rstrip("=")


def _epoch(db: Session, *, create: bool) -> str | None:
    epoch = get_flag(db, _EPOCH_FLAG).get("value")
    if epoch is None and create:
        epoch = secrets.token_hex(4)
        set_flag(db, _EPOCH_FLAG, {"value": epoch})
    return epoch


def verify_face(db: Session, *, user_id: int, value: str, channel: str = "app") -> dict:
    """核对模拟人脸，通过就签发进入凭证。通过、失败各记一条审计（单独一个 trace，运营后台能回放）。"""
    trace_id = secrets.token_hex(8)
    if not hmac.compare_digest(value.encode(), LIVENESS_OK.encode()):
        audit.record(db, trace_id, "entry_face_failed", {"channel": channel, "factor": "face", "simulated": True})
        raise EntryError("人脸验证未通过，请重试")
    ttl = get_settings().entry_face_ttl_s
    jti = secrets.token_hex(8)
    claims = {"uid": user_id, "exp": int(time.time()) + ttl, "jti": jti, "ep": _epoch(db, create=True)}
    body = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
    audit.record(db, trace_id, "entry_face_passed", {"channel": channel, "factor": "face", "simulated": True,
                                                     "ttl_s": ttl, "jti": jti})
    return {"token": f"{body}.{_sign(body.encode())}", "ttl_s": ttl, "trace_id": trace_id}


def check(db: Session, token: str, user_id: int) -> dict:
    """校验进入凭证：签名、用户、有效期、批次（重置演示数据后作废）。不通过抛 EntryError。"""
    try:
        body, sig = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError) as e:
        raise EntryError("请先完成人脸验证") from e
    if not hmac.compare_digest(sig, _sign(body.encode())) or claims.get("uid") != user_id:
        raise EntryError("请先完成人脸验证")
    if claims.get("exp", 0) < time.time():
        raise EntryError("人脸验证已过期，请重新验证")
    if claims.get("ep") != _epoch(db, create=False):
        raise EntryError("演示数据已重置，请重新完成人脸验证")
    return claims
