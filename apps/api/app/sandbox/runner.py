"""沙箱子进程入口。由 executor.py 用 `python -I -S -B runner.py` 拉起，只依赖标准库，不导入 app 包。

stdin 读一份 JSON：代码、脱敏快照、限额和白名单（由父进程给出）；stdout 写一行 JSON 结果。
进入用户代码之前依次收紧：资源限制 → 预先导入白名单模块 → 编译 → 审计钩子。之后用户代码只能拿到：
受限的内置函数表、只读快照 txns、只放行白名单模块的 __import__。审计钩子只放行执行用户代码那一次 exec，
其余事件（打开文件、网络、子进程、os.*、再次 compile / exec …）一律拦截。
"""

import json
import os
import resource
import sys
from types import MappingProxyType

_blocked: list[str] = []


class SandboxViolation(Exception):
    pass


def _limit(name: str, soft: int, hard: int | None = None) -> None:
    try:
        resource.setrlimit(getattr(resource, name), (soft, soft if hard is None else hard))
    except (ValueError, OSError, AttributeError):
        pass  # macOS 不支持内存类限制，由父进程按物理内存占用轮询兜底


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
    sys.stdout.flush()
    os._exit(0)


def _fail(error: str, detail: str = "") -> None:
    _emit({"ok": False, "error": error, "detail": detail[:160]})


def main() -> None:
    spec = json.loads(sys.stdin.read())
    cpu_s, mem = int(spec["cpu_s"]), int(spec["memory_bytes"])
    _limit("RLIMIT_CPU", cpu_s, cpu_s + 1)
    _limit("RLIMIT_FSIZE", 0)  # 一个字节都不能写进文件（stdout 是管道，不受影响）
    _limit("RLIMIT_CORE", 0)
    _limit("RLIMIT_NPROC", 0)  # 不能再起子进程
    _limit("RLIMIT_AS", mem)
    _limit("RLIMIT_DATA", mem)

    allowed = frozenset(spec["modules"])
    internal = allowed | {"_strptime"}  # datetime.strptime 内部会经由 __import__ 取 _strptime；用户代码写不出这个 import
    for name in sorted(internal):  # 在审计钩子之前导入完：之后再导入要读文件，会被拦截
        __import__(name)

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name not in internal:
            raise ImportError(f"不允许 import {name}")
        return sys.modules[name]

    import builtins
    safe_builtins = {k: getattr(builtins, k) for k in spec["builtins"]}
    safe_builtins["__import__"] = safe_import
    txns = tuple(MappingProxyType(dict(t)) for t in spec["txns"])  # 只读：改字段、追加都会报错
    scope = {"__builtins__": safe_builtins, "__name__": "analysis", "txns": txns}
    max_output = int(spec["max_output"])

    try:
        code = compile(spec["code"], "<analysis>", "exec")
    except (SyntaxError, ValueError) as e:
        _fail("syntax", str(e))
        return
    _limit("RLIMIT_NOFILE", 3)  # 只剩 stdin / stdout / stderr，打不开新的文件和套接字
    sys.setrecursionlimit(200)

    first_exec = [True]

    def hook(event: str, args) -> None:
        if event == "exec" and first_exec[0]:
            first_exec[0] = False
            return
        _blocked.append(event)
        raise SandboxViolation(f"禁止的操作：{event}")

    sys.addaudithook(hook)

    try:
        exec(code, scope)
    except MemoryError:
        _fail("memory")
    except RecursionError:
        _fail("recursion")
    except SandboxViolation as e:
        _fail("blocked", str(e))
    except BaseException as e:  # noqa: BLE001  用户代码的任何异常都只报类型和简短信息
        _fail("runtime", f"{type(e).__name__}: {e}")
    if _blocked:  # 违规被用户代码以外的地方吞掉了，照样作废
        _fail("blocked", f"禁止的操作：{_blocked[0]}")
    if "result" not in scope:
        _fail("no_result", "代码没有给 result 赋值")
    try:
        out = json.dumps(scope["result"], ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as e:
        _fail("bad_output", f"result 不能转成 JSON：{type(e).__name__}")
        return
    if len(out.encode()) > max_output:
        _fail("output_too_large", f"result 超过 {max_output} 字节")
    _emit({"ok": True, "result": json.loads(out)})


if __name__ == "__main__":
    main()
