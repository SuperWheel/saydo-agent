"""知识检索（向量）设置页的后端：选向量供应商、填密钥，写进仓库根目录的 .env，保存后立即生效、后台重算条目向量，不用重启。

和模型配置页一样：密钥只进 .env，不回传前端（页面只看到「已填写 · 尾号 ab12」），不写审计，测试连接的报错里也抹掉。
不配或停用时知识问答只用 BM25，行为和没有这个功能一样。
"""

import os
import re
import time

from . import config
from .config import EMBEDDING_PROVIDERS, get_settings
from .knowledge import embedding, rerank, warm_embeddings
from .llm_setup import ConfigError, update_env

LABELS = {"siliconflow": "硅基流动", "qwen": "千问（阿里云百炼）", "zhipu": "智谱", "openai": "其他 OpenAI 兼容接口"}
NOTES = {
    "siliconflow": "推荐。Qwen3-Embedding-8B，取 1024 维；在 cloud.siliconflow.cn 的「API 密钥」页创建",
    "qwen": "text-embedding-v4，取 1024 维；在阿里云百炼控制台创建 API Key（未实测）",
    "zhipu": "embedding-3；不填密钥就复用「模型」页的智谱密钥（需要账户有余额）",
    "openai": "任意兼容 /v1/embeddings 的服务，接口地址、模型名都要填",
}
# 供应商自带的密钥项：切换到它、又没填新密钥时，清掉 EMBEDDING_API_KEY 让它生效
OWN_KEY = {"siliconflow": "siliconflow_api_key", "qwen": "dashscope_api_key", "zhipu": "zhipuai_api_key"}
_KEY_OK = re.compile(r"[A-Za-z0-9._\-]{16,200}")
_MODEL_OK = re.compile(r"[A-Za-z0-9._\-/:]{2,100}")
_URL_OK = re.compile(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9_\-./]*)?")
_WATCHED = ("EMBEDDING_PROVIDER", "EMBEDDING_API_KEY", "SILICONFLOW_API_KEY", "DASHSCOPE_API_KEY", "EMBEDDING_MODEL",
            "EMBEDDING_BASE_URL", "EMBEDDING_DIMENSIONS")
_MARK = "# 以下由知识检索设置页写入"

STATE_TEXT = {
    "off": "只用关键词检索（BM25）",
    "idle": "已配置，条目向量还没开始算",
    "building": "正在计算知识库条目向量…",
    "ready": "向量 + 关键词混合检索",
    "failed": "向量接口出错，暂时只用关键词检索",
}


def _tail(key: str) -> str:
    return key[-4:] if len(key) >= 8 else ""


def status() -> dict:
    s = get_settings()
    providers = []
    for pid, preset in EMBEDDING_PROVIDERS.items():
        # 当前供应商看实际在用的密钥；停用状态下保存会沿用停用前的密钥；别家看切过去不填密钥时能用的那一项
        if pid == s.embedding_provider or (not s.embedding_provider and s.embedding_api_key):
            key = s.embedding_api_key
        else:
            key = getattr(s, OWN_KEY[pid]) if pid in OWN_KEY else ""
        providers.append({"id": pid, "label": LABELS[pid], "note": NOTES[pid], "model": preset.get("embedding_model") or "",
                          "base_url": preset.get("embedding_base_url") or "", "has_key": bool(key),
                          "key_tail": _tail(key)})
    warnings = [f"进程环境变量 {k} 优先于 .env，这里保存后对它不生效" for k in _WATCHED if os.environ.get(k)]
    rt = embedding.runtime_status()
    return {"provider": s.embedding_provider, "model": s.embedding_model or "", "base_url": s.embedding_base_url or "",
            "dimensions": s.embedding_dimensions, "ready": s.embedding_ready, "providers": providers,
            **rt, "state_text": STATE_TEXT.get(rt["state"], rt["state"]),
            "env_file": str(config.ENV_FILE), "warnings": warnings}


