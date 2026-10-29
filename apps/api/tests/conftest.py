import pytest
from fastapi.testclient import TestClient

from app import clock
from app.config import get_settings


def _make_client(tmp_path, monkeypatch, *, llm: bool):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("LLM_ENABLED", "true" if llm else "false")
    monkeypatch.setenv("LLM_API_KEY", "sk-test" if llm else "")
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
