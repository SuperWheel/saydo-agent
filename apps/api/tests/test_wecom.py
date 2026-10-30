"""企业微信渠道：长连接协议（假服务端）、IM 渠道降权、身份绑定、按配置启动。

协议测试不连真实企业微信：FakeWS 按官方报文格式回订阅 / 心跳回执，并在订阅成功后推送预设的消息回调。
"""

import asyncio
import json
import time

import pytest
from sqlalchemy import select

from app import channels
from app.channels import MORE, NOT_BOUND, READ_CARDS, TEXT_ONLY, im_turn, render, wants_write, wecom
from app.config import Settings, get_settings
from app.db import new_session
from app.models import AuditLog, ChatSession, ImBinding, LimitCounter, PendingAction

H5 = "http://127.0.0.1:5173"
CLOSE = object()


# ---------- 假的企业微信长连接服务端 ----------

class FakeWS:
    def __init__(self, script=(), *, errcode=0, ack_ping=True):
        self.script, self.errcode, self.ack_ping = list(script), errcode, ack_ping
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def push(self, item):
        self.inbox.put_nowait(item)

    async def send(self, raw: str):
        f = json.loads(raw)
        self.sent.append(f)
        rid = f["headers"]["req_id"]
        if f["cmd"] == "aibot_subscribe":
            self.push({"headers": {"req_id": rid}, "errcode": self.errcode, "errmsg": "ok" if self.errcode == 0 else "invalid secret"})
            if self.errcode == 0:
                for item in self.script:
                    self.push(item)
        elif f["cmd"] == "ping" and self.ack_ping:
            self.push({"headers": {"req_id": rid}, "errcode": 0, "errmsg": "ok"})
        elif f["cmd"] == "aibot_respond_msg":
            self.push({"headers": {"req_id": rid}, "errcode": 0, "errmsg": "ok"})

    async def recv(self):
        item = await self.inbox.get()
        if item is CLOSE:
            self.closed = True
            raise ConnectionError("closed")
        return json.dumps(item, ensure_ascii=False)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except ConnectionError:
            raise StopAsyncIteration

    async def close(self):
        self.push(CLOSE)

    def of(self, cmd: str) -> list[dict]:
        return [f for f in self.sent if f["cmd"] == cmd]


class FakeServer:
    """connect 的替身：按顺序交出预设的连接；某一项是异常时模拟连不上。用完了就给空连接。"""

    def __init__(self, *conns):
        self.conns = list(conns)
        self.urls: list[str] = []
        self.made: list[FakeWS] = []

    def __call__(self, url):
        self.urls.append(url)
        item = self.conns.pop(0) if self.conns else FakeWS()
        server = self

        class _Ctx:
            async def __aenter__(self):
                if isinstance(item, Exception):
                    raise item
                server.made.append(item)
                return item

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


def msg(text="我还有多少钱", *, userid="zhangsan", msgid="m1", req_id="cb1", chattype="single", msgtype="text"):
    body = {"msgid": msgid, "aibotid": "bot", "chattype": chattype, "from": {"userid": userid}, "msgtype": msgtype}
    if msgtype == "text":
        body["text"] = {"content": text}
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": req_id}, "body": body}


def make_bot(server, answer=None, **kw):
    return wecom.WecomBot(bot_id="BOT123456", secret="s3cret", url="wss://fake", user_id=1, connect=server,
                          answer=answer or (lambda user, text: f"echo:{user}:{text}"), backoff_min_s=0.01,
                          backoff_max_s=0.05, **kw)


async def until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "等待超时"
        await asyncio.sleep(0.005)


def run_bot(bot, check, timeout=3.0):
    async def main():
        bot.start()
        try:
            await until(check, timeout)
        finally:
            await bot.stop()
    asyncio.run(main())


# ---------- 报文格式 ----------

