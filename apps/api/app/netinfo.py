"""本机局域网地址：企业微信里引导回 App 的链接要让手机打得开，不能是 127.0.0.1。"""

import contextlib
import ipaddress
import socket
import subprocess
import sys
from urllib.parse import urlsplit

_LOOPBACK = ("127.0.0.1", "localhost")


def _private_v4(ip: str) -> bool:
    """局域网地址：10/8、172.16/12、192.168/16。排除代理软件 TUN 模式的 198.18/15 和 CGNAT 的 100.64/10。"""
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return a.is_private and not a.is_loopback and a not in ipaddress.IPv4Network("198.18.0.0/15")


def lan_ips() -> list[str]:
    """本机的局域网 IPv4。手机连电脑热点、或和电脑在同一 Wi-Fi 时用这个地址访问。"""
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


def phone_url(url: str) -> str:
    """App 地址还是本机回环时换成当前的局域网地址，手机才打得开；换了网络自动跟着变。填了别的地址就原样用。"""
    host = urlsplit(url).hostname
    if host in _LOOPBACK and (ips := lan_ips()):
        return url.replace(host, ips[0], 1)
    return url
