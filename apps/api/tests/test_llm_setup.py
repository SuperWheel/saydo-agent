"""模型配置页：DeepSeek / GLM 二选一写入 .env、立即生效；密钥不回传、不进审计；后台口令。"""

import os

import pytest

from app import config
from app.config import get_settings

KEY_DS = "sk-0123456789abcdef0123456789abcdef"
KEY_GLM = "0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOp"


@pytest.fixture
def env_file(client, tmp_path, monkeypatch):
    """临时 .env（绝不碰开发者本机的那份），并去掉会盖住 .env 的环境变量。"""
    path = tmp_path / ".env"
    path.write_text("# 说明注释\nLLM_PROVIDER=deepseek\n# DEEPSEEK_API_KEY=示例行不动\nDEEPSEEK_API_KEY=\nAPP_SECRET=keep-me\n",
                    encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    for k in ("LLM_API_KEY", "LLM_ENABLED", "LLM_PROVIDER", "DEEPSEEK_API_KEY", "ZHIPUAI_API_KEY", "ADMIN_PASSWORD",
              "WORKFLOW_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def test_save_key_takes_effect_without_restart(client, env_file):
    assert not client.get("/api/health").json()["llm"]["ready"]
    r = client.post("/admin/llm-config", json={"provider": "deepseek", "api_key": KEY_DS})
    assert r.status_code == 200 and KEY_DS not in r.text  # 密钥不回传
    ds = next(p for p in r.json()["providers"] if p["id"] == "deepseek")
    assert ds["has_key"] and ds["key_tail"] == KEY_DS[-4:]
    llm = client.get("/api/health").json()["llm"]
    assert llm["ready"] and llm["provider"] == "deepseek"
    text = env_file.read_text(encoding="utf-8")
    assert f"DEEPSEEK_API_KEY={KEY_DS}" in text and "APP_SECRET=keep-me" in text
    assert "# DEEPSEEK_API_KEY=示例行不动" in text and text.count("DEEPSEEK_API_KEY=") == 2
    assert oct(os.stat(env_file).st_mode & 0o777) == "0o600"


def test_switch_provider_keeps_both_keys(client, env_file):
    client.post("/admin/llm-config", json={"provider": "deepseek", "api_key": KEY_DS})
    r = client.post("/admin/llm-config", json={"provider": "glm", "api_key": KEY_GLM})
    assert r.json()["provider"] == "glm" and r.json()["model"] == "glm-5.3-flash"
    r = client.post("/admin/llm-config", json={"provider": "deepseek"})  # 不填密钥：沿用已有的，只切换
    assert r.status_code == 200 and r.json()["provider"] == "deepseek" and r.json()["ready"]
    assert all(p["has_key"] for p in r.json()["providers"])


def test_switch_to_provider_without_key_is_refused(client, env_file):
    r = client.post("/admin/llm-config", json={"provider": "glm"})
    assert r.status_code == 400 and "智谱" in r.json()["detail"]
    assert "LLM_PROVIDER=deepseek" in env_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", [KEY_DS + "\nADMIN_PASSWORD=x", "sk-has space 0123456789", "short"])
def test_malformed_key_cannot_inject_lines(client, env_file, bad):
    before = env_file.read_text(encoding="utf-8")
    r = client.post("/admin/llm-config", json={"provider": "deepseek", "api_key": bad})
    assert r.status_code == 400 and env_file.read_text(encoding="utf-8") == before


def test_legacy_generic_key_moves_to_its_provider(client, env_file):
    env_file.write_text(f"LLM_PROVIDER=glm\nLLM_API_KEY={KEY_GLM}\n", encoding="utf-8")
    get_settings.cache_clear()
    r = client.post("/admin/llm-config", json={"provider": "deepseek", "api_key": KEY_DS})
    text = env_file.read_text(encoding="utf-8")
    assert "LLM_API_KEY=\n" in text and f"ZHIPUAI_API_KEY={KEY_GLM}" in text  # 原来的 GLM 密钥没丢
    assert r.json()["provider"] == "deepseek" and get_settings().llm_api_key == KEY_DS


def test_toggle_rule_mode(client, env_file):
    client.post("/admin/llm-config", json={"provider": "deepseek", "api_key": KEY_DS})
    r = client.post("/admin/llm-config", json={"provider": "deepseek", "enabled": False})
    assert not r.json()["ready"] and not client.get("/api/health").json()["llm"]["ready"]
    client.post("/admin/llm-config", json={"provider": "deepseek", "admin_password": "s3cret-pw"})
    assert env_file.read_text(encoding="utf-8").count("由模型配置页写入") == 1  # 多次追加只留一行说明


def test_toggle_workflow(client, env_file):
    """设置页的「确定性工作流」开关：关掉后固定流程也交给模型（演示时现场对比），立即生效，写进 .env。"""
    client.post("/admin/llm-config", json={"provider": "deepseek", "api_key": KEY_DS})
    assert client.get("/admin/llm-config").json()["workflow"] is True
    r = client.post("/admin/llm-config", json={"provider": "deepseek", "workflow": False})
    assert r.json()["workflow"] is False and not get_settings().workflow_enabled
    assert "WORKFLOW_ENABLED=false" in env_file.read_text(encoding="utf-8")
    assert client.post("/admin/llm-config", json={"provider": "deepseek", "workflow": True}).json()["workflow"] is True


def test_audit_never_holds_secrets(client, env_file):
    client.post("/admin/llm-config", json={"provider": "deepseek", "api_key": KEY_DS, "admin_password": "s3cret-pw"})
    audit_text = client.get("/admin/audit", headers={"X-Admin-Password": "s3cret-pw"}).text
    assert "llm_config" in audit_text and KEY_DS not in audit_text and "s3cret-pw" not in audit_text


def test_admin_password_protects_admin_routes(client, env_file):
    assert client.get("/admin/llm-config").json()["admin_protected"] is False
    client.post("/admin/llm-config", json={"provider": "deepseek", "api_key": KEY_DS, "admin_password": "s3cret-pw"})
    assert client.get("/admin/audit").status_code == 401
    assert client.get("/admin/llm-config", headers={"X-Admin-Password": "wrong-pw"}).status_code == 401
    assert client.get("/admin/llm-config", headers={"X-Admin-Password": "s3cret-pw"}).json()["admin_protected"]
    assert client.get("/api/health").json()["admin_protected"]


def test_admin_closed_outside_demo_mode_without_password(client, env_file, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    assert client.get("/admin/audit").status_code == 403


def test_connection_test_hides_key(client, env_file, monkeypatch):
    from app import llm_setup
    from app.agent.llm import LLMError

    client.post("/admin/llm-config", json={"provider": "deepseek", "api_key": KEY_DS})

    class Boom:
        def chat(self, *a, **k):
            raise LLMError(f"AuthenticationError: Incorrect API key provided: {KEY_DS}")

    monkeypatch.setattr(llm_setup, "get_llm", lambda: Boom())
    r = client.post("/admin/llm-config/test").json()
    assert not r["ok"] and KEY_DS not in r["error"] and "AuthenticationError" in r["error"]
