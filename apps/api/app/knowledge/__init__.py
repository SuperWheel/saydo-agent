"""知识库（WS9，F-GN-14）：产品说明、规则 FAQ、商户取消指引，以及纯 Python 的关键词检索。

一句话定位：知识库回答「规则是什么」，工具回答「你的账户是什么」（技术文档 §5.9）。
余额、交易、持仓、额度、测评等级只来自工具；产品的风险等级和可售状态只取产品主数据，知识库文本不参与任何判断。

- 规则 FAQ、监管法规要点、商户取消指引是 kb/ 下的 YAML（随代码版本化，入库前双人审核）；产品说明由产品主数据
  core_mock/wealth.PRODUCTS 生成，保证和申购赎回时用的数字一致。
- 每条标明知识类别（kind）：「监管 / 法规通用知识」引用法律和监管文件原文要点，出处带文件名、条款、文号、发布机构、施行日期；
  「本行（演示）业务规则」是比赛原型参数，和代码里的实际参数一致，两者冲突时以代码为准；商户取消路径是团队整理的「一般路径」，未向商户核实。
- 加载时扫描注入特征（security.inspect），命中的条目不入库，记在 REJECTED 里。
- 检索：中文按字的二元组切分，英文和数字按词，BM25 打分；最佳条目的分数和查询词覆盖率都过门槛（或覆盖率很高）才算「查到」，
  否则返回空结果，由上层如实说没查到，不让模型用常识补充。没有向量模型，不联网。
"""

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from ..core_mock.wealth import PRODUCTS, RISK_LABEL, pct
from ..security import inspect

KB_DIR = Path(__file__).parent / "kb"
SOURCE_PREFIX = "演示知识库"
DISCLAIMER = "演示知识库内容（规则为比赛原型参数，产品为虚构产品），仅供参考，以本行正式公告为准"
KIND_REGULATION = "监管 / 法规通用知识"
KIND_BANK = "本行（演示）业务规则"
PRODUCT_LIBRARY = {"library": "product", "name": "理财产品说明（虚构产品）", "kind": KIND_BANK,
                   "version": "kb-2026-10", "effective": "2026-10-01"}

# 「查到」的门槛，用 evals/kb.yaml 校准（站内问题最低约 11 分，超范围问题最高约 8 分）；知识库扩充后要重新跑 scripts/kb_eval.py 校准
MIN_SCORE = 10.0  # 最佳条目的 BM25 分数
MIN_COVERAGE = 0.15  # 查询词（IDF 加权）被最佳条目覆盖的比例；扩库到 100 条后从 0.12 调到 0.15（评测集里命中题最低约 0.18，超范围题最高约 0.12）
SURE_COVERAGE = 0.6  # 短而准的问法（「R2 是什么意思」）分数低但几乎全被覆盖，覆盖率过这个数也算查到；超范围问题最高约 0.3
RELATIVE_CUTOFF = 0.6  # 第二、三条命中的分数至少是第一条的这个比例，否则不带上


@dataclass(frozen=True)
class Entry:
    id: str
    library: str  # faq / product / merchant_cancel
    library_name: str  # 子库名，如「常见问题」
    title: str  # 条款标题
    body: str  # 条文
    version: str
    effective: str  # 生效日期
    q: tuple[str, ...] = ()  # 用户可能的问法，只参与检索
    merchant: str | None = None  # 商户取消指引：对应的登记商户规范名
    where: str = ""
    steps: tuple[str, ...] = ()
    tips: tuple[str, ...] = ()
    kind: str = KIND_BANK  # 知识类别：监管 / 法规通用知识，或本行（演示）业务规则
    basis: tuple[str, ...] = ()  # 依据，如「《存款保险条例》第五条（国务院令第660号，国务院，2015-05-01 施行）」
    urls: tuple[str, ...] = ()  # 依据的官方原文链接，只上卡片（链接里的长数字串发给模型会被当成卡号打码）

    @property
    def source(self) -> str:
        """出处：知识库 · 类别 · 子库 · 条款 · 依据（文件 · 条款 · 文号 · 机构 · 施行日期）· 版本 · 生效日期。"""
        basis = f" · 依据：{'；'.join(self.basis)}" if self.basis else ""
        return f"{SOURCE_PREFIX} · {self.kind} · {self.library_name} · {self.title}{basis} · {self.version} · {self.effective} 生效"

    def view(self) -> dict:
        return {"id": self.id, "title": self.title, "text": self.body, "source": self.source}


@dataclass
class SearchResult:
    hits: list[tuple[Entry, float]] = field(default_factory=list)
    coverage: float = 0.0  # 最佳条目对查询词的覆盖率（0–1）

    @property
    def entries(self) -> list[Entry]:
        return [e for e, _ in self.hits]


# ---------- 数据加载 ----------

