"""模型客户端：供应商预设和各家参数差异（GLM 思考模式关不掉、tool_choice 只认 auto）。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent.llm import LLMClient
from app.config import Settings

_ENV = ("LLM_PROVIDER", "LLM_API_KEY", "DEEPSEEK_API_KEY", "ZHIPUAI_API_KEY", "GLM_API_KEY", "LLM_MODEL",
        "LLM_BASE_URL", "LLM_THINKING", "LLM_REASONING_EFFORT", "LLM_TEMPERATURE", "LLM_MAX_TOKENS", "LLM_TIMEOUT_S")


def settings(monkeypatch, **env) -> Settings:
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # 不读开发者本机的 .env


class _Completions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        msg = SimpleNamespace(content="", tool_calls=None, reasoning_content="用户想知道余额，先查账户")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


def client(s: Settings) -> tuple[LLMClient, _Completions]:
    c, comp = LLMClient(s), _Completions()
    c._client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return c, comp


def test_deepseek_is_default(monkeypatch):
    s = settings(monkeypatch, DEEPSEEK_API_KEY="sk-ds")
    assert (s.llm_provider, s.llm_model, s.llm_base_url, s.llm_thinking) == (
        "deepseek", "deepseek-flash", "https://api.deepseek.com", "disabled")
    assert s.llm_api_key == "sk-ds" and s.llm_ready


def test_glm_preset_and_key_alias(monkeypatch):
    s = settings(monkeypatch, LLM_PROVIDER="GLM", GLM_API_KEY="zp-key", DEEPSEEK_API_KEY="sk-ds")
    assert (s.llm_model, s.llm_base_url, s.llm_thinking) == (
        "glm-5.3-flash", "https://open.bigmodel.cn/api/paas/v4", "enabled")
    assert s.llm_api_key == "zp-key"  # 只取当前供应商的密钥


def test_explicit_values_override_preset(monkeypatch):
    s = settings(monkeypatch, LLM_PROVIDER="glm", LLM_MODEL="glm-5.3-flashx", LLM_API_KEY="k", LLM_REASONING_EFFORT="")
    assert s.llm_model == "glm-5.3-flashx" and s.llm_reasoning_effort == "" and s.llm_max_tokens == 4096


def test_unknown_provider_is_rejected(monkeypatch):
    with pytest.raises(ValidationError):
        settings(monkeypatch, LLM_PROVIDER="openai")


def test_glm_request_shape(monkeypatch):
    c, comp = client(settings(monkeypatch, LLM_PROVIDER="glm", ZHIPUAI_API_KEY="k"))
    reply = c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}], tool_choice="none")
    assert "tool_choice" not in comp.kwargs  # GLM 不支持 none，不传
    assert comp.kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert comp.kwargs["reasoning_effort"] == "low" and comp.kwargs["model"] == "glm-5.3-flash"
    assert reply.reasoning == "用户想知道余额，先查账户"


def test_deepseek_request_shape(monkeypatch):
    c, comp = client(settings(monkeypatch, DEEPSEEK_API_KEY="k"))
    c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}], tool_choice="none")
    assert comp.kwargs["tool_choice"] == "none" and "reasoning_effort" not in comp.kwargs
    assert comp.kwargs["extra_body"] == {"thinking": {"type": "disabled"}} and comp.kwargs["temperature"] == 0.2
