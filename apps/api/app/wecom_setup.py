"""企业微信配置页的后端：Bot ID、Secret 写进仓库根目录的 .env，保存后立即重连，不用重启。

和模型配置页一样：Secret 只进 .env，不回传前端（页面只看到「已填写」），不写审计。
"""

import contextlib
import ipaddress
import os
import re
import socket
import subprocess
import sys

from . import config
from .channels import wecom
from .config import get_settings
from .llm_setup import ConfigError, update_env

# 只收字母、数字和 _ - . + / =（Secret 可能是 base64）：拒绝空白、换行、# 和引号，防止借凭证往 .env 里夹带别的配置项
_ID_OK = re.compile(r"[A-Za-z0-9_\-.]{6,128}")
_SECRET_OK = re.compile(r"[A-Za-z0-9_\-.+/=]{8,200}")
_URL_OK = re.compile(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9_\-./]*)?")
_WATCHED = ("WECOM_BOT_ID", "WECOM_BOT_SECRET", "APP_H5_URL")
_MARK = "# 以下由企业微信设置页写入"

STATE_TEXT = {
    "off": "未启用",
    "connecting": "正在连接…",
    "connected": "已连接",
    "reconnecting": "连接断开，正在重连…",
    "auth_failed": "Bot ID 或 Secret 不对，企业微信拒绝了连接",
    "taken_over": "这个机器人在别处建立了新连接，本机已停止",
    "stopped": "已停止",
}


def _masked(bot_id: str) -> str:
    return f"{bot_id[:4]}…{bot_id[-4:]}" if len(bot_id) > 10 else bot_id[:2] + "…"


def _private_v4(ip: str) -> bool:
    """局域网地址：10/8、172.16/12、192.168/16。排除代理软件 TUN 模式的 198.18/15 和 CGNAT 的 100.64/10。"""
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return a.is_private and not a.is_loopback and a not in ipaddress.IPv4Network("198.18.0.0/15")


def lan_ips() -> list[str]:
    """本机的局域网 IPv4，给设置页的「填入本机局域网地址」用。手机连电脑热点、或和电脑在同一 Wi-Fi 时用这个地址访问。"""
    found: list[str] = []
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("223.5.5.5", 53))  # UDP connect 不发包，只让系统选出口网卡
        found.append(s.getsockname()[0])
    if sys.platform == "darwin":  # 开着代理软件的 TUN 模式时出口是虚拟网卡，再直接问 Wi-Fi / 有线网卡
        for nic in ("en0", "en1"):
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                out = subprocess.run(["ipconfig", "getifaddr", nic], capture_output=True, text=True, timeout=2)
                found.append(out.stdout.strip())
    return list(dict.fromkeys(ip for ip in found if _private_v4(ip)))


def status() -> dict:
    s = get_settings()
    rt = wecom.runtime_status()
    bot_id = s.wecom_bot_id.strip()
    warnings = [f"进程环境变量 {k} 优先于 .env，这里保存后对它不生效" for k in _WATCHED if os.environ.get(k)]
    return {"configured": s.wecom_ready, "bot_id": _masked(bot_id) if bot_id else "",
            "has_secret": bool(s.wecom_bot_secret.strip()), "app_h5_url": s.app_h5_url,
            **rt, "state_text": STATE_TEXT.get(rt["state"], rt["state"]),
            "env_file": str(config.ENV_FILE), "lan_ips": lan_ips(), "warnings": warnings}


def save(bot_id: str = "", secret: str = "", app_h5_url: str = "", disable: bool = False) -> None:
    """写入 .env 并重新加载配置；重连由调用方做（要在事件循环里）。
    bot_id / secret 留空表示沿用已填写的；disable=True 清空两项、停用渠道。"""
    if disable:
        update_env(config.ENV_FILE, {"WECOM_BOT_ID": "", "WECOM_BOT_SECRET": ""}, _MARK)
        get_settings.cache_clear()
        return
    bot_id, secret, app_h5_url = bot_id.strip(), secret.strip(), app_h5_url.strip().rstrip("/")
    if bot_id and not _ID_OK.fullmatch(bot_id):
        raise ConfigError("Bot ID 格式不对：应为 6–128 位字母、数字或 _ - .，不能有空格")
    if secret and not _SECRET_OK.fullmatch(secret):
        raise ConfigError("Secret 格式不对：应为 8–200 位字母、数字或 _ - . + / =，不能有空格和换行")
    if app_h5_url and not _URL_OK.fullmatch(app_h5_url):
        raise ConfigError("App 地址应形如 http://192.168.1.8:5173")
    s = get_settings()
    if not (bot_id or s.wecom_bot_id.strip()):
        raise ConfigError("请填写 Bot ID")
    if not (secret or s.wecom_bot_secret.strip()):
        raise ConfigError("请填写 Secret（在机器人配置页点「点击获取」）")
    updates = {}
    if bot_id:
        updates["WECOM_BOT_ID"] = bot_id
    if secret:
        updates["WECOM_BOT_SECRET"] = secret
    if app_h5_url:
        updates["APP_H5_URL"] = app_h5_url
    if updates:
        update_env(config.ENV_FILE, updates, _MARK)
    get_settings.cache_clear()
