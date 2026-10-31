#!/bin/bash
# macOS：在访达里双击这个文件，就会打开终端启动演示（后端 + 前端），并打印电脑和手机的访问地址。
# 默认手机可访问、临时演示数据、演示日期 2026-10-30；说明见 scripts/start.py。停止：在终端窗口按 Ctrl+C。
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"  # 双击启动时不读 shell 配置，补上 uv / node 的常见安装位置
if ! command -v uv >/dev/null 2>&1; then
  echo "没找到 uv。先在终端运行下面这行安装，装完重新双击："
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  read -r -p "按回车关闭窗口" _
  exit 1
fi
uv run --project apps/api python scripts/start.py "$@"
read -r -p "按回车关闭窗口" _