def _yuan(fen: int) -> str:
    return f"{fen // 100:,}" if fen % 100 == 0 else f"{fen / 100:,.2f}"


def _product_entries() -> list[Entry]:
    """每个产品两条：产品概要、赎回规则和费用。数字全部来自产品主数据。"""
    lib = PRODUCT_LIBRARY
    out = []
    for code, name, kind, risk, term, settle, min_fen, bench, buy_bp, redeem_bp, redeem_days, _m, aliases, intro in PRODUCTS:
        term_text = (f"封闭期 {term} 天，封闭期内不能提前赎回，到期自动赎回到活期" if term
                     else "开放式净值型，交易日可以随时申购、赎回")
        bench_text = f"业绩比较基准 {pct(bench[0])}–{pct(bench[1])}（不代表实际收益）" if bench else "没有业绩比较基准，收益随净值变化"
        overview = (f"{name}是{kind}产品，风险等级 {RISK_LABEL[risk]}；{term_text}；起购金额 {_yuan(min_fen)} 元；{bench_text}。"
                    f"{intro}。本产品为虚构演示产品，不保本，过往业绩不代表未来表现。")
        arrive = "T+0（当天到账）" if settle == 0 else f"T+{settle}（提交赎回后第 {settle} 个交易日到账）"
        redeem = (f"封闭期 {term} 天内不能提前赎回，到期后自动赎回，资金 {arrive}" if term
                  else f"可以随时申请赎回，资金 {arrive}")
        buy_fee = f"申购费 {pct(buy_bp)}" if buy_bp else "不收申购费"
        redeem_fee = (f"持有不足 {redeem_days} 天赎回收取 {pct(redeem_bp)} 赎回费，持有满 {redeem_days} 天免赎回费"
                      if redeem_bp else "不收赎回费")
        out += [
            Entry(id=f"prod-{code.lower()}", library=lib["library"], library_name=lib["name"], title=f"{name}产品概要",
                  body=overview, version=lib["version"], effective=lib["effective"],
                  q=tuple(f"{a}{s}" for a in aliases for s in ("是什么产品", "风险高吗", "起购金额", "期限多长"))),
            Entry(id=f"prod-{code.lower()}-redeem", library=lib["library"], library_name=lib["name"],
                  title=f"{name}赎回规则和费用", body=f"{redeem}。{buy_fee}，{redeem_fee}。",
                  version=lib["version"], effective=lib["effective"],
                  q=tuple(f"{a}{s}" for a in aliases for s in ("能提前赎回吗", "赎回多久到账", "手续费", "赎回费"))),
        ]
    return out


def _merchant_body(where: str, steps: list[str], tips: list[str]) -> str:
    numbered = "；".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    body = f"一般路径（以{where}实际页面为准）：{numbered}。"
    return body + ("".join(f"{t}。" for t in tips))


def _basis(b: dict) -> str:
    extra = "，".join(x for x in (b.get("docno"), b["org"], f"{b['effective']} 施行") if x)
    return f"《{b['doc']}》{b['ref']}（{extra}）"


def _load_yaml(path: Path) -> list[Entry]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    lib, name, version, effective = doc["library"], doc["name"], doc["version"], str(doc["effective"])
    lib_kind = doc.get("kind", KIND_BANK)
    common = tuple(doc.get("common_tips") or ())
    out = []
    for e in doc["entries"]:
        if lib == "merchant_cancel":
            steps, tips = tuple(e["steps"]), tuple(e.get("tips") or ()) + (common if e.get("merchant") != "中国移动话费" else ())
            out.append(Entry(id=e["id"], library=lib, library_name=name, title=f"取消{e['name']}自动续费（一般路径）",
                             body=_merchant_body(e["where"], list(steps), list(tips)), version=version, effective=effective,
                             q=tuple(e.get("q") or ()), merchant=e.get("merchant"), where=e["where"], steps=steps, tips=tips,
                             kind=lib_kind))
        else:
            basis = e.get("basis") or []
            out.append(Entry(id=e["id"], library=lib, library_name=name, title=e["title"], body=e["body"].strip(),
                             version=version, effective=effective, q=tuple(e.get("q") or ()), kind=e.get("kind", lib_kind),
                             basis=tuple(_basis(b) for b in basis), urls=tuple(b["url"] for b in basis if b.get("url"))))
    return out


REJECTED: list[tuple[str, list[str]]] = []  # 入库扫描命中注入特征、没有入库的条目：(id, flags)


@lru_cache
def entries() -> tuple[Entry, ...]:
    REJECTED.clear()
    out, seen = [], set()
    for e in [*_product_entries(), *(x for p in sorted(KB_DIR.glob("*.yaml")) for x in _load_yaml(p))]:
        if e.id in seen:
            raise ValueError(f"知识库条目编号重复：{e.id}")
        seen.add(e.id)
        flags = inspect(" ".join((e.title, e.body, *e.q, *e.steps, *e.tips))).flags
        if flags:
            REJECTED.append((e.id, flags))
            continue
        out.append(e)
    return tuple(out)


