"""第一道关：AST 白名单校验。模型生成的分析代码先在这里过一遍，不通过的根本不会启动子进程。

全部按白名单判断：语法节点、模块、属性名、内置函数都只放行列出来的；名单外的一律拒绝。
运行阶段（runner.py）还会再挡一次：受限的内置函数表、只放行白名单模块的 __import__、审计钩子、资源限制。
"""

import ast
import re
from dataclasses import dataclass, field

MAX_CODE_CHARS = 4000
MAX_NODES = 2500
MAX_STR_CONST = 200

ALLOWED_MODULES = frozenset({"statistics", "math", "datetime", "collections", "itertools"})

# 运行时提供的内置函数，和 runner.SAFE_BUILTINS 保持一致
ALLOWED_BUILTINS = frozenset({
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "frozenset", "int", "isinstance",
    "len", "list", "map", "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip",
})

# 点出来的名字只能是这些：容器 / 字符串 / 日期的常用方法，以及白名单模块里的函数和类。
# 刻意不放 format / format_map（格式串能取任意属性）、join 以外的拼接放大（ljust / center / zfill）、mro 之类
ALLOWED_ATTRS = frozenset({
    # dict / list / set
    "get", "items", "keys", "values", "setdefault", "update", "pop", "copy",
    "append", "extend", "sort", "index", "count", "insert", "reverse",
    "add", "discard", "union", "intersection", "difference",
    # str
    "startswith", "endswith", "strip", "split", "join", "replace", "lower", "upper", "isdigit",
    # date / datetime / timedelta
    "datetime", "year", "month", "day", "hour", "minute", "weekday", "isoweekday", "isocalendar", "week", "date", "days",
    "total_seconds", "fromisoformat", "strptime", "strftime", "isoformat", "timedelta",
    # collections
    "Counter", "defaultdict", "OrderedDict", "most_common",
    # statistics
    "mean", "fmean", "median", "median_low", "median_high", "mode", "multimode", "pstdev", "stdev",
    "pvariance", "variance", "quantiles",
    # math
    "floor", "ceil", "sqrt", "fsum", "isclose", "log", "log10", "exp", "prod", "inf",
    # itertools
    "groupby", "chain", "accumulate", "pairwise", "islice", "combinations",
})

# 这些名字出现就拒绝，并给出明确理由（其余没列在内置白名单里的，运行时也取不到）
FORBIDDEN_NAMES = {
    "open": "不能读写文件", "eval": "不能动态执行代码", "exec": "不能动态执行代码", "compile": "不能动态执行代码",
    "getattr": "不能动态取属性", "setattr": "不能动态改属性", "delattr": "不能动态改属性", "hasattr": "不能动态取属性",
    "globals": "不能访问运行环境", "locals": "不能访问运行环境", "vars": "不能访问运行环境", "dir": "不能访问运行环境",
    "type": "不能操作类型", "object": "不能操作类型", "super": "不能操作类型", "memoryview": "不能操作内存",
    "bytearray": "不能操作内存", "bytes": "不能操作内存", "input": "不能交互输入", "breakpoint": "不能调试",
    "help": "不能交互输入", "print": "结果要赋给变量 result，不能打印", "exit": "不能退出进程", "quit": "不能退出进程",
}

_SAFE_NODES = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.For, ast.While, ast.If, ast.Break, ast.Continue, ast.Pass,
    ast.FunctionDef, ast.Return, ast.Lambda, ast.Import, ast.ImportFrom, ast.alias, ast.arguments, ast.arg,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.keyword, ast.Attribute,
    ast.Subscript, ast.Slice, ast.Starred, ast.JoinedStr, ast.FormattedValue, ast.NamedExpr,
    ast.operator, ast.unaryop, ast.boolop, ast.cmpop,
)
_NAMED_REJECTS = {
    ast.ClassDef: "不能定义类", ast.Try: "不能用 try", ast.TryStar: "不能用 try", ast.Raise: "不能抛出异常",
    ast.With: "不能用 with", ast.AsyncFunctionDef: "不能用异步", ast.AsyncFor: "不能用异步", ast.AsyncWith: "不能用异步",
    ast.Await: "不能用异步", ast.Yield: "不能用生成器", ast.YieldFrom: "不能用生成器", ast.Global: "不能用 global",
    ast.Nonlocal: "不能用 nonlocal", ast.Match: "不能用 match", ast.Delete: "不能用 del",
}
_BAD_OPS = {ast.LShift: "<<", ast.RShift: ">>", ast.MatMult: "@"}
_BIG_WIDTH = re.compile(r"\d{3,}")


# 明显是在找出路（而不是模型写错了语法）：双下划线、动态执行 / 取属性、系统类模块。计入熔断的越权次数
_ESCAPE_NAMES = frozenset({"open", "eval", "exec", "compile", "getattr", "setattr", "delattr", "globals", "locals",
                           "vars", "dir", "type", "object", "super", "memoryview", "breakpoint", "input"})
