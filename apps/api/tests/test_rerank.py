"""知识库重排：默认用硅基流动、有密钥即开、off 关闭、供应商预设和门槛、两种接口格式、重排决定查到还是拒答、超时 / 报错回退且结果和不开一致、
冷却、查询脱敏、审计；以及设置页（保存 / 停用 / 切换供应商 / 测试连接，密钥不回传、不进审计）。全部用假客户端，不联网。"""

import json
import time

import httpx
import pytest

from app import config, knowledge
from app.config import Settings, get_settings
from app.knowledge import embedding, rerank

KEY_SF = "sk-siliconflow0123456789abcdefghij"
KEY_DS = "sk-dashscope0123456789abcdefghijkl"
QUERY = "跨行转账多久到账"


class FakeReranker:
    """假的重排接口：条目按 id 给分（scores），没登记的 0.01；可设 fail / delay。记下每次收到的问题和候选。"""

    def __init__(self, scores=None, fail=None, delay=0.0):
        self.scores, self.fail, self.delay = scores or {}, fail, delay
        self.calls: list[tuple[str, list[str]]] = []
        self.by_text = {t: e.id for t, e in zip(knowledge.doc_texts(), knowledge._index().docs)}

    def rerank(self, query, docs, timeout):
        self.calls.append((query, list(docs)))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return [self.scores.get(self.by_text[d], 0.01) for d in docs]


@pytest.fixture
def rr_on(monkeypatch):
    """install(fake, **env)：打开重排（默认硅基流动预设、只用关键词候选）并换成假客户端。"""

    def install(fake, **env):
        monkeypatch.setenv("RERANK_PROVIDER", "siliconflow")
        monkeypatch.setenv("SILICONFLOW_API_KEY", KEY_SF)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(rerank, "make_client", lambda settings: fake)
        get_settings.cache_clear()
        rerank.reset()
        return fake

    return install


def _ids(r):
    return [e.id for e in r.entries]


def _baseline(query):
    r = knowledge.search(query)
    return _ids(r), r.coverage, r.mode, r.fallback


# ---------- 配置 ----------

