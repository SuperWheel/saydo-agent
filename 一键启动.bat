@echo off
rem Windows：双击这个文件启动演示（后端 + 前端），并打印电脑和手机的访问地址。说明见 scripts\start.py
chcp 65001 >nul
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo 没找到 uv。先在 PowerShell 里运行下面这行安装，装完重新双击：
  echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
  pause
  exit /b 1
)
uv run --project apps/api python scripts\start.py %*
pause