_ESCAPE_MODULES = frozenset({"os", "sys", "subprocess", "socket", "shutil", "ctypes", "importlib", "builtins", "pathlib",
                             "io", "signal", "multiprocessing", "threading", "urllib", "http", "requests", "pickle",
                             "marshal", "gc", "inspect", "posix", "pty", "resource", "code", "codeop", "runpy", "asyncio"})


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    escape: bool = False  # 有逃逸意图的写法


def check(code: str) -> Verdict:
    if not isinstance(code, str) or not code.strip():
        return Verdict(False, ["没有代码"])
    if len(code) > MAX_CODE_CHARS:
        return Verdict(False, [f"代码超过 {MAX_CODE_CHARS} 个字符"])
    try:
        tree = ast.parse(code, mode="exec")
    except (SyntaxError, ValueError) as e:
        return Verdict(False, [f"语法错误：{getattr(e, 'msg', str(e))}"])
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        return Verdict(False, ["代码太长"])
    reasons: list[str] = []
    for node in nodes:
        reason = _node_problem(node)
        if reason and reason not in reasons:
            reasons.append(reason)
    if not any(isinstance(n, ast.Name) and n.id == "result" and isinstance(n.ctx, ast.Store) for n in nodes):
        reasons.append("代码要把结果赋给变量 result")
    return Verdict(not reasons, reasons[:5], escape=any(_hostile(n) for n in nodes))


def _hostile(node: ast.AST) -> bool:
    match node:
        case ast.Name(id=name):
            return name.startswith("__") or name in _ESCAPE_NAMES
        case ast.Attribute(attr=attr):
            return attr.startswith("__")
        case ast.Import(names=names):
            return any(a.name.split(".")[0] in _ESCAPE_MODULES for a in names)
        case ast.ImportFrom(module=module):
            return (module or "").split(".")[0] in _ESCAPE_MODULES
    return False


def _node_problem(node: ast.AST) -> str | None:
    if type(node) in _NAMED_REJECTS:
        return _NAMED_REJECTS[type(node)]
    if not isinstance(node, _SAFE_NODES):
        return f"不允许的语法：{type(node).__name__}"
    match node:
        case ast.Import(names=names):
            bad = [a.name for a in names if a.name not in ALLOWED_MODULES]
            return f"不允许 import {bad[0]}（只能用 {'、'.join(sorted(ALLOWED_MODULES))}）" if bad else None
        case ast.ImportFrom(module=module, names=names, level=level):
            if level or module not in ALLOWED_MODULES:
                return f"不允许 import {module or '.'}（只能用 {'、'.join(sorted(ALLOWED_MODULES))}）"
            bad = [a.name for a in names if a.name not in ALLOWED_ATTRS]
            return f"不允许从 {module} 导入 {bad[0]}" if bad else None
        case ast.alias(asname=asname) if asname and asname.startswith("__"):
            return "不允许使用双下划线名字"
        case ast.Name(id=name):
            if name.startswith("__"):
                return f"不允许使用双下划线名字 {name}"
            if name in FORBIDDEN_NAMES:
                return f"不允许使用 {name}：{FORBIDDEN_NAMES[name]}"
        case ast.arg(arg=name) | ast.FunctionDef(name=name) if name.startswith("__"):
            return f"不允许使用双下划线名字 {name}"
        case ast.FunctionDef(decorator_list=decos) if decos:
            return "不能用装饰器"
        case ast.FunctionDef(name=name) | ast.arg(arg=name) if name in FORBIDDEN_NAMES:
            return f"不允许使用 {name}：{FORBIDDEN_NAMES[name]}"
        case ast.Attribute(attr=attr, ctx=ctx):
            if not isinstance(ctx, ast.Load):
                return "不能给属性赋值"
            if attr.startswith("_"):
                return f"不允许访问下划线属性 .{attr}"
            if attr not in ALLOWED_ATTRS:
                return f"不允许访问属性 .{attr}"
        case ast.Constant(value=value):
            if isinstance(value, bytes):
                return "不能用 bytes 常量"
            if isinstance(value, str) and len(value) > MAX_STR_CONST:
                return "字符串常量太长"
        case ast.BinOp(op=op) | ast.AugAssign(op=op) if type(op) in _BAD_OPS:
            return f"不支持 {_BAD_OPS[type(op)]} 运算"
        case ast.FormattedValue(format_spec=spec) if spec is not None:
            parts = spec.values if isinstance(spec, ast.JoinedStr) else [spec]
            if not all(isinstance(p, ast.Constant) for p in parts):
                return "格式说明只能是常量"
            if _BIG_WIDTH.search("".join(str(p.value) for p in parts)):
                return "格式宽度太大"
    return None