def test_on_by_default_with_siliconflow_key(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")  # 不读开发者本机的 .env（里面可能有真密钥）
    monkeypatch.delenv("RERANK_PROVIDER", raising=False)
    monkeypatch.delenv("RERANK_API_KEY", raising=False)
    monkeypatch.setenv("SILICONFLOW_API_KEY", KEY_SF)
    get_settings.cache_clear()
    s = get_settings()
    assert s.rerank_provider == "siliconflow" and s.rerank_ready and s.rerank_model == "Qwen/Qwen3-Reranker-4B"
    monkeypatch.setenv("RERANK_PROVIDER", "")  # 旧版设置页停用时写的空值也按默认打开
    get_settings.cache_clear()
    assert get_settings().rerank_ready
    monkeypatch.delenv("SILICONFLOW_API_KEY")
    get_settings.cache_clear()
    assert not get_settings().rerank_ready  # 没有密钥就不重排


def test_off_disables_even_with_key(monkeypatch):
    monkeypatch.setenv("RERANK_PROVIDER", "off")
    monkeypatch.setenv("SILICONFLOW_API_KEY", KEY_SF)
    get_settings.cache_clear()
    s = get_settings()
    assert s.rerank_provider == "" and not s.rerank_ready and not rerank.enabled()
    r = knowledge.search(QUERY)
    assert r.mode == "bm25" and r.rerank_score is None and r.rerank_fallback == ""


def _settings(monkeypatch, **env) -> Settings:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_presets_thresholds_and_aliases(monkeypatch):
    s = _settings(monkeypatch, RERANK_PROVIDER="siliconflow", SILICONFLOW_API_KEY=KEY_SF)
    assert (s.rerank_model, s.rerank_base_url, s.rerank_api_key, s.rerank_min_score, s.rerank_timeout_s) == (
        "Qwen/Qwen3-Reranker-4B", "https://api.siliconflow.cn/v1", KEY_SF, 0.525, 3.0)
    assert s.rerank_ready and s.rerank_format == "siliconflow"
    s = _settings(monkeypatch, RERANK_MODEL="Qwen/Qwen3-Reranker-0.6B")
    assert s.rerank_min_score == 0.502
    s = _settings(monkeypatch, RERANK_MODEL="BAAI/bge-reranker-v2-m3", RERANK_MIN_SCORE="0.3")
    assert s.rerank_min_score == 0.3  # 写了就用写的
    s = _settings(monkeypatch, RERANK_MODEL="some/new-reranker", RERANK_MIN_SCORE="")
    assert s.rerank_min_score == 0.55  # 没校准过的模型取保守默认值
    for alias in ("qwen", "dashscope", "bailian", "QianWen"):
        s = _settings(monkeypatch, RERANK_PROVIDER=alias, RERANK_MODEL="", RERANK_API_KEY="", DASHSCOPE_API_KEY=KEY_DS)
        assert (s.rerank_provider, s.rerank_model, s.rerank_base_url, s.rerank_api_key, s.rerank_format) == (
            "qwen", "qwen3-rerank", "https://dashscope.aliyuncs.com/api/v1", KEY_DS, "dashscope")
    with pytest.raises(ValueError, match="RERANK_PROVIDER"):
        _settings(monkeypatch, RERANK_PROVIDER="cohere")


def test_qwen_embedding_preset(monkeypatch):
    s = _settings(monkeypatch, EMBEDDING_PROVIDER="dashscope", DASHSCOPE_API_KEY=KEY_DS)
    assert (s.embedding_provider, s.embedding_model, s.embedding_base_url, s.embedding_dimensions, s.embedding_api_key) == (
        "qwen", "text-embedding-v4", "https://dashscope.aliyuncs.com/compatible-mode/v1", 1024, KEY_DS)
    assert s.embedding_query_instruction is None and s.embedding_ready
    s = _settings(monkeypatch, EMBEDDING_MODEL="text-embedding-v3")
    assert s.embedding_dimensions is None  # 预设维度只跟预设模型走


# ---------- 接口格式 ----------

@pytest.mark.parametrize("env, url, native", [
    ({"RERANK_PROVIDER": "siliconflow"}, "https://api.siliconflow.cn/v1/rerank", False),
    ({"RERANK_PROVIDER": "qwen"}, "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank", True),
    ({"RERANK_PROVIDER": "qwen", "RERANK_BASE_URL": "https://ws1.cn-beijing.maas.aliyuncs.com/api/v1"},
     "https://ws1.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank", True),
    ({"RERANK_PROVIDER": "qwen", "RERANK_BASE_URL": "https://cn-beijing.maas.qwencloudapi.com/compatible-api/v1"},
     "https://cn-beijing.maas.qwencloudapi.com/compatible-api/v1/reranks", False),
    ({"RERANK_PROVIDER": "siliconflow", "RERANK_BASE_URL": "https://example.com/v2/rerank"}, "https://example.com/v2/rerank", False),
])
def test_endpoint(monkeypatch, env, url, native):
    s = _settings(monkeypatch, SILICONFLOW_API_KEY=KEY_SF, DASHSCOPE_API_KEY=KEY_DS, **env)
    assert rerank.endpoint(s) == (url, native)


def _client_with(monkeypatch, provider, handler):
    s = _settings(monkeypatch, RERANK_PROVIDER=provider, SILICONFLOW_API_KEY=KEY_SF, DASHSCOPE_API_KEY=KEY_DS)
    c = rerank.Client(s)
    c._http = httpx.Client(transport=httpx.MockTransport(handler), headers=c._http.headers)
    return c


def test_siliconflow_request_and_response(monkeypatch):
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.content))
        return httpx.Response(200, json={"id": "x", "results": [{"index": 1, "relevance_score": 0.9},
                                                                 {"index": 0, "relevance_score": 0.2}]})

    assert _client_with(monkeypatch, "siliconflow", handler).rerank("问", ["甲", "乙"], 1.0) == [0.2, 0.9]
    assert seen["url"] == "https://api.siliconflow.cn/v1/rerank" and seen["auth"] == f"Bearer {KEY_SF}"
    assert seen["body"] == {"model": "Qwen/Qwen3-Reranker-4B", "query": "问", "documents": ["甲", "乙"], "top_n": 2,
                            "return_documents": False}


def test_dashscope_native_request_and_response(monkeypatch):
    """千问（百炼）原生 text-rerank：参数放 input / parameters，结果在 output.results（按官方文档，未用真实密钥实测）。"""
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.content))
        return httpx.Response(200, json={"request_id": "r", "output": {"results": [{"index": 0, "relevance_score": 0.7}]},
                                         "usage": {"total_tokens": 9}})

    assert _client_with(monkeypatch, "qwen", handler).rerank("问", ["甲", "乙"], 1.0) == [0.7, 0.0]
    assert seen["url"].endswith("/api/v1/services/rerank/text-rerank/text-rerank") and seen["auth"] == f"Bearer {KEY_DS}"
    assert seen["body"] == {"model": "qwen3-rerank", "input": {"query": "问", "documents": ["甲", "乙"]},
                            "parameters": {"top_n": 2, "return_documents": False}}


