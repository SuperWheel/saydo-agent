"""一键启动演示：装依赖 → 起后端和前端 → 打开浏览器，打印电脑和手机的访问地址。Ctrl+C 一起停掉。

双击仓库根目录的「一键启动.command」（macOS）/「一键启动.bat」（Windows），或者 `make demo`。默认：
  - 手机可访问：前端对局域网开放，手机和电脑连同一 Wi-Fi（或手机开热点、电脑连上）就能打开；
  - 临时演示数据：每次启动都是一份全新的演示数据，放在系统临时目录，退出时删掉，不碰 data/dev.db；
  - 演示日期 2026-10-30：账单区间、生日计划这些演示用例按这一天编排；
  - 端口被占用时自动换下一个空闲端口。
模型、企业微信照常读仓库根目录的 .env（没配模型就是规则模式，也能完整演示）。

可选参数（`make demo ARGS="--keep-data --local"`，或直接 `uv run --project apps/api python scripts/start.py --help`）：
  --keep-data   用持久的 data/dev.db（演示中途的转账、订单、设置下次启动还在）
  --local       只给本机用，不对局域网开放
  --rules       规则模式：不调大模型（不用配密钥、不耗 API 额度）
  --no-face     打开 App 时跳过进入前人脸核身
  --date D      演示日期，形如 2026-11-02；--today 用真实日期
  --no-browser  不自动打开浏览器
只用标准库：没有装依赖时也能跑起来，再由它去装。
"""

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API, WEB = ROOT / "apps" / "api", ROOT / "apps" / "web"
WINDOWS = os.name == "nt"
DEMO_DATE = "2026-10-30"


def say(text: str = "") -> None:
    print(text, flush=True)


def fail(text: str) -> None:
    say(f"\n✗ {text}")
    sys.exit(1)


def need(cmd: str, hint: str) -> str:
    path = shutil.which(cmd)
    if path is None:
        fail(f"没找到 {cmd}。{hint}")
    return path


def free_port(start: int) -> int:
    """从 start 往后找一个本机和局域网都没人占的端口。"""
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:  # macOS 上 0.0.0.0 绑得上不代表 127.0.0.1 没人听
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    fail(f"{start} 往后 50 个端口都被占用了")


def lan_ips() -> list[str]:
    sys.path.insert(0, str(API))
    try:
        from app.netinfo import lan_ips as found  # 和企业微信引导链接用同一套判断（排除代理软件的虚拟网卡）
        return found()
    except Exception:  # 依赖没装好时退回最简单的办法
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.connect(("223.5.5.5", 53))
                return [s.getsockname()[0]]
            except OSError:
                return []