def test_frames_match_official_format():
    assert wecom.subscribe_frame("BOT", "SEC", "r1") == {
        "cmd": "aibot_subscribe", "headers": {"req_id": "r1"}, "body": {"bot_id": "BOT", "secret": "SEC"}}
    assert wecom.ping_frame("r2") == {"cmd": "ping", "headers": {"req_id": "r2"}}
    f = wecom.reply_frame("cb1", "你好")
    assert f["cmd"] == "aibot_respond_msg" and f["headers"] == {"req_id": "cb1"}
    assert f["body"]["msgtype"] == "stream"
    assert f["body"]["stream"]["finish"] is True and f["body"]["stream"]["content"] == "你好"
    assert f["body"]["stream"]["id"]
    assert wecom.subscribe_frame("B", "S")["headers"]["req_id"] != wecom.subscribe_frame("B", "S")["headers"]["req_id"]


def test_subscribe_then_text_message_gets_reply_with_same_req_id():
    ws = FakeWS([msg("我还有多少钱", userid="zhangsan", req_id="cb-42")])
    server = FakeServer(ws)
    calls = []
    bot = make_bot(server, answer=lambda u, t: calls.append((u, t)) or "余额 1.00 元")
    run_bot(bot, lambda: ws.of("aibot_respond_msg"))
    assert server.urls == ["wss://fake"]
    sub = ws.sent[0]
    assert sub["cmd"] == "aibot_subscribe" and sub["body"] == {"bot_id": "BOT123456", "secret": "s3cret"}
    assert calls == [("zhangsan", "我还有多少钱")]
    reply = ws.of("aibot_respond_msg")[0]
    assert reply["headers"]["req_id"] == "cb-42"
    assert reply["body"]["stream"] == {**reply["body"]["stream"], "finish": True, "content": "余额 1.00 元"}


def test_group_chat_non_text_and_duplicates_are_not_sent_to_agent():
    ws = FakeWS([msg("我还有多少钱", chattype="group", msgid="g1", req_id="c1"),
                 msg(msgtype="image", msgid="i1", req_id="c2"),
                 msg("查余额", msgid="d1", req_id="c3"), msg("查余额", msgid="d1", req_id="c4")])
    calls = []
    bot = make_bot(FakeServer(ws), answer=lambda u, t: calls.append(t) or "ok")
    run_bot(bot, lambda: len(ws.of("aibot_respond_msg")) >= 3)
    replies = {f["headers"]["req_id"]: f["body"]["stream"]["content"] for f in ws.of("aibot_respond_msg")}
    assert replies == {"c1": wecom.GROUP_REPLY, "c2": TEXT_ONLY, "c3": "ok"}
    assert calls == ["查余额"]


def test_heartbeat_pings_with_official_format():
    ws = FakeWS()
    bot = make_bot(FakeServer(ws), heartbeat_s=0.01)
    run_bot(bot, lambda: len(ws.of("ping")) >= 3)
    assert all(set(f) == {"cmd", "headers"} and f["headers"]["req_id"] for f in ws.of("ping"))
    assert not ws.closed  # 有回执就一直连着


def test_reconnects_after_connect_error_server_close_and_missing_pong():
    first = OSError("network down")
    closed_by_server = FakeWS([CLOSE])
    silent = FakeWS(ack_ping=False)  # 心跳没有回执：连续两次后主动断开重连
    last = FakeWS([msg("查余额")])
    server = FakeServer(first, closed_by_server, silent, last)
    bot = make_bot(server, heartbeat_s=0.01)
    run_bot(bot, lambda: last.of("aibot_respond_msg"))
    assert len(server.urls) == 4
    assert bot.subscribed == 3
    assert silent.closed and len(silent.of("ping")) == 2
    assert last.of("aibot_subscribe")  # 每次重连都重新订阅


def test_subscribe_failure_is_retried():
    bad = FakeWS(errcode=40001)
    good = FakeWS([msg("查余额")])
    bot = make_bot(FakeServer(bad, good))
    run_bot(bot, lambda: good.of("aibot_respond_msg"))
    assert bot.subscribed == 1


