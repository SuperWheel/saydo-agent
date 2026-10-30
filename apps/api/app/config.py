"""运行配置。所有可调参数都从环境变量或仓库根目录的 .env 读取，切换模型不改代码。"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
# 模型配置页（llm_setup.py）也写这个文件；SAYDO_ENV_FILE 可以指到别处（测试、多套配置）
ENV_FILE = Path(os.environ.get("SAYDO_ENV_FILE") or REPO_ROOT / ".env")

# 模型供应商预设。.env 里写 LLM_PROVIDER 和对应密钥就能跑；LLM_MODEL 等单项写了就覆盖预设
PROVIDERS: dict[str, dict] = {
    # deepseek-flash 当前由 DeepSeek-V4.1-Flash 提供服务；工具调用走非思考模式，延迟低
    "deepseek": {
        "llm_base_url": "https://api.deepseek.com", "llm_model": "deepseek-flash", "llm_thinking": "disabled",
        "llm_reasoning_effort": "", "llm_temperature": 0.2, "llm_max_tokens": 800, "llm_timeout_s": 20.0,
        "key_field": "deepseek_api_key", "tool_choices": ("auto", "none"),
    },
    # 智谱 GLM-5.3-Flash，走标准 API 端点（按条款，Coding Plan 额度不能用在自建应用里）。
    # 思考模式关不掉，tool_choice 只支持 auto；max_tokens 给思考内容留足余量
    "glm": {
        "llm_base_url": "https://open.bigmodel.cn/api/paas/v4", "llm_model": "glm-5.3-flash", "llm_thinking": "enabled",
        "llm_reasoning_effort": "low", "llm_temperature": 1.0, "llm_max_tokens": 4096, "llm_timeout_s": 60.0,
        "key_field": "zhipuai_api_key", "tool_choices": ("auto",),
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")  # .env 由 get_settings 按 ENV_FILE 传入

    llm_provider: str = "deepseek"
    # 下面几项留空就用供应商预设
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_thinking: str | None = None  # enabled / disabled；空串表示不传
    llm_reasoning_effort: str | None = None  # 空串表示不传
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None
    llm_timeout_s: float | None = None
    llm_enabled: bool = True  # false 时只走规则兜底（CI、断网演示）
    workflow_enabled: bool = True  # 固定流程走规则工作流、不调模型；false 时全部交给模型（对比评测用）
    # 密钥：LLM_API_KEY 填了就优先用；否则按供应商取对应的那一项
    llm_api_key: str = ""
    deepseek_api_key: str = ""
    zhipuai_api_key: str = Field(default="", validation_alias=AliasChoices("ZHIPUAI_API_KEY", "GLM_API_KEY"))

    database_url: str = f"sqlite:///{REPO_ROOT / 'data' / 'dev.db'}"
    app_secret: str = "dev-only-secret-change-me"
    admin_password: str = ""  # 运营后台口令；不设时后台只在演示模式下开放
    demo_mode: bool = True  # 演示环境：模拟短信内容直接回显到前端
    demo_user_id: int = 1
    action_ttl_s: int = 120  # 确认卡有效期
    token_ttl_s: int = 60  # 执行令牌有效期
    otp_window_s: int = 300  # 短信验证码有效期

    @model_validator(mode="after")
    def _apply_provider(self) -> "Settings":
        self.llm_provider = self.llm_provider.strip().lower()
        preset = PROVIDERS.get(self.llm_provider)
        if preset is None:
            raise ValueError(f"LLM_PROVIDER 只支持 {' / '.join(PROVIDERS)}，收到 {self.llm_provider!r}")
        for key, value in preset.items():
            if key in type(self).model_fields and getattr(self, key) is None:
                setattr(self, key, value)
        self.llm_api_key = self.llm_api_key or getattr(self, preset["key_field"])
        return self

    @property
    def llm_tool_choices(self) -> tuple[str, ...]:
        return PROVIDERS[self.llm_provider]["tool_choices"]

    @property
    def llm_ready(self) -> bool:
        return self.llm_enabled and bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=ENV_FILE)  # 改了 .env 之后 get_settings.cache_clear() 就会重新读
