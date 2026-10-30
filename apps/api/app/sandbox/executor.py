"""第二道关：子进程执行。一次性子进程（-I 隔离模式、空环境变量、临时空目录），父进程负责：

- 墙钟超时：到点杀掉整个进程组；
- 内存：macOS 不支持内存类 rlimit，父进程每 5 毫秒查一次子进程的物理内存占用，超限立即杀掉；
  Linux 上 runner 里的 RLIMIT_AS 同时生效；
- CPU：runner 里设 RLIMIT_CPU，超时由内核发 SIGXCPU；
- 输出：stdout 超过上限直接作废，stderr 只留一小段用于审计。
"""

import ctypes
import ctypes.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .checker import ALLOWED_BUILTINS, ALLOWED_MODULES

RUNNER = Path(__file__).with_name("runner.py")


@dataclass(frozen=True)
class Limits:
    cpu_s: int = 2
    wall_s: float = 5.0
    memory_bytes: int = 256 * 1024 * 1024
    max_output: int = 16 * 1024  # runner 里 result 序列化后的上限
    max_stdout: int = 64 * 1024  # 父进程读 stdout 的上限


@dataclass
class RunOutcome:
    status: str  # ok / timeout / cpu_limit / memory / recursion / blocked / runtime / syntax / no_result / bad_output / output_too_large / crashed
    elapsed_ms: int
    result: object = None
    detail: str = ""
    peak_bytes: int = 0
    exit_code: int | None = None
    stderr_tail: str = field(default="", repr=False)


def _footprint_reader():
    """返回 pid → 当前物理内存占用（字节）的函数；拿不到时返回 None。"""
    if sys.platform == "darwin":
        lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
        buf = ctypes.create_string_buffer(256)  # rusage_info_v0：uuid[16] 后 9 个 uint64，ri_phys_footprint 在偏移 72

        def read(pid: int) -> int | None:
            if lib.proc_pid_rusage(pid, 0, buf) != 0:
                return None
            return int.from_bytes(buf.raw[72:80], sys.byteorder)
        return read

    def read_proc(pid: int) -> int | None:
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except OSError:
            return None
        return None
    return read_proc


_footprint = _footprint_reader()


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run(code: str, txns: list[dict], limits: Limits = Limits()) -> RunOutcome:
    payload = json.dumps({
        "code": code, "txns": txns, "cpu_s": limits.cpu_s, "memory_bytes": limits.memory_bytes,
        "max_output": limits.max_output, "modules": sorted(ALLOWED_MODULES), "builtins": sorted(ALLOWED_BUILTINS),
    }, ensure_ascii=False).encode()
    started = time.monotonic()
    killed_for: list[str] = []
    peak = [0]
    with tempfile.TemporaryDirectory(prefix="saydo-sbx-") as cwd:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(RUNNER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd, env={}, start_new_session=True, close_fds=True,
        )
        done = threading.Event()

        def watch_memory() -> None:
            while not done.wait(0.005):
                used = _footprint(proc.pid)
                if used is None:
                    continue
                peak[0] = max(peak[0], used)
                if used > limits.memory_bytes:
                    killed_for.append("memory")
                    _kill(proc)
                    return

        watcher = threading.Thread(target=watch_memory, daemon=True)
        watcher.start()
        try:
            out, err = proc.communicate(payload, timeout=limits.wall_s)
        except subprocess.TimeoutExpired:
            killed_for.append("timeout")
            _kill(proc)
            out, err = proc.communicate()
        finally:
            done.set()
            watcher.join()
    elapsed = int((time.monotonic() - started) * 1000)
    rc = proc.returncode
    base = dict(elapsed_ms=elapsed, peak_bytes=peak[0], exit_code=rc, stderr_tail=err[-300:].decode(errors="replace"))

    if killed_for:
        return RunOutcome(status=killed_for[0], **base)
    if rc == -signal.SIGXCPU or rc == -signal.SIGKILL:
        return RunOutcome(status="cpu_limit", **base)
    if rc == -signal.SIGXFSZ:
        return RunOutcome(status="blocked", detail="禁止写文件", **base)
    if len(out) > limits.max_stdout:
        return RunOutcome(status="output_too_large", **base)
    try:
        msg = json.loads(out.decode().strip().splitlines()[-1])
    except (ValueError, IndexError, UnicodeDecodeError):
        return RunOutcome(status="crashed", detail=f"退出码 {rc}", **base)
    if not isinstance(msg, dict):
        return RunOutcome(status="crashed", **base)
    if msg.get("ok"):
        return RunOutcome(status="ok", result=msg.get("result"), **base)
    return RunOutcome(status=str(msg.get("error") or "runtime"), detail=str(msg.get("detail") or ""), **base)
