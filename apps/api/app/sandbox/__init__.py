"""代码沙箱（F-BA-06 自由分析，技术文档 §5.6、S-05）。本机没有 Docker，用子进程沙箱，三道关：

1. checker.py   AST 白名单：语法节点、模块、属性名、内置函数都只放行列出来的；
2. executor.py  一次性子进程：CPU / 内存 / 墙钟限制，空环境变量，临时空目录，禁写文件、禁子进程；
   runner.py    子进程里：受限内置函数表、只读快照、只放行白名单模块的 import、审计钩子拦截文件 / 网络 / 子进程；
3. 这里         输出契约：只接受 {title, rows: [{label, amount_fen | count | value, unit}]}，超大或格式不对就作废。

快照只含脱敏后的结构化字段（日期、类型、分类、可信商户标签、整数分金额），没有备注原文、商户原文、账号。
隔离强度如实说明：子进程沙箱不等于容器隔离，生产环境应换成 gVisor / microVM（技术文档 §3、R11）。
"""

import hashlib
import math
import re
from dataclasses import dataclass, field

from ..analytics.engine import Txn
from . import checker, executor

MAX_ROWS = 40
MAX_LABEL = 24
MAX_TITLE = 30
MAX_ABS_FEN = 10**12
MAX_ABS_VALUE = 10**9
UNITS = ("", "%", "笔", "次", "天", "倍")
_ROW_KEYS = {"label", "amount_fen", "count", "value", "unit"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁯﻿]")

STATUS_TEXT = {
    "rejected": "代码没有通过安全校验", "timeout": "运行超时", "cpu_limit": "CPU 时间超限", "memory": "内存超限",
    "recursion": "递归太深", "blocked": "触发了禁止的操作", "runtime": "运行出错", "syntax": "语法错误",
    "no_result": "没有给 result 赋值", "bad_output": "结果格式不对", "output_too_large": "结果太大",
    "crashed": "沙箱进程异常退出",
}

# 给模型看的数据说明（写进工具描述），字段和 snapshot() 保持一致
SCHEMA_DOC = (
    "变量 txns 是只读的交易列表，每笔是字典：date「2026-09-14」、time「12:30」、month「2026-09」、"
    "weekday 0–6（0 是周一，5、6 是周末）、hour 0–23、kind「消费 / 转账 / 收入」、"
    "category（餐饮、购物、交通、居住、生活缴费、医疗、教育、娱乐、旅行、订阅、其他；转账是「转账」，收入是「工资 / 转入」）、"
    "merchant（登记商户的规范名，如美团外卖、星巴克、瑞幸咖啡；未登记的是「未登记商户」；转账是脱敏收款人）、"
    "amount_fen 金额，整数分，恒为正数、direction「in / out」、auto 是否自动扣费、cross_border 是否境外。"
)
CONTRACT_DOC = (
    "代码只能 import statistics、math、datetime、collections、itertools，不能用 open、eval、exec、getattr、print、"
    "try、class、双下划线名字或属性。把结果赋给变量 result："
    "{\"title\": \"不超过30字的标题\", \"rows\": [{\"label\": \"工作日\", \"amount_fen\": 12345, \"count\": 20}, …]}。"
    "每行 label 必填（不超过24字）；amount_fen（整数分）、count（整数）、value（其他数值，如均值、占比，可带 unit：% 笔 次 天 倍）至少一个；"
    "最多40行。金额一律用分做整数运算，不要换算成元；差值、比例这类回复里要说的数也在代码里算好，放进 rows。"
)


def snapshot(txns: list[Txn]) -> list[dict]:
    """脱敏快照：只带结构化字段和可信标签。不含交易号、备注、商户原文、展示名、账号、收款人 ID。"""
    return [{
        "date": f"{t.at:%Y-%m-%d}", "time": f"{t.at:%H:%M}", "month": f"{t.at:%Y-%m}", "weekday": t.at.weekday(),
        "hour": t.at.hour, "kind": t.kind, "category": t.category, "merchant": t.party, "amount_fen": t.amount_fen,
        "direction": t.direction, "auto": t.auto, "cross_border": t.cross_border,
    } for t in txns]


class OutputError(ValueError):
    pass


def _label(v, what: str, limit: int) -> str:
    if not isinstance(v, str):
        raise OutputError(f"{what} 必须是字符串")
    s = _CONTROL.sub("", v).strip()
    if not s or len(s) > limit:
        raise OutputError(f"{what} 长度要在 1–{limit} 字之间")
    return s


def _int(v, what: str, limit: int, *, signed: bool = True) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise OutputError(f"{what} 必须是整数")
    if abs(v) > limit or (not signed and v < 0):
        raise OutputError(f"{what} 超出范围")
    return v


def validate_output(obj) -> tuple[str, list[dict]]:
    """输出契约。任何一处不符合就整份作废，不做部分采纳。"""
    if not isinstance(obj, dict) or set(obj) - {"title", "rows"} or "rows" not in obj:
        raise OutputError("result 必须是只含 title 和 rows 的字典")
    title = _label(obj.get("title") or "分析结果", "title", MAX_TITLE)
    rows = obj["rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise OutputError(f"rows 必须是 1–{MAX_ROWS} 行的列表")
    out = []
    for r in rows:
        if not isinstance(r, dict) or set(r) - _ROW_KEYS:
            raise OutputError(f"每行只能有 {'、'.join(sorted(_ROW_KEYS))} 这些字段")
        row = {"label": _label(r.get("label"), "label", MAX_LABEL)}
        if r.get("amount_fen") is not None:
            row["amount_fen"] = _int(r["amount_fen"], "amount_fen", MAX_ABS_FEN)
        if r.get("count") is not None:
            row["count"] = _int(r["count"], "count", 10**7, signed=False)
        if r.get("value") is not None:
            v = r["value"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > MAX_ABS_VALUE:
                raise OutputError("value 必须是有限的数值")
            row["value"] = round(v, 2) if isinstance(v, float) else v
            unit = r.get("unit") or ""
            if unit not in UNITS:
                raise OutputError(f"unit 只能是 {' '.join(u for u in UNITS if u)}")
            if unit:
                row["unit"] = unit
        elif r.get("unit"):
            raise OutputError("unit 只能跟 value 一起用")
        if len(row) == 1:
            raise OutputError("每行至少要有 amount_fen、count、value 中的一个")
        out.append(row)
    return title, out


@dataclass
class Analysis:
    status: str  # ok，或 STATUS_TEXT 里的一种
    code_sha256: str
    verdict: checker.Verdict
    run: executor.RunOutcome | None = None
    title: str = ""
    rows: list[dict] = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def escape(self) -> bool:
        """有逃逸意图：静态检查认出的危险写法，或运行时被审计钩子 / 文件限制拦下。"""
        return self.verdict.escape or (self.run is not None and self.run.status == "blocked")

    @property
    def reason(self) -> str:
        return STATUS_TEXT.get(self.status, self.status)


def analyze(code: str, txns: list[dict], limits: executor.Limits = executor.Limits()) -> Analysis:
    digest = hashlib.sha256((code or "").encode()).hexdigest()
    verdict = checker.check(code)
    if not verdict.ok:
        return Analysis("rejected", digest, verdict, detail="；".join(verdict.reasons))
    run = executor.run(code, txns, limits)
    if run.status != "ok":
        return Analysis(run.status, digest, verdict, run, detail=run.detail)
    try:
        title, rows = validate_output(run.result)
    except OutputError as e:
        return Analysis("bad_output", digest, verdict, run, detail=str(e))
    return Analysis("ok", digest, verdict, run, title=title, rows=rows)