def test_disconnected_event_stops_without_fighting_new_connection():
    kicked = FakeWS([{"cmd": "aibot_event_callback", "headers": {"req_id": "e1"},
                      "body": {"msgid": "x", "aibotid": "bot", "msgtype": "event",
                               "event": {"eventtype": "disconnected_event"}}}])
    server = FakeServer(kicked)
    bot = make_bot(server)

    async def main():
        await asyncio.wait_for(bot.run(), 3)
    asyncio.run(main())
    assert len(server.urls) == 1


# ---------- 按配置启动 ----------

def test_not_started_without_credentials(caplog):
    assert wecom.from_settings(Settings(wecom_bot_id="", wecom_bot_secret="")) is None
    assert wecom.from_settings(Settings(wecom_bot_id="BOT", wecom_bot_secret=" ")) is None
    assert "要一起填" in caplog.text
    bot = wecom.from_settings(Settings(wecom_bot_id=" BOT ", wecom_bot_secret="SEC", wecom_allowed_users="a, b,",
                                       demo_user_id=1))
    assert bot.bot_id == "BOT" and bot.user_id == 1 and bot.allowed == {"a", "b"}
    assert bot.url == "wss://openws.work.weixin.qq.com"


def test_client_without_credentials_runs_normally(client):
    assert client.get("/api/health").status_code == 200


def test_lifespan_starts_and_stops_channel_when_configured(tmp_path, monkeypatch):
    ws = FakeWS()
    server = FakeServer(ws)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("WECOM_BOT_ID", "BOTLIFE")
    monkeypatch.setenv("WECOM_BOT_SECRET", "SECLIFE")
    monkeypatch.setattr(wecom, "ws_connect", server)
    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import create_app
    try:
        with TestClient(create_app()) as c:
            deadline = time.monotonic() + 3
            while not ws.of("aibot_subscribe") and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ws.of("aibot_subscribe")[0]["body"] == {"bot_id": "BOTLIFE", "secret": "SECLIFE"}
            assert c.get("/api/health").status_code == 200
    finally:
        get_settings.cache_clear()


# ---------- 渠道降权与回复 ----------

def turn(text, external_id="zhangsan", **kw):
    with new_session() as db:
        return im_turn(db, channel="wecom", external_id=external_id, text=text, user_id=1, **kw)


def test_read_only_query_goes_through_agent_and_renders_plain_text(client):
    r = turn("我还有多少钱")
    assert not r.downgraded
    assert "元" in r.text and "尾号" in r.text and len(r.text) <= 220
    assert "**" not in r.text and "{" not in r.text
    with new_session() as db:
        b = db.scalars(select(ImBinding)).one()
        assert (b.channel, b.external_id, b.user_id) == ("wecom", "zhangsan", 1)
        assert db.get(ChatSession, b.session_id).channel == "wecom"
        msg_events = db.scalars(select(AuditLog).where(AuditLog.trace_id == r.trace_id,
                                                       AuditLog.event_type == "user_message")).all()
        assert msg_events[0].payload["channel"] == "wecom"
    turn("这个月花了多少")
    with new_session() as db:  # 同一个企业微信账号的消息落在同一个会话里
        assert len(db.scalars(select(ChatSession).where(ChatSession.channel == "wecom")).all()) == 1


@pytest.mark.parametrize("text", ["钱都花哪了", "我的卡状态", "我有哪些订阅", "我的理财这个月赚了多少", "跨行转账多久到账"])
def test_other_read_only_queries_are_answered(client, text):
    r = turn(text)
    assert not r.downgraded and r.text and H5 not in r.text and len(r.text) <= 220
    assert "见卡片" not in r.text


@pytest.mark.parametrize("text", ["给赵磊转100", "和小王小李AA 300", "把尾号1234的卡锁了", "取消爱奇艺",
                                  "帮我买稳盈30天 1万", "下周六老婆生日订花订蛋糕", "存5000到心愿罐"])
def test_write_requests_are_downgraded_before_agent(client, text):
    r = turn(text)
    assert r.downgraded
    assert r.text == f"这项操作需要在言行 App 内确认，请打开言行 App 办理：{H5}"
    with new_session() as db:
        assert db.scalars(select(PendingAction)).all() == []
        ev = db.scalars(select(AuditLog).where(AuditLog.trace_id == r.trace_id)).one()
        assert ev.event_type == "channel_downgraded" and ev.payload["stage"] == "intent"
    assert client.post("/admin/audit/verify").json()["ok"]


