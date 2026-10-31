"""知识库检索的向量通道（可选）：调 OpenAI 兼容的 /embeddings 接口给条目和查询算向量，供 search() 和 BM25 做混合排序。

没配 EMBEDDING_PROVIDER（或没有密钥）时这里什么都不做，检索和原来一样只走 BM25。
- 条目向量：批量算一次存在内存，按「模型 + 文本」的哈希落盘到 data/kb_embeddings.json（不进仓库），重启和条目没改时不用重算。
  后端启动时在后台线程预热；还没算好时检索照常只走 BM25，不等它。
- 查询向量：先 mask_pii 脱敏再发出去（Qwen3-Embedding 按官方建议在查询前加一句检索指令，条目不加）；超时（EMBEDDING_TIMEOUT_S，默认 2 秒）或任何报错都静默回到 BM25，
  记一条日志（kb.search 工具另写一条审计），之后冷却一会儿（报错 60 秒、超时 10 秒）不再调，免得接口挂了每一问都多等 2 秒。
"""

import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import openai

from ..config import EMBEDDING_PROVIDERS, REPO_ROOT, Settings, get_settings
from ..security import mask_pii

log = logging.getLogger("saydo.kb")

CACHE_FILE = REPO_ROOT / "data" / "kb_embeddings.json"
BATCH = 32  # 一次请求最多送多少条（智谱 embedding-3 单次上限 64 条；千问 text-embedding-v4 是 10 条，见预设里的 batch）
INDEX_TIMEOUT_S = 15.0  # 条目批量算向量每批的时限（在后台跑，不卡检索）
COOLDOWN_S = 60.0  # 出错（鉴权失败、额度不足、网络断开……）后这么久内不再调接口
TIMEOUT_COOLDOWN_S = 10.0  # 只是这一问超时：偶尔慢一下（实测中位 0.3 秒，偶尔 1.6 秒以上），短暂停一下就再试
QUERY_CACHE = 256  # 查询向量的内存缓存条数

Vector = list[float]


class EmbeddingError(Exception):
    pass


class Client:
    """OpenAI 兼容的 /embeddings。测试里换成假客户端（make_client）。"""

    def __init__(self, settings: Settings):
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self._client = openai.OpenAI(api_key=settings.embedding_api_key, base_url=settings.embedding_base_url, max_retries=0)

    def embed(self, texts: list[str], timeout: float) -> list[Vector]:
        extra = {"dimensions": self.dimensions} if self.dimensions else {}
        resp = self._client.embeddings.create(model=self.model, input=texts, timeout=timeout, **extra)
        return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]


def make_client(settings: Settings) -> Client:
    return Client(settings)


def _unit(v: Vector) -> Vector:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: Vector, b: Vector) -> float:
    """两个已归一化向量的余弦相似度。"""
    return sum(x * y for x, y in zip(a, b))


class _State:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None
        self.last_error = ""
        if settings.embedding_ready:
            try:
                self.client = make_client(settings)
            except Exception as e:  # noqa: BLE001  建不起客户端也只是退回 BM25
                self.last_error = f"创建客户端：{describe(e, settings.embedding_api_key)}"
                log.warning("知识库向量检索不可用（%s），只用 BM25", self.last_error)
        # 缓存按模型和维度区分：换了任何一个，落盘的旧向量都不再用
        self.model = f"{settings.embedding_model or ''}@{settings.embedding_dimensions or 'default'}"
        self.instruction = (settings.embedding_query_instruction or "").strip()
        self.docs: tuple[str, ...] = ()
        self.doc_vecs: list[Vector] | None = None
        self.building = False
        self.failed_until = 0.0
        self.queries: OrderedDict[str, Vector] = OrderedDict()
        self.lock = threading.Lock()


_state: _State | None = None
_state_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kb-embed")


def _get() -> _State:
    global _state
    s = get_settings()
    with _state_lock:
        if _state is None or _state.settings is not s:  # 改了 .env、get_settings.cache_clear() 之后重建
            _state = _State(s)
        return _state


def enabled() -> bool:
    return get_settings().embedding_ready


def reset() -> None:
    """丢掉内存里的向量和冷却状态（测试用）。"""
    global _state
    with _state_lock:
        _state = None


def _fail(st: _State, what: str, err: BaseException) -> None:
    cooldown = TIMEOUT_COOLDOWN_S if _is_timeout(err) else COOLDOWN_S
    st.failed_until = time.monotonic() + cooldown
    reason = describe(err, st.settings.embedding_api_key)
    st.last_error = f"{what}：{reason}"
    log.warning("知识库向量检索不可用（%s），回到纯 BM25，%d 秒内不再调用：%s", what, int(cooldown), reason)


def describe(err: BaseException, api_key: str = "") -> str:
    """报错说明：抹掉密钥、脱敏、截短，可以进日志和设置页。"""
    text = str(err).replace(api_key, "***") if api_key else str(err)
    return f"{type(err).__name__}: {mask_pii(text)[:200]}" if text else type(err).__name__