def test_http_error_and_bad_payload_raise(monkeypatch):
    c = _client_with(monkeypatch, "siliconflow", lambda req: httpx.Response(401, json={"message": "Invalid token"}))
    with pytest.raises(rerank.RerankError, match="401"):
        c.rerank("问", ["甲"], 1.0)
    c = _client_with(monkeypatch, "siliconflow", lambda req: httpx.Response(200, json={"results": [{"index": 5}]}))
    with pytest.raises(rerank.RerankError, match="越界"):
        c.rerank("问", ["甲"], 1.0)
    c = _client_with(monkeypatch, "siliconflow", lambda req: httpx.Response(200, json={"code": 50507}))
    with pytest.raises(rerank.RerankError, match="results"):
        c.rerank("问", ["甲"], 1.0)


# ---------- 检索 ----------

def test_rerank_orders_and_decides_hit(rr_on):
    fake = rr_on(FakeReranker({"faq-transfer-interbank": 0.95, "faq-transfer-inner": 0.6, "reg-atm-24h": 0.2}))
    r = knowledge.search(QUERY)
    assert r.mode == "rerank" and r.rerank_score == 0.95 and r.rerank_fallback == ""
    # 第二条 0.6 ≥ 0.95 × 0.3 带上；0.2 < 0.285 不带
    assert _ids(r) == ["faq-transfer-interbank", "faq-transfer-inner"]
    (q, docs), = fake.calls
    assert q == QUERY and 0 < len(docs) <= knowledge.RERANK_POOL and set(docs) <= set(knowledge.doc_texts())


def test_rerank_below_threshold_refuses_even_if_bm25_hits(rr_on):
    rr_on(FakeReranker({"faq-transfer-interbank": 0.4}))
    assert _ids(knowledge.search(QUERY)) == []
    r = knowledge.search(QUERY)
    assert r.mode == "rerank" and r.rerank_score == 0.4 and not r.hits


def test_rerank_can_accept_what_bm25_alone_rejects(rr_on):
    """「怎么证明那笔钱不是我自己花的」纯 BM25 不过门槛；重排给了高分就算查到（门槛改由重排决定）。"""
    q = "怎么证明那笔钱不是我自己花的"
    assert _ids(knowledge.search(q)) == []
    rr_on(FakeReranker({"reg-card-fraud-evidence": 0.9}))
    r = knowledge.search(q)
    assert r.mode == "rerank" and _ids(r) == ["reg-card-fraud-evidence"]


def test_custom_threshold(rr_on):
    rr_on(FakeReranker({"faq-transfer-interbank": 0.4}), RERANK_MIN_SCORE="0.3")
    assert _ids(knowledge.search(QUERY))[0] == "faq-transfer-interbank"


@pytest.mark.parametrize("query", [QUERY, "信用卡挂失要钱吗", "怎么证明那笔钱不是我自己花的", "今天天气怎么样"])
def test_error_falls_back_identical_and_cools_down(rr_on, query):
    base = _baseline(query)
    fake = rr_on(FakeReranker(fail=RuntimeError("401 bad key")))
    r = knowledge.search(query)
    assert (_ids(r), r.coverage, r.mode, r.fallback) == base
    if fake.calls:
        assert r.rerank_fallback == "RuntimeError"
        assert rerank.runtime_status()["state"] == "failed" and rerank.runtime_status()["retry_in_s"] > 50
        r2 = knowledge.search(query)  # 冷却期内不再调，也不再记回退
        assert len(fake.calls) == 1 and r2.rerank_fallback == "" and _ids(r2) == base[0]


def test_timeout_falls_back_fast_with_short_cooldown(rr_on):
    base = _baseline(QUERY)
    rr_on(FakeReranker({"faq-transfer-interbank": 0.9}, delay=1.0), RERANK_TIMEOUT_S="0.2")
    t0 = time.perf_counter()
    r = knowledge.search(QUERY)
    assert time.perf_counter() - t0 < 0.8
    assert (_ids(r), r.coverage, r.mode, r.fallback, r.rerank_fallback) == (*base, "timeout")
    assert 0 < rerank.runtime_status()["retry_in_s"] <= rerank.TIMEOUT_COOLDOWN_S