def wait_until(url: str, proc: subprocess.Popen, name: str, timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            fail(f"{name}没能启动（退出码 {proc.returncode}），看上面的报错")
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except Exception:
            time.sleep(0.5)
    fail(f"{name}等了 {timeout:.0f} 秒还没就绪")


def spawn(args: list[str], cwd: Path, env: dict) -> subprocess.Popen:
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
    return subprocess.Popen(args, cwd=cwd, env=env, **kw)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def _interrupt(*_) -> None:
    raise KeyboardInterrupt


def main() -> None:
    # Ctrl+C、关掉终端窗口（SIGHUP）、kill 都走同一条退出路径：停掉前后端、删临时数据。
    # 前后端在自己的进程组里，不跟着终端一起收到信号，所以必须由这里来停
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _interrupt)
    p = argparse.ArgumentParser(description="一键启动言行 SayDo 演示（后端 + 前端）")
    p.add_argument("--keep-data", action="store_true", help="用持久的 data/dev.db，不用临时演示数据")
    p.add_argument("--local", action="store_true", help="只给本机用，不对局域网开放")
    p.add_argument("--rules", action="store_true", help="规则模式，不调大模型")
    p.add_argument("--no-face", action="store_true", help="跳过进入前人脸核身")
    p.add_argument("--date", default=DEMO_DATE, help=f"演示日期，默认 {DEMO_DATE}")
    p.add_argument("--today", action="store_true", help="用真实日期")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    opt = p.parse_args()

    say("言行 SayDo · 一键启动演示\n")
    uv = need("uv", "先装 uv：https://docs.astral.sh/uv/getting-started/installation/ （装完重新打开终端）")
    npm = need("npm", "先装 Node.js 20 以上：https://nodejs.org/ （装完重新打开终端）")

    say("① 检查依赖（第一次会下载，需要几分钟）")
    if subprocess.run([uv, "sync", "--quiet"], cwd=API).returncode != 0:
        fail("后端依赖安装失败")
    if not (WEB / "node_modules" / ".bin").exists() and subprocess.run([npm, "install"], cwd=WEB).returncode != 0:
        fail("前端依赖安装失败")

    if not (ROOT / ".env").exists():  # 第一次运行：照示例建一份，「设置」页填的模型密钥写在这里
        shutil.copyfile(ROOT / ".env.example", ROOT / ".env")
        say("   已按 .env.example 建好 .env（没填模型密钥时是规则模式，可在 App「设置」里填）")

    api_port, web_port = free_port(8000), free_port(5173)
    tmp = None if opt.keep_data else tempfile.mkdtemp(prefix="saydo-demo-")
    env = {**os.environ}
    if not opt.today:
        env["DEMO_DATE"] = opt.date
    if tmp:
        env["DATABASE_URL"] = f"sqlite:///{Path(tmp) / 'demo.db'}"
    if opt.rules:
        env["LLM_ENABLED"] = "false"
    if opt.no_face:
        env["ENTRY_FACE_REQUIRED"] = "false"
    if web_port != 5173:  # 企业微信里引导回 App 的链接跟着换端口
        env["APP_H5_URL"] = f"http://127.0.0.1:{web_port}"
    web_env = {**env, "API_BASE": f"http://127.0.0.1:{api_port}", "WEB_HOST": "127.0.0.1" if opt.local else "0.0.0.0"}

    say(f"② 启动后端（端口 {api_port}）和前端（端口 {web_port}）")
    procs = [spawn([uv, "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(api_port)], API, env)]
    try:
        wait_until(f"http://127.0.0.1:{api_port}/api/health", procs[0], "后端")
        procs.append(spawn([npm, "run", "dev", "--", "--port", str(web_port), "--strictPort"], WEB, web_env))
        local = f"http://127.0.0.1:{web_port}"
        wait_until(local, procs[1], "前端")

        phones = [] if opt.local else [f"http://{ip}:{web_port}" for ip in lan_ips()]
        say("\n" + "=" * 56)
        say(f"  电脑打开：{local}")
        for url in phones:
            say(f"  手机打开：{url}   （手机和电脑连同一个 Wi-Fi）")
        if not opt.local and not phones:
            say("  没检测到局域网地址：手机和电脑连同一个 Wi-Fi，或手机开热点、电脑连上后重新启动")
        say(f"  演示日期：{'真实日期' if opt.today else opt.date}")
        say(f"  演示数据：{'data/dev.db（保留）' if opt.keep_data else '临时数据，退出后删除'}")
        say(f"  模型：{'规则模式（不调大模型）' if opt.rules else '按 .env 配置；没配就是规则模式，可在 App「设置」里填'}")
        say("  停止：在这个窗口按 Ctrl+C")
        say("=" * 56 + "\n")
        if not opt.no_browser:
            webbrowser.open(local)
        while all(pr.poll() is None for pr in procs):
            time.sleep(1)
        fail("有一个服务意外退出了，看上面的报错")
    except KeyboardInterrupt:
        say("\n正在停止……")
    finally:
        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):  # 清理途中再按 Ctrl+C 也要清理完
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), signal.SIG_IGN)
        for pr in reversed(procs):
            stop(pr)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        say("已停止。" + ("临时演示数据已删除。" if tmp else ""))


if __name__ == "__main__":
    main()
