"""企业微信智能机器人渠道（长连接模式）。

后端主动连企业微信，不需要公网 IP、回调地址，也不需要消息加解密：
  连接 → aibot_subscribe（BotID + Secret）→ 每 30 秒 ping → 收 aibot_msg_callback → 回 aibot_respond_msg
断线（网络问题、心跳没回应、订阅失败）后按 1、2、4……最长 60 秒退避重连；收到 disconnected_event
（同一个机器人有新连接接管）就停下，不和新连接互相踢。
协议见官方文档「智能机器人长连接」：https://developer.work.weixin.qq.com/document/path/101463

只处理单聊文本消息。群聊里不查账户，请用户私聊；回复内容和渠道降权见 channels/__init__.py。
"""

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable

from websockets.asyncio.client import connect as ws_connect

from ..config import Settings
from ..db import new_session
from . import TEXT_ONLY, im_turn

log = logging.getLogger("saydo.wecom")

CHANNEL = "wecom"
HEARTBEAT_S = 30  # 官方建议 30 秒一次
ACK_TIMEOUT_S = 10  # 订阅回执的等待时间
BACKOFF_MAX_S = 60
SEEN_MAX = 500  # msgid 去重窗口：企业微信可能重推同一条消息
GROUP_REPLY = "为保护你的账户隐私，群聊里不查询账户信息，请私聊我。"
ERROR_REPLY = "服务暂时不可用，请稍后再试，或打开言行 App 办理。"


def new_req_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def subscribe_frame(bot_id: str, secret: str, req_id: str | None = None) -> dict:
    return {"cmd": "aibot_subscribe", "headers": {"req_id": req_id or new_req_id("sub")},
            "body": {"bot_id": bot_id, "secret": secret}}


def ping_frame(req_id: str | None = None) -> dict:
    return {"cmd": "ping", "headers": {"req_id": req_id or new_req_id("ping")}}


def reply_frame(req_id: str, content: str) -> dict:
    """回复一条文本：用流式消息一次发完（finish=true）。req_id 必须透传消息回调里的 req_id。"""
    return {"cmd": "aibot_respond_msg", "headers": {"req_id": req_id},
            "body": {"msgtype": "stream", "stream": {"id": secrets.token_hex(8), "finish": True, "content": content}}}


class SubscribeError(Exception):
    """订阅失败（BotID / Secret 不对、机器人没开 API 长连接模式等）。"""