def test_query_is_masked_and_cached(rr_on):
    fake = rr_on(FakeReranker({"faq-transfer-interbank": 0.9}))
    q = "我手机号13812345678，跨行转账多久到账"
    knowledge.search(q)
    knowledge.search(q)
    assert len(fake.calls) == 1 and "13812345678" not in fake.calls[0][0]


def test_no_candidates_refuses_without_calling(rr_on):
    fake = rr_on(FakeReranker({"faq-handoff": 0.9}))
    r = knowledge.search("嗯嗯呃呃")  # 一个关键词都对不上，又没有向量：没有候选
    assert not r.hits and fake.calls == []


def test_vector_candidates_reach_reranker_when_bm25_finds_nothing(rr_on, monkeypatch, tmp_path):
    """配了向量：关键词一个没对上的问题，向量候选也送去重排，过门槛就算查到（只开向量时这种问题直接拒答）。"""
    from tests.test_knowledge import FakeEmbedder

    q = "嗯嗯呃呃"
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.setattr(embedding, "make_client", lambda settings: FakeEmbedder(targets={q: "faq-handoff"}))
    fake = rr_on(FakeReranker({"faq-handoff": 0.9}))
    embedding.reset()
    assert knowledge.warm_embeddings(block=True)
    r = knowledge.search(q)
    assert r.mode == "rerank" and _ids(r) == ["faq-handoff"] and r.similarity == 1.0
    (_, docs), = fake.calls
    assert len(docs) == knowledge.RERANK_POOL
    monkeypatch.setenv("RERANK_PROVIDER", "off")
    get_settings.cache_clear()
    assert not knowledge.search(q).hits


def test_kb_tool_audits_rerank_fallback(client, rr_on):
    rr_on(FakeReranker(fail=RuntimeError(f"401 {KEY_SF}")))
    r = client.post("/api/chat", json={"text": QUERY}).json()
    assert any(c["type"] == "knowledge" for c in r["cards"])
    events = client.get(f"/admin/audit?trace_id={r['trace_id']}").json()
    assert [e["payload"] for e in events if e["event"] == "kb_rerank_fallback"] == [{"reason": "RuntimeError"}]
    assert KEY_SF not in json.dumps(events, ensure_ascii=False)


# ---------- 设置页 ----------

class SetupFake(FakeReranker):
    """测试连接送的是两条自拟条文（不在知识库里），第二条才相关。"""

    def rerank(self, query, docs, timeout):
        if len(docs) == 2 and "存款保险" in docs[0]:
            return [0.1, 0.9]
        return super().rerank(query, docs, timeout)


