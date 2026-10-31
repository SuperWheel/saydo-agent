"""知识检索（向量）设置页：选供应商、填密钥写入 .env、立即生效并后台算条目向量；密钥不回传、不进审计；停用回到 BM25。"""

import time

import pytest

from app import config, knowledge
from app.config import get_settings
from app.knowledge import embedding

KEY_SF = "sk-siliconflow0123456789abcdefghij"
KEY_ZP = "0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOp"


class Fake:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def embed(self, texts, timeout):
        self.calls.append(list(texts))
        if self.fail:
            raise self.fail
        return [[float(len(t) % 7 + 1), 1.0, float(i)] for i, t in enumerate(texts)]


@pytest.fixture
def env_file(client, tmp_path, monkeypatch):
    """临时 .env 和向量缓存（绝不碰开发者本机的那份），去掉会盖住 .env 的环境变量，向量客户端换成假的。"""
    path = tmp_path / ".env"
    path.write_text("# 说明注释\nLLM_PROVIDER=deepseek\n# EMBEDDING_PROVIDER=siliconflow\nAPP_SECRET=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    monkeypatch.setattr(embedding, "CACHE_FILE", tmp_path / "kb_embeddings.json")
    for k in ("EMBEDDING_PROVIDER", "EMBEDDING_API_KEY", "SILICONFLOW_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL",
              "EMBEDDING_DIMENSIONS", "ZHIPUAI_API_KEY", "GLM_API_KEY", "ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    fake = Fake()
    monkeypatch.setattr(embedding, "make_client", lambda settings: fake)
    get_settings.cache_clear()
    embedding.reset()
    yield path
    get_settings.cache_clear()
    embedding.reset()


def _wait_ready(client) -> dict:
    for _ in range(50):
        c = client.get("/admin/embedding-config").json()
        if c["state"] != "building":
            return c
        time.sleep(0.05)
    return c


def test_default_is_bm25_only(client, env_file):
    c = client.get("/admin/embedding-config").json()
    assert c["provider"] == "" and not c["ready"] and c["state"] == "off" and c["state_text"].startswith("只用关键词")
    assert [p["id"] for p in c["providers"]] == ["siliconflow", "zhipu", "openai"]
    assert knowledge.search("跨行转账多久到账").mode == "bm25"


def test_save_siliconflow_key_takes_effect_without_restart(client, env_file):
    r = client.post("/admin/embedding-config", json={"provider": "siliconflow", "api_key": KEY_SF})
    assert r.status_code == 200 and KEY_SF not in r.text  # 密钥不回传
    c = r.json()
    assert c["ready"] and c["model"] == "Qwen/Qwen3-Embedding-8B" and c["dimensions"] == 1024
    sf = next(p for p in c["providers"] if p["id"] == "siliconflow")
    assert sf["has_key"] and sf["key_tail"] == KEY_SF[-4:]
    text = env_file.read_text(encoding="utf-8")
    assert "EMBEDDING_PROVIDER=siliconflow" in text and f"EMBEDDING_API_KEY={KEY_SF}" in text
    assert "APP_SECRET=keep-me" in text and "# EMBEDDING_PROVIDER=siliconflow" in text
    c = _wait_ready(client)
    assert c["state"] == "ready" and c["docs"] == len(knowledge.entries()) and c["state_text"] == "向量 + 关键词混合检索"
    assert knowledge.search("跨行转账多久到账").mode == "hybrid"


def test_other_model_drops_qwen3_dimensions_and_instruction(client, env_file):
    client.post("/admin/embedding-config", json={"provider": "siliconflow", "api_key": KEY_SF, "model": "BAAI/bge-m3"})
    s = get_settings()
    assert s.embedding_model == "BAAI/bge-m3" and s.embedding_dimensions is None and s.embedding_query_instruction is None
    client.post("/admin/embedding-config", json={"provider": "siliconflow", "model": "Qwen/Qwen3-Embedding-4B"})
    s = get_settings()
    assert s.embedding_dimensions == 1024 and s.embedding_query_instruction.startswith("Instruct:")


def test_switch_to_zhipu_reuses_model_key(client, env_file, monkeypatch):
    client.post("/admin/embedding-config", json={"provider": "siliconflow", "api_key": KEY_SF})
    r = client.post("/admin/embedding-config", json={"provider": "zhipu"})
    assert r.status_code == 400 and "智谱的密钥" in r.json()["detail"]  # 没有智谱密钥，旧的硅基流动密钥不能拿去用
    monkeypatch.setenv("ZHIPUAI_API_KEY", KEY_ZP)
    get_settings.cache_clear()
    c = client.post("/admin/embedding-config", json={"provider": "zhipu"}).json()
    assert c["ready"] and c["model"] == "embedding-3" and c["dimensions"] is None
    assert get_settings().embedding_api_key == KEY_ZP and "EMBEDDING_API_KEY=\n" in env_file.read_text(encoding="utf-8")


def test_openai_compatible_needs_url_and_model(client, env_file):
    r = client.post("/admin/embedding-config", json={"provider": "openai", "api_key": KEY_SF})
    assert r.status_code == 400 and "模型名" in r.json()["detail"]
    r = client.post("/admin/embedding-config", json={"provider": "openai", "api_key": KEY_SF, "model": "text-embedding-3-small"})
    assert r.status_code == 400 and "接口地址" in r.json()["detail"]
    c = client.post("/admin/embedding-config", json={"provider": "openai", "api_key": KEY_SF, "model": "text-embedding-3-small",
                                                     "base_url": "https://api.example.com/v1/"}).json()
    assert c["ready"] and c["base_url"] == "https://api.example.com/v1"


@pytest.mark.parametrize("field, bad", [
    ("api_key", "sk-abcdefghijklmnop\nLLM_PROVIDER=glm"), ("model", "x\nADMIN_PASSWORD=1"),
    ("base_url", "https://a.com/v1\nAPP_SECRET=x"), ("api_key", "short"),
])
def test_malformed_input_cannot_inject_lines(client, env_file, field, bad):
    body = {"provider": "siliconflow", "api_key": KEY_SF, field: bad}
    assert client.post("/admin/embedding-config", json=body).status_code == 400
    assert "EMBEDDING" not in env_file.read_text(encoding="utf-8").replace("# EMBEDDING_PROVIDER", "")


def test_disable_returns_to_bm25_and_keeps_key(client, env_file):
    client.post("/admin/embedding-config", json={"provider": "siliconflow", "api_key": KEY_SF})
    _wait_ready(client)
    c = client.post("/admin/embedding-config", json={"disable": True}).json()
    assert c["provider"] == "" and not c["ready"] and c["state"] == "off"
    assert knowledge.search("跨行转账多久到账").mode == "bm25"
    assert f"EMBEDDING_API_KEY={KEY_SF}" in env_file.read_text(encoding="utf-8")
    c = client.post("/admin/embedding-config", json={"provider": "siliconflow"}).json()  # 重新打开不用重填
    assert c["ready"]


def test_audit_never_holds_key(client, env_file):
    client.post("/admin/embedding-config", json={"provider": "siliconflow", "api_key": KEY_SF})
    events = client.get("/admin/audit?trace_id=admin").json()
    ev = [e for e in events if e["event"] == "embedding_config"]
    assert ev and ev[-1]["payload"]["key_updated"] is True and KEY_SF not in str(events)


def test_connection_test_reports_dims_and_hides_key(client, env_file, monkeypatch):
    assert not client.post("/admin/embedding-config/test").json()["ok"]  # 没配置
    client.post("/admin/embedding-config", json={"provider": "siliconflow", "api_key": KEY_SF})
    r = client.post("/admin/embedding-config/test").json()
    assert r["ok"] and r["dimensions"] == 3 and r["model"] == "Qwen/Qwen3-Embedding-8B"
    monkeypatch.setattr(embedding, "make_client", lambda s: Fake(fail=RuntimeError(f"401 invalid key {KEY_SF}")))
    r = client.post("/admin/embedding-config/test").json()
    assert not r["ok"] and KEY_SF not in r["error"] and "***" in r["error"]


def test_index_error_shows_on_status_and_search_stays_bm25(client, env_file, monkeypatch):
    monkeypatch.setattr(embedding, "make_client", lambda s: Fake(fail=RuntimeError("429 余额不足")))
    client.post("/admin/embedding-config", json={"provider": "siliconflow", "api_key": KEY_SF})
    c = _wait_ready(client)
    assert c["state"] == "failed" and "余额不足" in c["last_error"] and c["retry_in_s"] > 0
    r = knowledge.search("跨行转账多久到账")
    assert r.mode == "bm25" and r.entries[0].id == "faq-transfer-interbank"
