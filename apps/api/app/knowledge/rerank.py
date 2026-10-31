"""知识库检索的重排模型（可选）：把关键词（和向量）检索的前 10 条候选连同问题送给重排模型，逐条打相关性分（0–1），
按分数重新排序，并用最高分决定「查到」还是拒答（search() 里的 _reranked）。

没配 RERANK_PROVIDER（或没有密钥）时这里什么都不做，检索和原来一样。
- 两种接口格式：硅基流动 POST {base}/rerank（扁平参数，返回 results）；阿里云百炼（千问）原生
  POST {base}/services/rerank/text-rerank/text-rerank（input / parameters，返回 output.results）。
  百炼的 OpenAI 兼容地址（…/compatible-api/v1）走 {base}/reranks，参数和硅基流动一样扁平。
- 问题先 mask_pii 脱敏再发出去，候选条文是公开的知识库内容。
- 超时（RERANK_TIMEOUT_S，默认 3 秒）或任何报错都静默回到不重排的检索，记一条日志（kb.search 工具另写一条审计），
  之后冷却一会儿（报错 60 秒、超时 10 秒）不再调。同一问题、同一批候选的分数在内存里缓存。
"""

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..config import Settings, get_settings
from ..security import mask_pii
from .embedding import describe

log = logging.getLogger("saydo.kb")

COOLDOWN_S = 60.0  # 出错（鉴权失败、额度不足、网络断开……）后这么久内不再调接口
TIMEOUT_COOLDOWN_S = 10.0  # 只是这一问超时：短暂停一下就再试
CACHE = 256  # 「问题 + 候选」分数的内存缓存条数
_NATIVE = "/services/rerank/text-rerank/text-rerank"


class RerankError(Exception):
    pass


def endpoint(settings: Settings) -> tuple[str, bool]:
    """(请求地址, 是否百炼原生格式)。RERANK_BASE_URL 直接写到 /rerank、/reranks、/text-rerank 结尾的完整地址也行。"""
    base = (settings.rerank_base_url or "").rstrip("/")
    if base.endswith(("/rerank", "/reranks")):
        return base, False
    if base.endswith("/text-rerank"):
        return base, True
    if settings.rerank_format == "dashscope":
        return (f"{base}/reranks", False) if "compatible-api" in base else (base + _NATIVE, True)
    return f"{base}/rerank", False


class Client:
    """测试里换成假客户端（make_client）。"""

    def __init__(self, settings: Settings):
        self.model = settings.rerank_model
        self.url, self.native = endpoint(settings)
        self._http = httpx.Client(headers={"Authorization": f"Bearer {settings.rerank_api_key}"})

    def body(self, query: str, docs: list[str]) -> dict:
        if self.native:
            return {"model": self.model, "input": {"query": query, "documents": docs},
                    "parameters": {"top_n": len(docs), "return_documents": False}}
        return {"model": self.model, "query": query, "documents": docs, "top_n": len(docs), "return_documents": False}

    def rerank(self, query: str, docs: list[str], timeout: float) -> list[float]:
        resp = self._http.post(self.url, json=self.body(query, docs), timeout=timeout)
        if resp.status_code != 200:
            raise RerankError(f"HTTP {resp.status_code}：{resp.text[:200]}")
        return parse(resp.json(), len(docs))


def parse(data: dict, n: int) -> list[float]:
    """两种返回格式都认：results[{index, relevance_score}] 或 output.results[...]。没返回的候选记 0 分。"""
    results = data.get("results")
    if results is None:
        results = (data.get("output") or {}).get("results")
    if not isinstance(results, list):
        raise RerankError(f"返回里没有 results：{str(data)[:200]}")
    scores = [0.0] * n
    for r in results:
        i = r.get("index")
        if not isinstance(i, int) or not 0 <= i < n:
            raise RerankError(f"返回的 index 越界：{i}")
        scores[i] = float(r.get("relevance_score", 0.0))
    return scores


def make_client(settings: Settings) -> Client:
    return Client(settings)


class _State:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None
        self.last_error = ""
        if settings.rerank_ready:
            try:
                self.client = make_client(settings)
            except Exception as e:  # noqa: BLE001  建不起客户端也只是不重排
                self.last_error = f"创建客户端：{describe(e, settings.rerank_api_key)}"
                log.warning("知识库重排不可用（%s），不重排", self.last_error)
        self.failed_until = 0.0
        self.cache: OrderedDict[tuple, list[float]] = OrderedDict()
        self.lock = threading.Lock()


_state: _State | None = None
_state_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kb-rerank")


def _get() -> _State:
    global _state
    s = get_settings()
    with _state_lock:
        if _state is None or _state.settings is not s:  # 改了 .env、get_settings.cache_clear() 之后重建
            _state = _State(s)
        return _state


def enabled() -> bool:
    return get_settings().rerank_ready


def min_score() -> float:
    return get_settings().rerank_min_score or 0.0


def reset() -> None:
    """丢掉内存里的缓存和冷却状态（测试、设置页保存后用）。"""
    global _state
    with _state_lock:
        _state = None


def _is_timeout(err: BaseException) -> bool:
    return isinstance(err, (TimeoutError, httpx.TimeoutException))  # 前者是我们的硬时限，后者是 httpx 自己的超时


def last_error_reason(err: BaseException) -> str:
    return "timeout" if _is_timeout(err) else type(err).__name__


def _fail(st: _State, err: BaseException) -> None:
    cooldown = TIMEOUT_COOLDOWN_S if _is_timeout(err) else COOLDOWN_S
    st.failed_until = time.monotonic() + cooldown
    reason = describe(err, st.settings.rerank_api_key)
    st.last_error = reason
    log.warning("知识库重排不可用，这一问不重排，%d 秒内不再调用：%s", int(cooldown), reason)


def runtime_status() -> dict:
    """设置页用：off 没配置 / ready 可用 / failed 出错冷却中（或客户端建不起来）。"""
    st = _get()
    if st.client is None:
        return {"state": "failed" if st.last_error else "off", "last_error": st.last_error, "retry_in_s": 0}
    left = st.failed_until - time.monotonic()
    if left > 0:
        return {"state": "failed", "last_error": st.last_error, "retry_in_s": max(0, round(left))}
    return {"state": "ready", "last_error": st.last_error, "retry_in_s": 0}


def scores(query: str, docs: list[str]) -> tuple[list[float] | None, str]:
    """(每条候选的相关性分, 失败原因)。没配置、冷却中返回 (None, "")；这一次超时或出错返回 (None, 原因)。"""
    st = _get()
    if st.client is None or time.monotonic() < st.failed_until or not docs:
        return None, ""
    masked = mask_pii(query)
    key = (masked, tuple(docs))
    with st.lock:
        if (v := st.cache.get(key)) is not None:
            st.cache.move_to_end(key)
            return v, ""
    timeout = st.settings.rerank_timeout_s
    try:
        out = _pool.submit(st.client.rerank, masked, list(docs), timeout).result(timeout=timeout)
        if len(out) != len(docs):
            raise RerankError(f"返回 {len(out)} 个分数，送了 {len(docs)} 条")
    except Exception as e:  # noqa: BLE001  含超时：这一问不重排
        _fail(st, e)
        return None, last_error_reason(e)
    with st.lock:
        st.last_error = ""
        st.cache[key] = out
        while len(st.cache) > CACHE:
            st.cache.popitem(last=False)
    return out, ""