@pytest.fixture
def env_file(client, tmp_path, monkeypatch):
    """临时 .env（绝不碰开发者本机的那份），去掉会盖住 .env 的环境变量，重排客户端换成假的。"""
    path = tmp_path / ".env"
    path.write_text("# 说明注释\nLLM_PROVIDER=deepseek\nAPP_SECRET=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    for k in ("RERANK_PROVIDER", "RERANK_API_KEY", "RERANK_MODEL", "RERANK_BASE_URL", "RERANK_MIN_SCORE",
              "SILICONFLOW_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY", "EMBEDDING_PROVIDER", "ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    fake = SetupFake({"faq-transfer-interbank": 0.9})
    monkeypatch.setattr(rerank, "make_client", lambda settings: fake)
    get_settings.cache_clear()
    rerank.reset()
    yield path
    get_settings.cache_clear()
    rerank.reset()


def test_setup_default_siliconflow_waits_for_key(client, env_file):
    c = client.get("/admin/rerank-config").json()  # 默认选硅基流动，没有密钥时不重排
    assert c["provider"] == "siliconflow" and not c["ready"] and c["state"] == "off" and c["state_text"] == "不重排"
    assert [p["id"] for p in c["providers"]] == ["siliconflow", "qwen"] and not any(p["has_key"] for p in c["providers"])


def test_setup_save_siliconflow_takes_effect(client, env_file):
    r = client.post("/admin/rerank-config", json={"provider": "siliconflow", "api_key": KEY_SF})
    assert r.status_code == 200 and KEY_SF not in r.text  # 密钥不回传
    c = r.json()
    assert c["ready"] and c["state"] == "ready" and c["model"] == "Qwen/Qwen3-Reranker-4B" and c["min_score"] == 0.525
    assert next(p for p in c["providers"] if p["id"] == "siliconflow")["key_tail"] == KEY_SF[-4:]
    text = env_file.read_text(encoding="utf-8")
    assert "RERANK_PROVIDER=siliconflow" in text and f"RERANK_API_KEY={KEY_SF}" in text and "APP_SECRET=keep-me" in text
    assert knowledge.search(QUERY).mode == "rerank"


def test_setup_reuses_siliconflow_key_from_embedding(client, env_file, monkeypatch):
    """向量检索已经填了硅基流动的密钥：重排选硅基流动不用再填。"""
    r = client.post("/admin/rerank-config", json={"provider": "siliconflow"})
    assert r.status_code == 400 and "硅基流动的密钥" in r.json()["detail"]
    monkeypatch.setenv("SILICONFLOW_API_KEY", KEY_SF)
    get_settings.cache_clear()
    c = client.post("/admin/rerank-config", json={"provider": "siliconflow"}).json()
    assert c["ready"] and get_settings().rerank_api_key == KEY_SF


def test_setup_switch_to_qwen_and_change_model(client, env_file, monkeypatch):
    client.post("/admin/rerank-config", json={"provider": "siliconflow", "api_key": KEY_SF, "model": "Qwen/Qwen3-Reranker-0.6B"})
    assert get_settings().rerank_min_score == 0.502
    r = client.post("/admin/rerank-config", json={"provider": "qwen"})
    assert r.status_code == 400 and "千问" in r.json()["detail"]  # 硅基流动的密钥不能拿去调百炼
    c = client.post("/admin/rerank-config", json={"provider": "qwen", "api_key": KEY_DS}).json()
    assert c["ready"] and c["model"] == "qwen3-rerank" and c["base_url"] == "https://dashscope.aliyuncs.com/api/v1"
    assert c["min_score"] == 0.55  # 模型换了，门槛回到新模型的预设
    text = env_file.read_text(encoding="utf-8")
    assert "RERANK_MODEL=\n" in text and "RERANK_MIN_SCORE=\n" in text and f"RERANK_API_KEY={KEY_DS}" in text


@pytest.mark.parametrize("field, bad", [
    ("api_key", "sk-abcdefghijklmnop\nLLM_PROVIDER=glm"), ("model", "x\nADMIN_PASSWORD=1"),
    ("base_url", "https://a.com/v1\nAPP_SECRET=x"), ("provider", "cohere"),
])
def test_setup_rejects_injection(client, env_file, field, bad):
    body = {"provider": "siliconflow", "api_key": KEY_SF, field: bad}
    assert client.post("/admin/rerank-config", json=body).status_code == 400
    assert "RERANK" not in env_file.read_text(encoding="utf-8")


def test_setup_disable_keeps_key_and_reenable(client, env_file):
    client.post("/admin/rerank-config", json={"provider": "siliconflow", "api_key": KEY_SF})
    c = client.post("/admin/rerank-config", json={"disable": True}).json()
    assert c["provider"] == "" and not c["ready"] and c["state"] == "off"
    assert knowledge.search(QUERY).mode == "bm25"
    assert f"RERANK_API_KEY={KEY_SF}" in env_file.read_text(encoding="utf-8")
    assert client.post("/admin/rerank-config", json={"provider": "siliconflow"}).json()["ready"]  # 重新打开不用重填


def test_setup_audit_never_holds_key(client, env_file):
    client.post("/admin/rerank-config", json={"provider": "siliconflow", "api_key": KEY_SF})
    events = client.get("/admin/audit?trace_id=admin").json()
    ev = [e for e in events if e["event"] == "rerank_config"]
    assert ev and ev[-1]["payload"]["key_updated"] is True and KEY_SF not in str(events)


def test_setup_connection_test(client, env_file, monkeypatch):
    assert not client.post("/admin/rerank-config/test").json()["ok"]  # 没配置
    client.post("/admin/rerank-config", json={"provider": "siliconflow", "api_key": KEY_SF})
    r = client.post("/admin/rerank-config/test").json()
    assert r["ok"] and r["sensible"] and r["scores"] == [0.1, 0.9] and r["model"] == "Qwen/Qwen3-Reranker-4B"
    assert "latency_ms" in r
    monkeypatch.setattr(rerank, "make_client", lambda s: FakeReranker(fail=RuntimeError(f"401 invalid key {KEY_SF}")))
    r = client.post("/admin/rerank-config/test").json()
    assert not r["ok"] and KEY_SF not in r["error"] and "***" in r["error"]
