import json

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.agent import loop
from app.agent.llm import LLMReply
from app.config import get_settings


def _make_client(tmp_path, monkeypatch, *, llm: bool):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("LLM_ENABLED", "true" if llm else "false")
    monkeypatch.setenv("LLM_API_KEY", "sk-test" if llm else "")
    monkeypatch.setenv("ADMIN_PASSWORD", "")  # 不受开发者本机 .env 里口令的影响
    get_settings.cache_clear()
    clock.reset()
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture
def client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, llm=False) as c:
        yield c
    get_settings.cache_clear()
    clock.reset()


@pytest.fixture
def llm_client(tmp_path, monkeypatch):
    """模型路径：真实接口换成脚本化的假模型，测试工具循环、事实校验和降级。"""
    with _make_client(tmp_path, monkeypatch, llm=True) as c:
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