def save(provider: str = "", api_key: str = "", model: str = "", base_url: str = "", disable: bool = False) -> dict:
    """写入 .env、重新加载配置并在后台重算条目向量。api_key / model / base_url 留空表示沿用（或用预设）。
    disable=True 只把 EMBEDDING_PROVIDER 清空（回到纯 BM25），密钥保留，下次打开不用重填。"""
    if disable:
        update_env(config.ENV_FILE, {"EMBEDDING_PROVIDER": ""}, _MARK)
        return _reload()
    provider = provider.strip().lower()
    if provider not in EMBEDDING_PROVIDERS:
        raise ConfigError(f"向量供应商只能是 {' / '.join(LABELS.values())}")
    api_key, model, base_url = api_key.strip(), model.strip(), base_url.strip().rstrip("/")
    if api_key and not _KEY_OK.fullmatch(api_key):
        raise ConfigError("密钥格式不对：应为 16–200 位字母、数字或 . _ -，不能有空格和换行")
    if model and not _MODEL_OK.fullmatch(model):
        raise ConfigError("模型名只能有字母、数字和 . _ - / :，如 Qwen/Qwen3-Embedding-8B")
    if base_url and not _URL_OK.fullmatch(base_url):
        raise ConfigError("接口地址应形如 https://api.example.com/v1")

    s = get_settings()
    preset = EMBEDDING_PROVIDERS[provider]
    switching = provider != s.embedding_provider
    updates = {"EMBEDDING_PROVIDER": provider}
    if api_key:
        updates["EMBEDDING_API_KEY"] = api_key
    elif switching and s.embedding_provider == "" and s.embedding_api_key:
        pass  # 停用后重新打开：沿用停用前填的密钥
    elif switching:  # 换了供应商又没填新密钥：只能用它自带的那一项，旧的通用密钥是给别家的
        if not (provider in OWN_KEY and getattr(s, OWN_KEY[provider])):
            raise ConfigError(f"还没有{LABELS[provider]}的密钥，请先填写")
        updates["EMBEDDING_API_KEY"] = ""
    elif not s.embedding_api_key:
        raise ConfigError(f"还没有{LABELS[provider]}的密钥，请先填写")
    # 模型和地址：填了就写；没填且换了供应商，清掉旧值回到新供应商的预设
    if model or switching:
        updates["EMBEDDING_MODEL"] = model
    if base_url or switching:
        updates["EMBEDDING_BASE_URL"] = base_url
    if switching or model:
        updates["EMBEDDING_DIMENSIONS"] = ""  # 维度跟着预设走（Qwen3 取 1024，别的模型不传）
    if not (model or preset.get("embedding_model") or (not switching and s.embedding_model)):
        raise ConfigError("请填写模型名")
    if not (base_url or preset.get("embedding_base_url") or (not switching and s.embedding_base_url)):
        raise ConfigError("请填写接口地址")
    update_env(config.ENV_FILE, updates, _MARK)
    return _reload()


def reload_settings() -> None:
    """改了 .env 之后：重新读配置，丢掉向量和重排的内存状态，后台重新加载条目向量（多半来自落盘缓存）。"""
    get_settings.cache_clear()
    embedding.reset()
    rerank.reset()
    warm_embeddings()  # 没配置时什么都不做；配了在后台算，页面轮询状态


def _reload() -> dict:
    reload_settings()
    return status()


def test_connection() -> dict:
    """用当前配置算一句话的向量，看密钥、模型和网络是否可用。"""
    s = get_settings()
    if not s.embedding_ready:
        return {"ok": False, "error": "还没有配置向量检索，现在只用关键词检索"}
    started = time.monotonic()
    try:
        vecs = embedding.make_client(s).embed(["跨行转账多久到账"], 10.0)
    except Exception as e:  # noqa: BLE001  报错原样（抹掉密钥）给页面看
        return {"ok": False, "error": embedding.describe(e, s.embedding_api_key)}
    return {"ok": True, "provider": s.embedding_provider, "model": s.embedding_model,
            "dimensions": len(vecs[0]) if vecs else 0, "latency_ms": round((time.monotonic() - started) * 1000)}
