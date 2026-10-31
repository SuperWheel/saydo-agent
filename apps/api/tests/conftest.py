import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import clock, netinfo
from app.agent import loop
from app.agent.llm import LLMReply
from app.config import get_settings


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """每条测试从固定时刻开始；DEMO_DATE、reset 和 travel 照常生效，不随运行耗时跨秒 / 跨日。"""
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            at = datetime(2026, 10, 30, 12, tzinfo=clock.TZ)  # 日历种子覆盖 2026–2027 年
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)

    monkeypatch.setattr(clock, "datetime", FrozenDateTime)
    monkeypatch.setattr(clock, "_offset", clock._offset)  # 独立建库的测试也不能把快进留给下一条
    clock.reset()


def _make_client(tmp_path, monkeypatch, *, llm: bool):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("LLM_ENABLED", "true" if llm else "false")
    monkeypatch.setenv("LLM_API_KEY", "sk-test" if llm else "")
    monkeypatch.setenv("ADMIN_PASSWORD", "")  # 不受开发者本机 .env 里口令的影响
    monkeypatch.setenv("WECOM_BOT_ID", "")  # 本机 .env 填了企业微信凭证时，测试也不去连企业微信
    monkeypatch.setenv("WECOM_BOT_SECRET", "")
    monkeypatch.setenv("ENTRY_FACE_REQUIRED", "true")  # 测试里也校验进入凭证，client 夹具会先过一次人脸
    get_settings.cache_clear()
    clock.reset()
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _no_lan(monkeypatch):
    """引导链接里的 127.0.0.1 会换成本机局域网地址；测试里固定成没有局域网，结果不随开发机的网络变。"""
    monkeypatch.setattr(netinfo, "lan_ips", lambda: [])


def enter(client) -> dict:
    """进入前人脸核身（模拟），之后这个客户端的请求都带上进入凭证。重置演示数据（/dev/seed）后要重新调一次。"""
    r = client.post("/api/auth/entry-face", json={"value": "face-ok:entry"})
    assert r.status_code == 200, r.text
    client.headers["X-Entry-Token"] = r.json()["token"]
    return r.json()


@pytest.fixture
def client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, llm=False) as c:
        enter(c)
        yield c
    get_settings.cache_clear()
    clock.reset()


@pytest.fixture
def llm_client(tmp_path, monkeypatch):
    """模型路径：真实接口换成脚本化的假模型，测试工具循环、事实校验和降级。"""
    with _make_client(tmp_path, monkeypatch, llm=True) as c:
        enter(c)
        yield c
    get_settings.cache_clear()
    clock.reset()


def say(client, text, session_id=None, **extra):
    r = client.post("/api/chat", json={"text": text, "session_id": session_id, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def confirm(client, card):
    return client.post(f"/api/actions/{card['action_id']}/confirm", json={"digest": card["digest"]})


# ---------- 脚本化的假模型：按顺序返回预设的回复，不调用真实接口 ----------

class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _ToolCall:
    def __init__(self, name, args, id="call_1"):
        self.id, self.type = id, "function"
        self.function = _Fn(name, json.dumps(args, ensure_ascii=False))

    def model_dump(self, **_):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


def tool(name, **args):
    return LLMReply(content="", tool_calls=[_ToolCall(name, args)], usage={})


def text(content):
    return LLMReply(content=content, tool_calls=[], usage={})


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

        class _S:
            llm_model = "fake"
        self.settings = _S()

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def fake(monkeypatch):
    def install(*script):
        f = FakeLLM(script)
        monkeypatch.setattr(loop, "get_llm", lambda: f)
        # 这些测试验的是模型路径（工具参数校验、事实校验、虚构卡片），固定流程的工作流只留查余额，
        # 否则「给我妈转 500」这类话会被工作流截走、根本不调模型。工作流本身见 test_workflow.py
        monkeypatch.setattr(loop, "FAST_INTENTS", {"balance"})
        return f
    return install