def runtime_status() -> dict:
    """设置页用：off 没配置 / ready 条目向量就绪 / building 正在算 / failed 出错冷却中 / idle 还没开始算。"""
    st = _get()
    if st.client is None:
        return {"state": "failed" if st.last_error else "off", "docs": 0, "last_error": st.last_error, "retry_in_s": 0}
    left = st.failed_until - time.monotonic()
    state = ("ready" if st.doc_vecs is not None else "building" if st.building
             else "failed" if left > 0 else "idle")
    return {"state": state, "docs": len(st.doc_vecs or ()), "last_error": st.last_error,
            "retry_in_s": max(0, round(left)) if state == "failed" else 0}


def _is_timeout(err: BaseException) -> bool:
    return isinstance(err, (TimeoutError, openai.APITimeoutError))  # 前者是我们的 2 秒时限，后者是 SDK 自己的读超时


def last_error_reason(err: BaseException) -> str:
    return "timeout" if _is_timeout(err) else type(err).__name__


# ---------- 条目向量（批量、落盘缓存） ----------

def _key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()


def _load_cache(path: Path, model: str, dims: int | None) -> dict[str, Vector]:
    """落盘的条目向量。模型键不同、各条长度不一、或和配置的维度不符（被别处写坏了）都当没有缓存，重新算。"""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("model") != model:
            return {}
        vectors = doc["vectors"]
        lens = {len(v) for v in vectors.values()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    if len(lens) > 1 or (dims and lens and lens != {dims}):
        log.warning("知识库向量缓存 %s 的维度不对（%s，应为 %s），不用它、重新算", path, sorted(lens), dims or "各条一致")
        return {}
    return vectors


def _save_cache(path: Path, model: str, vectors: dict[str, Vector]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"model": model, "vectors": vectors}), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:  # 只读目录之类：内存里照样能用
        log.warning("知识库向量缓存写不进 %s：%s", path, e)


def _build(st: _State, texts: tuple[str, ...], path: Path) -> None:
    """算条目向量。缓存路径由发起方定好传进来：后台线程跑完时模块里的 CACHE_FILE 可能已经变了（测试夹具还原），
    不能那时再去读它，否则会写到别处（曾经把测试的假向量写进了开发者本机的 data/kb_embeddings.json）。"""
    try:
        cached = _load_cache(path, st.model, st.settings.embedding_dimensions)
        keys = [_key(st.model, t) for t in texts]
        todo = [i for i, k in enumerate(keys) if k not in cached]
        batch = EMBEDDING_PROVIDERS.get(st.settings.embedding_provider, {}).get("batch", BATCH)
        for start in range(0, len(todo), batch):
            part = todo[start:start + batch]
            vecs = st.client.embed([texts[i] for i in part], INDEX_TIMEOUT_S)
            if len(vecs) != len(part):
                raise EmbeddingError(f"返回 {len(vecs)} 条向量，送了 {len(part)} 条")
            for i, v in zip(part, vecs):
                cached[keys[i]] = v
        if todo:
            _save_cache(path, st.model, {k: cached[k] for k in keys})  # 只留当前条目的，改过的旧条目顺手清掉
        with st.lock:
            st.docs, st.doc_vecs = texts, [_unit(cached[k]) for k in keys]
            st.last_error = ""
        log.info("知识库向量就绪：%d 条（新算 %d 条，模型 %s）", len(texts), len(todo), st.model)
    except Exception as e:  # noqa: BLE001  任何错误都只是退回 BM25
        _fail(st, "条目向量", e)
    finally:
        st.building = False


def doc_vectors(texts: tuple[str, ...], block: bool = False) -> list[Vector] | None:
    """条目向量（和 texts 一一对应）；还没算好返回 None，并在后台开始算。block=True 时就地算完（评测脚本、预热）。"""
    st = _get()
    if st.client is None:
        return None
    with st.lock:
        if st.doc_vecs is not None and st.docs == texts:
            return st.doc_vecs
        if st.building or time.monotonic() < st.failed_until:
            return None
        st.building = True
    if block:
        _build(st, texts, CACHE_FILE)
        return st.doc_vecs if st.docs == texts else None
    threading.Thread(target=_build, args=(st, texts, CACHE_FILE), name="kb-embed-index", daemon=True).start()
    return None


# ---------- 查询向量 ----------

def query_vector(text: str) -> tuple[Vector | None, str]:
    """(查询向量, 失败原因)。没配置、冷却中返回 (None, "")；这一次超时或出错返回 (None, 原因)。"""
    st = _get()
    if st.client is None or time.monotonic() < st.failed_until:
        return None, ""
    masked = mask_pii(text)
    with st.lock:
        if (v := st.queries.get(masked)) is not None:
            st.queries.move_to_end(masked)
            return v, ""
    timeout = st.settings.embedding_timeout_s
    try:
        vecs = _pool.submit(st.client.embed, [st.instruction + masked], timeout).result(timeout=timeout)
        if len(vecs) != 1:
            raise EmbeddingError(f"返回 {len(vecs)} 条向量，送了 1 条")
        v = _unit(vecs[0])
    except Exception as e:  # noqa: BLE001  含超时：这一问只用 BM25
        _fail(st, "查询向量", e)
        return None, last_error_reason(e)
    with st.lock:
        st.queries[masked] = v
        while len(st.queries) > QUERY_CACHE:
            st.queries.popitem(last=False)
    return v, ""