class WecomBot:
    """一个长连接。connect 和 answer 可注入，测试里换成假的服务端和假的 agent。"""

    def __init__(self, *, bot_id: str, secret: str, url: str, user_id: int, allowed: set[str] | frozenset = frozenset(),
                 proxy: str | None = None, connect: Callable | None = None,
                 answer: Callable[[str, str], str] | None = None,
                 heartbeat_s: float = HEARTBEAT_S, backoff_min_s: float = 1.0, backoff_max_s: float = BACKOFF_MAX_S):
        self.bot_id, self._secret, self.url = bot_id, secret, url
        self.user_id, self.allowed = user_id, allowed
        # 默认直连：企业微信是国内服务；websockets 默认会用系统代理，开发机上的 SOCKS 代理还要额外装库才连得上。
        # 关掉库自带的 WebSocket ping：企业微信服务端不回这种 ping，20 秒后库会以 keepalive 超时断开；保活靠协议里的 cmd=ping。
        # 关掉 permessage-deflate 压缩：实测开着时会出现 incorrect masking / invalid opcode 的帧解析错误
        self._connect = connect or (lambda url: ws_connect(url, proxy=proxy, ping_interval=None, compression=None))
        self._answer = answer or self.answer
        self.heartbeat_s, self.backoff_min_s, self.backoff_max_s = heartbeat_s, backoff_min_s, backoff_max_s
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._pending_pings: set[str] = set()
        self.subscribed = 0  # 订阅成功的次数（首次连接 + 每次重连）
        # 给配置页看的连接状态：connecting / connected / reconnecting / auth_failed / taken_over / stopped
        self.state = "connecting"
        self.last_error = ""
        self.connected_at: float | None = None

    # ---------- 生命周期 ----------

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run(), name="wecom-bot")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        self.state = "stopped"
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def run(self) -> None:
        delay = self.backoff_min_s
        while not self._stop.is_set():
            try:
                async with self._connect(self.url) as ws:
                    await self._serve(ws)
                delay = self.backoff_min_s  # 连上过、正常断开：从头退避
            except asyncio.CancelledError:
                raise
            except SubscribeError as e:
                self.state, self.last_error = "auth_failed", str(e)
                log.error("企业微信机器人订阅失败：%s。请检查 WECOM_BOT_ID / WECOM_BOT_SECRET", e)
            except Exception as e:  # 网络错误、连接被关闭、心跳超时
                self.last_error = repr(e)[:200]
                log.warning("企业微信长连接断开：%r，%.0f 秒后重连", e, delay)
            if self._stop.is_set():
                break
            if self.state != "auth_failed":
                self.state = "reconnecting"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(delay * 2, self.backoff_max_s)

    # ---------- 一条连接 ----------

    async def _send(self, ws, frame: dict) -> None:
        async with self._send_lock:
            await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _subscribe(self, ws) -> None:
        rid = new_req_id("sub")
        await self._send(ws, subscribe_frame(self.bot_id, self._secret, rid))
        async with asyncio.timeout(ACK_TIMEOUT_S):
            while True:
                frame = json.loads(await ws.recv())
                if (frame.get("headers") or {}).get("req_id") == rid:
                    break
        if frame.get("errcode") != 0:
            raise SubscribeError(f"errcode={frame.get('errcode')} errmsg={frame.get('errmsg')}")
        self.subscribed += 1
        self.state, self.last_error, self.connected_at = "connected", "", time.time()
        log.info("企业微信机器人已连接（BotID %s…）", self.bot_id[:6])

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            if len(self._pending_pings) >= 2:  # 连续两次心跳没有回执：连接已经不通，关掉重连
                await ws.close()
                return
            rid = new_req_id("ping")
            self._pending_pings.add(rid)
            await self._send(ws, ping_frame(rid))

    async def _serve(self, ws) -> None:
        self._pending_pings.clear()
        await self._subscribe(ws)
        heartbeat = asyncio.create_task(self._heartbeat(ws))
        handlers: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                frame = json.loads(raw)
                cmd = frame.get("cmd")
                if cmd == "aibot_msg_callback":
                    t = asyncio.create_task(self._on_message(ws, frame))  # agent 可能要几秒，不阻塞收消息和心跳
                    handlers.add(t)
                    t.add_done_callback(handlers.discard)
                elif cmd == "aibot_event_callback":
                    event = ((frame.get("body") or {}).get("event") or {}).get("eventtype")
                    if event == "disconnected_event":
                        log.error("企业微信机器人被新的连接接管，本进程的企业微信渠道停止")
                        self._stop.set()
                        self.state = "taken_over"
                        return
                else:  # 心跳、回复的回执
                    rid = (frame.get("headers") or {}).get("req_id")
                    self._pending_pings.discard(rid)
                    if frame.get("errcode") not in (0, None):
                        log.warning("企业微信回执错误：errcode=%s errmsg=%s", frame.get("errcode"), frame.get("errmsg"))
                    elif rid and rid not in self._pending_pings and not rid.startswith("ping"):
                        log.info("企业微信回执：%s", rid)
        finally:
            heartbeat.cancel()
            for t in handlers:
                t.cancel()

    # ---------- 消息 ----------

    def _dup(self, msgid: str | None) -> bool:
        if not msgid:
            return False
        if msgid in self._seen:
            return True
        self._seen[msgid] = None
        if len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)
        return False

    async def _on_message(self, ws, frame: dict) -> None:
        rid = (frame.get("headers") or {}).get("req_id")
        body = frame.get("body") or {}
        log.info("收到企业微信消息：chattype=%s msgtype=%s", body.get("chattype"), body.get("msgtype"))
        if not rid or self._dup(body.get("msgid")):
            return
        if body.get("chattype") == "group":
            reply = GROUP_REPLY
        elif body.get("msgtype") != "text":
            reply = TEXT_ONLY
        else:
            user = (body.get("from") or {}).get("userid", "")
            text = (body.get("text") or {}).get("content", "")
            reply = await asyncio.to_thread(self._answer, user, text)
        await self._send(ws, reply_frame(rid, reply))

    def answer(self, external_id: str, text: str) -> str:
        """一条单聊文本的回复。在线程里跑：agent 和数据库都是同步的。"""
        with new_session() as db:
            try:
                return im_turn(db, channel=CHANNEL, external_id=external_id, text=text, user_id=self.user_id,
                               allowed=self.allowed).text
            except Exception:
                log.exception("企业微信消息处理失败")
                db.rollback()
                return ERROR_REPLY


def from_settings(settings: Settings, **kwargs) -> WecomBot | None:
    """BotID 和 Secret 都填了才建渠道；没填返回 None，后端照常启动。"""
    if not settings.wecom_ready:
        if settings.wecom_bot_id.strip() or settings.wecom_bot_secret.strip():
            log.warning("WECOM_BOT_ID 和 WECOM_BOT_SECRET 要一起填，企业微信渠道未启动")
        return None
    allowed = frozenset(u.strip() for u in settings.wecom_allowed_users.split(",") if u.strip())
    return WecomBot(bot_id=settings.wecom_bot_id.strip(), secret=settings.wecom_bot_secret.strip(),
                    url=settings.wecom_ws_url, user_id=settings.wecom_bind_user_id or settings.demo_user_id,
                    proxy=settings.wecom_proxy.strip() or None,
                    allowed=allowed, **kwargs)


# ---------- 进程里唯一的一个机器人：启动时按配置建，配置页保存后重建 ----------

_bot: WecomBot | None = None


async def restart(settings: Settings) -> WecomBot | None:
    """停掉旧连接，按当前配置重新连接；没配凭证就只停不连。配置页保存后调用，不用重启后端。"""
    global _bot
    await shutdown()
    _bot = from_settings(settings)
    if _bot:
        _bot.start()
    return _bot


async def shutdown() -> None:
    global _bot
    if _bot:
        await _bot.stop()
    _bot = None


def runtime_status() -> dict:
    if _bot is None:
        return {"state": "off", "last_error": "", "connected_at": None}
    return {"state": _bot.state, "last_error": _bot.last_error, "connected_at": _bot.connected_at}
