"""知识检索（重排）设置页的后端：选重排供应商、填密钥，写进仓库根目录的 .env，保存后立即生效，不用重启。

和向量设置页一样：密钥只进 .env，不回传前端（页面只看到「已填写 · 尾号 ab12」），不写审计，测试连接的报错里也抹掉。
重排是可选的：不配或停用时知识问答不重排，行为和没有这个功能一样；只配重排、不配向量也能用（候选来自关键词检索）。
"""

import os
import time

from . import config
from .config import RERANK_PROVIDERS, get_settings
from .embedding_setup import _KEY_OK, _MODEL_OK, _URL_OK, _tail, reload_settings
from .knowledge import embedding, rerank
from .llm_setup import ConfigError, update_env

LABELS = {"siliconflow": "硅基流动", "qwen": "千问（阿里云百炼）"}
NOTES = {
    "siliconflow": "推荐。Qwen3-Reranker-8B；不填密钥就复用硅基流动的密钥（和向量检索同一个）",
    "qwen": "qwen3-rerank，百炼原生 text-rerank 接口；不填密钥就用 DASHSCOPE_API_KEY（未实测，门槛未校准）",
}
OWN_KEY = {"siliconflow": "siliconflow_api_key", "qwen": "dashscope_api_key"}
_WATCHED = ("RERANK_PROVIDER", "RERANK_API_KEY", "SILICONFLOW_API_KEY", "DASHSCOPE_API_KEY", "RERANK_MODEL",
            "RERANK_BASE_URL", "RERANK_MIN_SCORE")
_MARK = "# 以下由知识检索设置页写入"

STATE_TEXT = {
    "off": "不重排",
    "ready": "已启用重排",
    "failed": "重排接口出错，暂时不重排",
}


def status() -> dict:
    s = get_settings()
    providers = []
    for pid, preset in RERANK_PROVIDERS.items():
        # 当前供应商看实际在用的密钥；停用状态下保存会沿用停用前的密钥；别家看切过去不填密钥时能用的那一项
        if pid == s.rerank_provider or (not s.rerank_provider and s.rerank_api_key):
            key = s.rerank_api_key
        else:
            key = getattr(s, OWN_KEY[pid])
        providers.append({"id": pid, "label": LABELS[pid], "note": NOTES[pid], "model": preset["rerank_model"],
                          "base_url": preset["rerank_base_url"], "has_key": bool(key), "key_tail": _tail(key)})
    warnings = [f"进程环境变量 {k} 优先于 .env，这里保存后对它不生效" for k in _WATCHED if os.environ.get(k)]
    rt = rerank.runtime_status()
    return {"provider": s.rerank_provider, "model": s.rerank_model or "", "base_url": s.rerank_base_url or "",
            "min_score": s.rerank_min_score, "timeout_s": s.rerank_timeout_s, "ready": s.rerank_ready,
            "with_embedding": s.embedding_ready, "providers": providers,
            **rt, "state_text": STATE_TEXT.get(rt["state"], rt["state"]),
            "env_file": str(config.ENV_FILE), "warnings": warnings}


def save(provider: str = "", api_key: str = "", model: str = "", base_url: str = "", disable: bool = False) -> dict:
    """写入 .env 并重新加载配置。api_key / model / base_url 留空表示沿用（或用预设）。
    disable=True 只把 RERANK_PROVIDER 清空（不重排），密钥保留，下次打开不用重填。"""
    if disable:
        update_env(config.ENV_FILE, {"RERANK_PROVIDER": ""}, _MARK)
        return _reload()
    provider = provider.strip().lower()
    if provider not in RERANK_PROVIDERS:
        raise ConfigError(f"重排供应商只能是 {' / '.join(LABELS.values())}")
    api_key, model, base_url = api_key.strip(), model.strip(), base_url.strip().rstrip("/")
    if api_key and not _KEY_OK.fullmatch(api_key):
        raise ConfigError("密钥格式不对：应为 16–200 位字母、数字或 . _ -，不能有空格和换行")
    if model and not _MODEL_OK.fullmatch(model):
        raise ConfigError("模型名只能有字母、数字和 . _ - / :，如 Qwen/Qwen3-Reranker-8B")
    if base_url and not _URL_OK.fullmatch(base_url):
        raise ConfigError("接口地址应形如 https://api.example.com/v1")

    s = get_settings()
    switching = provider != s.rerank_provider
    updates = {"RERANK_PROVIDER": provider}
    if api_key:
        updates["RERANK_API_KEY"] = api_key
    elif switching and s.rerank_provider == "" and s.rerank_api_key:
        pass  # 停用后重新打开：沿用停用前填的密钥
    elif switching:  # 换了供应商又没填新密钥：只能用它自带的那一项，旧的通用密钥是给别家的
        if not getattr(s, OWN_KEY[provider]):
            raise ConfigError(f"还没有{LABELS[provider]}的密钥，请先填写")
        updates["RERANK_API_KEY"] = ""
    elif not s.rerank_api_key:
        raise ConfigError(f"还没有{LABELS[provider]}的密钥，请先填写")
    # 模型和地址：填了就写；没填且换了供应商，清掉旧值回到新供应商的预设。门槛跟着模型的预设走
    if model or switching:
        updates["RERANK_MODEL"] = model
        updates["RERANK_MIN_SCORE"] = ""
    if base_url or switching:
        updates["RERANK_BASE_URL"] = base_url
    update_env(config.ENV_FILE, updates, _MARK)
    return _reload()


def _reload() -> dict:
    reload_settings()
    return status()


def test_connection() -> dict:
    """用当前配置给两条条文打分，看密钥、模型和网络是否可用、排序是否合理。"""
    s = get_settings()
    if not s.rerank_ready:
        return {"ok": False, "error": "还没有配置重排模型，现在不重排"}
    docs = ["存款保险：同一存款人在同一家银行的存款，最高偿付限额为人民币 50 万元。",
            "跨行转账多久到账：实时到账一般几秒钟到账，普通到账 2 小时内，次日到账下一个自然日。"]
    started = time.monotonic()
    try:
        scores = rerank.make_client(s).rerank("转到别的银行的卡要多久能到", docs, 10.0)
    except Exception as e:  # noqa: BLE001  报错原样（抹掉密钥）给页面看
        return {"ok": False, "error": embedding.describe(e, s.rerank_api_key)}
    return {"ok": True, "provider": s.rerank_provider, "model": s.rerank_model,
            "scores": [round(x, 3) for x in scores], "sensible": len(scores) == 2 and scores[1] > scores[0],
            "latency_ms": round((time.monotonic() - started) * 1000)}