def merchant_entry(merchant: str) -> Entry | None:
    """登记商户规范名 → 商户取消指引条目。"""
    return next((e for e in entries() if e.library == "merchant_cancel" and e.merchant == merchant), None)


# ---------- 切词 ----------

# 同义说法归一，查询和条文都做；长的写在前面
_SYNONYMS = [
    ("多长时间", "多久"), ("什么时候", "多久"), ("啥时候", "多久"), ("几天", "多久"), ("多少天", "多久"),
    ("同一家银行", "本行"), ("同一个银行", "本行"), ("马上到", "实时到账"), ("打错", "转错"),
    ("别的银行", "跨行"), ("其他银行", "跨行"), ("他行", "跨行"), ("外行", "跨行"),
    ("汇款", "转账"), ("转钱", "转账"), ("打钱", "转账"),
    ("要钱", "收费"), ("收钱", "收费"), ("费用", "收费"), ("免费", "收费"),
    ("丢了", "丢失"), ("掉了", "丢失"), ("不见了", "丢失"), ("找不到", "丢失"), ("弄丢", "丢失"),
    ("刷脸", "人脸"), ("提前取出", "提前赎回"), ("提前卖", "提前赎回"), ("取出来", "赎回"),
    ("b站", "哔哩哔哩"), ("苹果", "apple"), ("退订", "取消"), ("关掉", "关闭"), ("停掉", "停止"),
    ("一类", "i类"), ("二类", "ii类"), ("三类", "iii类"),
]
_WEAK = set("么吗呢吧啊嘛的了")  # 带这些字的二元组不算词（「么情」「费吗」「的卡」）
_STOP = set("的了吗呢吧啊么嘛是我你他她它们在和与或就都也还要能会可以怎什哪个这那有没不多少啥请问下一想把被给让得着过帮")
_TOKEN = re.compile(r"[一-鿿]+|[a-z0-9]+(?:\+[0-9]+)?")


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    for a, b in _SYNONYMS:
        t = t.replace(a, b)
    return t


def tokens(text: str) -> list[str]:
    out = []
    for run in _TOKEN.findall(_normalize(text)):
        if not ("一" <= run[0] <= "鿿"):
            out.append(run)
        elif len(run) == 1:
            if run not in _STOP:
                out.append(run)
        else:
            out += [b for b in (run[i:i + 2] for i in range(len(run) - 1))
                    if not (b[0] in _STOP and b[1] in _STOP) and not (_WEAK & set(b))]
    return out


# ---------- BM25 ----------

_K1, _B = 1.2, 0.75


@dataclass
class _Index:
    docs: list[Entry]
    tfs: list[Counter]
    lens: list[int]
    avg: float
    df: Counter

    def idf(self, t: str) -> float:
        n = len(self.docs)
        return math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))


def _doc_tokens(e: Entry) -> list[str]:
    # 标题和问法各算两遍：用户的问法和它们最接近
    return tokens(e.title) * 2 + [t for q in e.q for t in tokens(q)] * 2 + tokens(e.body)


@lru_cache
def _index() -> _Index:
    docs = list(entries())
    tfs = [Counter(_doc_tokens(e)) for e in docs]
    lens = [sum(tf.values()) for tf in tfs]
    df = Counter(t for tf in tfs for t in tf)
    return _Index(docs, tfs, lens, sum(lens) / max(len(lens), 1), df)


def search(query: str, top_k: int = 3) -> SearchResult:
    """返回命中的条目（按分数从高到低，最多 top_k 条）。覆盖率低于 MIN_COVERAGE 时 hits 为空。"""
    idx = _index()
    q = list(dict.fromkeys(tokens(query)))
    if not q or not idx.docs:
        return SearchResult()
    scores = []
    for i, tf in enumerate(idx.tfs):
        s = 0.0
        for t in q:
            if f := tf.get(t):
                s += idx.idf(t) * f * (_K1 + 1) / (f + _K1 * (1 - _B + _B * idx.lens[i] / idx.avg))
        scores.append(s)
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    best = order[0]
    if scores[best] <= 0:
        return SearchResult()
    total = sum(idx.idf(t) for t in q)
    coverage = round(sum(idx.idf(t) for t in q if t in idx.tfs[best]) / total, 3)
    if (scores[best] < MIN_SCORE or coverage < MIN_COVERAGE) and coverage < SURE_COVERAGE:
        return SearchResult(coverage=coverage)
    hits = [(idx.docs[i], round(scores[i], 3)) for i in order[:top_k] if scores[i] >= scores[best] * RELATIVE_CUTOFF]
    return SearchResult(hits=hits, coverage=coverage)
