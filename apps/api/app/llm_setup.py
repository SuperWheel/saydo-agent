"""模型配置页的后端：DeepSeek / GLM 二选一，密钥写进仓库根目录的 .env，改完立即生效，不用重启。

密钥只进 .env：不回传前端（页面只看到「已填写 · 尾号 ab12」），不写审计，测试连接的报错里也抹掉。
"""

import os
import re
import time
from pathlib import Path

from dotenv import dotenv_values

from . import config
from .agent.llm import LLMError, get_llm
from .config import PROVIDERS, get_settings
from .security import mask_pii

KEY_ENV = {"deepseek": "DEEPSEEK_API_KEY", "glm": "ZHIPUAI_API_KEY"}
LABELS = {"deepseek": "DeepSeek", "glm": "智谱 GLM"}
# 两家的密钥都只有字母、数字和 . _ -；拒绝空白和换行，防止借密钥往 .env 里夹带别的配置项
_KEY_OK = re.compile(r"[A-Za-z0-9._\-]{16,200}")
_PASSWORD_OK = re.compile(r"[\x21-\x7e]{6,64}")  # 可见 ASCII，不含空格
# 进程环境变量比 .env 优先，页面要提示「这里改了也不生效」
_WATCHED = ("LLM_PROVIDER", "LLM_API_KEY", "DEEPSEEK_API_KEY", "ZHIPUAI_API_KEY", "GLM_API_KEY", "LLM_ENABLED",
            "ADMIN_PASSWORD")


_MARK = "# 以下由模型配置页写入"


class ConfigError(ValueError):
    pass


def _env_path() -> Path:
    return config.ENV_FILE


def _tail(key: str) -> str:
    return key[-4:] if len(key) >= 8 else ""


def status() -> dict:
    s = get_settings()
    file = {k: v for k, v in dotenv_values(_env_path()).items() if v} if _env_path().exists() else {}
    providers = []
    for pid, preset in PROVIDERS.items():
        key = getattr(s, preset["key_field"])
        providers.append({"id": pid, "label": LABELS[pid], "model": preset["llm_model"], "key_env": KEY_ENV[pid],
                          "has_key": bool(key), "key_tail": _tail(key)})
    warnings = [f"进程环境变量 {k} 优先于 .env，这里保存后对它不生效" for k in _WATCHED if os.environ.get(k)]
    if file.get("LLM_API_KEY"):
        warnings.append("当前用的是 .env 里的 LLM_API_KEY；保存时会把它移到对应供应商那一项")
    return {"provider": s.llm_provider, "model": s.llm_model, "enabled": s.llm_enabled, "ready": s.llm_ready,
            "providers": providers, "env_file": str(_env_path()), "env_file_exists": _env_path().exists(),
            "admin_protected": bool(s.admin_password), "warnings": warnings}


def save(provider: str, api_key: str = "", enabled: bool | None = None, admin_password: str = "") -> dict:
    """写入 .env 并重新加载配置。api_key 留空表示沿用已有的密钥（只切换供应商）。"""
    provider = provider.strip().lower()
    if provider not in KEY_ENV:
        raise ConfigError(f"供应商只能是 {' / '.join(LABELS.values())}")
    api_key = api_key.strip()
    if api_key and not _KEY_OK.fullmatch(api_key):
        raise ConfigError("密钥格式不对：应为 16–200 位字母、数字或 . _ -，不能有空格和换行")
    if admin_password and not _PASSWORD_OK.fullmatch(admin_password):
        raise ConfigError("后台口令要 6–64 位，只能用字母、数字和常见符号，不能有空格")

    s = get_settings()
    updates = {"LLM_PROVIDER": provider}
    file = dotenv_values(_env_path()) if _env_path().exists() else {}
    if legacy := (file.get("LLM_API_KEY") or "").strip():  # 通用密钥归到它原来那家的专用项，避免盖住新填的密钥
        own = KEY_ENV[s.llm_provider]
        if not (file.get(own) or "").strip():
            updates[own] = legacy
        updates["LLM_API_KEY"] = ""
    if api_key:
        updates[KEY_ENV[provider]] = api_key
    elif not (updates.get(KEY_ENV[provider]) or getattr(s, PROVIDERS[provider]["key_field"])):
        raise ConfigError(f"还没有 {LABELS[provider]} 的密钥，请先填写")
    if enabled is not None:
        updates["LLM_ENABLED"] = "true" if enabled else "false"
    if admin_password:
        updates["ADMIN_PASSWORD"] = admin_password
    update_env(_env_path(), updates)
    get_settings.cache_clear()
    return status()


def update_env(path: Path, updates: dict[str, str], mark: str = _MARK) -> None:
    """改写 .env 里的这些项：已有的行原地替换（注释掉的同名示例行不动），没有的追加在末尾；其余内容原样保留。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if "=" in line and not line.lstrip().startswith("#") and name in pending:
            lines[i] = f"{name}={pending.pop(name)}"
    if pending:
        if mark not in lines:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(mark)
        lines += [f"{k}={v}" for k, v in pending.items()]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)  # 只有本人可读
    os.replace(tmp, path)


def test_connection() -> dict:
    """用当前配置发一句很短的话，看密钥和网络是否可用。"""
    s = get_settings()
    if not s.llm_ready:
        return {"ok": False, "error": "模型未启用或没有密钥，现在走规则模式"}
    started = time.monotonic()
    try:
        get_llm().chat([{"role": "user", "content": "请只回复一个字：好"}])
    except LLMError as e:
        text = str(e).replace(s.llm_api_key, "***") if s.llm_api_key else str(e)
        return {"ok": False, "error": mask_pii(text)[:300]}
    return {"ok": True, "provider": s.llm_provider, "model": s.llm_model,
            "latency_ms": round((time.monotonic() - started) * 1000)}