def test_write_that_slips_past_intent_check_is_cancelled_after_agent(client, monkeypatch):
    """规则没认出来、模型提议了写操作：确认卡不展示，待确认动作撤销，预占额度释放。"""
    monkeypatch.setattr(channels, "wants_write", lambda text: False)
    r = turn("给赵磊转100")
    assert r.downgraded and H5 in r.text and "确认卡" not in r.text
    with new_session() as db:
        a = db.scalars(select(PendingAction).where(PendingAction.trace_id == r.trace_id)).one()
        assert a.status == "cancelled"
        assert sum(c.reserved_fen for c in db.scalars(select(LimitCounter))) == 0
        events = [e.event_type for e in db.scalars(select(AuditLog).where(AuditLog.trace_id == r.trace_id))]
        assert {"action_created", "action_cancelled", "channel_downgraded"} <= set(events)


def test_follow_up_question_of_write_is_not_kept(client, monkeypatch):
    real = channels.wants_write
    skip = {"on": True}
    monkeypatch.setattr(channels, "wants_write", lambda text: False if skip["on"] else real(text))
    assert turn("给赵磊转").downgraded  # 「要转多少钱？」是写操作的追问
    with new_session() as db:
        s = db.scalars(select(ChatSession).where(ChatSession.channel == "wecom")).one()
        assert not (s.state or {}).get("pending_question")
    skip["on"] = False
    assert not turn("100").downgraded  # 下一句金额不会接着办转账
    with new_session() as db:
        assert db.scalars(select(PendingAction)).all() == []


def test_h5_link_is_configurable(client, monkeypatch):
    monkeypatch.setenv("APP_H5_URL", "http://192.168.1.8:5173")
    get_settings.cache_clear()
    assert turn("给赵磊转100").text.endswith("http://192.168.1.8:5173")


def test_binding_allowlist_and_empty_text(client):
    assert turn("我还有多少钱", external_id="stranger", allowed=frozenset({"zhangsan"})).text == NOT_BOUND
    assert turn("   ").text == TEXT_ONLY
    with new_session() as db:
        assert [b.external_id for b in db.scalars(select(ImBinding))] == ["zhangsan"]


def test_wants_write_classification():
    for t in ["我还有多少钱", "这个月花了多少", "有没有异常消费", "我的卡状态", "未来一周有什么扣费",
              "信用卡挂失要钱吗", "上个月账单报告"]:
        assert not wants_write(t), t
    for t in ["给我妈转500", "我的卡丢了", "给我推荐理财", "每月1号给我妈转2000", "关闭境外支付",
              "查下余额，然后给赵磊转100"]:
        assert wants_write(t), t


def test_render_limits_length_and_masks():
    cards = [{"type": "balance", "accounts": [{"name": "借记卡", "tail": "1234", "balance": "1.00"}] * 3, "total": "3.00"}]
    out = render("你名下账户余额合计 3.00 元。", cards)
    assert out.splitlines()[0] == "你名下账户余额合计 3.00 元。" and "借记卡（尾号1234）：1.00 元" in out
    assert out.count("借记卡") == 1  # 重复的要点只列一次
    long = render("。".join(["这是一句很长的回复"] * 40) + "。", [])
    assert len(long) <= 200 + len(MORE) + 1 and long.endswith(MORE)
    assert "13812345678" not in render("你的手机号 13812345678", [])
    assert render("你名下共有 2 张卡，状态和限额见卡片。", []) == "你名下共有 2 张卡。"  # IM 里没有卡片可看
    assert render("10月24日 一家未登记商户（名称见卡片） 2,999.00 元。详情见卡片。", []) == "10月24日 一家未登记商户 2,999.00 元。"
    assert {"confirm", "auth", "receipt", "plan", "clarify", "contact"}.isdisjoint(READ_CARDS)
