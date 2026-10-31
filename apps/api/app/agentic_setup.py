"""知识检索设置页的「agentic 重查」开关：写 KB_AGENTIC_RETRY 进仓库根目录的 .env，保存后立即生效，不用重启。

开着时，知识库原话没查到会让对话模型把问题改写成 1–2 个书面说法再查一次（tools/knowledge.py）；要对话模型可用，
规则模式不改写。没有密钥要填，所以这里只有一个开关。
"""

import os

from . import config
from .config import get_settings
from .embedding_setup import reload_settings
from .llm_setup import update_env

_MARK = "# 以下由知识检索设置页写入"


def status() -> dict:
    s = get_settings()
    warnings = ["进程环境变量 KB_AGENTIC_RETRY 优先于 .env，这里保存后对它不生效"] if os.environ.get("KB_AGENTIC_RETRY") else []
    return {"enabled": s.kb_agentic_retry, "llm_ready": s.llm_ready, "active": s.kb_agentic_retry and s.llm_ready,
            "llm": f"{s.llm_provider} / {s.llm_model}" if s.llm_ready else "", "timeout_s": s.kb_rewrite_timeout_s,
            "env_file": str(config.ENV_FILE), "warnings": warnings}


def save(enabled: bool) -> dict:
    update_env(config.ENV_FILE, {"KB_AGENTIC_RETRY": "true" if enabled else "false"}, _MARK)
    reload_settings()
    return status()
