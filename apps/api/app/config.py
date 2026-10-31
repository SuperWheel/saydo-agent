"""运行配置。所有可调参数都从环境变量或仓库根目录的 .env 读取，切换模型不改代码。"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
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

# 知识库检索的向量通道（knowledge/embedding.py，可选）。EMBEDDING_PROVIDER 留空就只走 BM25。
# siliconflow：硅基流动 Qwen/Qwen3-Embedding-8B（取 1024 维），EMBEDDING_API_KEY 没填时用 SILICONFLOW_API_KEY；
# zhipu：智谱 embedding-3，没填时复用 ZHIPUAI_API_KEY；openai：任意 OpenAI 兼容 /embeddings，地址、模型、密钥自己填
_QWEN3_QUERY = "Instruct: Given a bank customer's question, retrieve knowledge base entries that answer it\nQuery:"
EMBEDDING_PROVIDERS: dict[str, dict] = {
    "siliconflow": {"embedding_base_url": "https://api.siliconflow.cn/v1", "embedding_model": "Qwen/Qwen3-Embedding-8B",
                    "embedding_dimensions": 1024, "embedding_query_instruction": _QWEN3_QUERY,
                    "key_field": "siliconflow_api_key"},
    "zhipu": {"embedding_base_url": "https://open.bigmodel.cn/api/paas/v4", "embedding_model": "embedding-3",
              "key_field": "zhipuai_api_key"},
    "openai": {"key_field": None},
}
_QWEN3_ONLY = ("embedding_dimensions", "embedding_query_instruction")
_EMBEDDING_ALIASES = {"glm": "zhipu", "zhipuai": "zhipu", "silicon": "siliconflow", "off": "", "none": "", "bm25": ""}


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

    # 知识库向量检索（可选）：留空 = 只用 BM25，行为和不配一样
    embedding_provider: str = ""
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str = ""
    embedding_dimensions: int | None = None  # 输出维度（Qwen3、embedding-3 支持）；不填用预设，预设也没有就不传
    embedding_query_instruction: str | None = None  # 查询前面加的检索指令（Qwen3-Embedding 预设自带）；写一个空格表示不加
    siliconflow_api_key: str = ""
    embedding_timeout_s: float = 2.0  # 查询取向量的时限，超时就这一问只用 BM25

    database_url: str = f"sqlite:///{REPO_ROOT / 'data' / 'dev.db'}"
    app_secret: str = "dev-only-secret-change-me"
    admin_password: str = ""  # 运营后台口令；不设时后台只在演示模式下开放
    demo_mode: bool = True  # 演示环境：模拟短信内容直接回显到前端
    demo_user_id: int = 1
    action_ttl_s: int = 120  # 确认卡有效期
    token_ttl_s: int = 60  # 执行令牌有效期
    otp_window_s: int = 300  # 短信验证码有效期
    entry_face_required: bool = True  # 进入 App 前先过模拟人脸，/api 业务接口校验进入凭证（auth/entry.py）
    entry_face_ttl_s: int = 1800  # 进入凭证有效期（按真实时间，不受演示时间快进影响）
    app_h5_url: str = "http://127.0.0.1:5173"  # IM 里需要确认的操作，引导用户到这个地址的言行 App 办理

    # 企业微信智能机器人（长连接模式，channels/wecom.py）。BotID 和 Secret 都填了才启动
    wecom_bot_id: str = ""
    wecom_bot_secret: str = ""
    wecom_ws_url: str = "wss://openws.work.weixin.qq.com"
    wecom_proxy: str = ""  # 连企业微信走的代理（如 http://127.0.0.1:7890）；空 = 直连，不用系统代理
    wecom_bind_user_id: int | None = None  # 企业微信用户默认绑定到哪个演示用户；不填用 demo_user_id
    wecom_allowed_users: str = ""  # 逗号分隔的企业微信 userid 白名单；空 = 企业内成员都可以用

    @field_validator("embedding_model", "embedding_base_url", "embedding_dimensions", "embedding_query_instruction",
                     mode="before")
    @classmethod
    def _blank_is_preset(cls, v):
        # 设置页切换供应商时会写 EMBEDDING_MODEL= 这样的空值：空值表示用预设。查询指令要关掉时写一个空格
        return None if isinstance(v, str) and v == "" else v

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
        p = self.embedding_provider.strip().lower()
        self.embedding_provider = _EMBEDDING_ALIASES.get(p, p)
        if self.embedding_provider:
            emb = EMBEDDING_PROVIDERS.get(self.embedding_provider)
            if emb is None:
                raise ValueError(f"EMBEDDING_PROVIDER 只支持 {' / '.join(EMBEDDING_PROVIDERS)}（留空 = 只用 BM25），"
                                 f"收到 {self.embedding_provider!r}")
            self.embedding_model = self.embedding_model or emb.get("embedding_model")
            for key, value in emb.items():
                # 维度和查询指令只对 Qwen3-Embedding 系列有意义；换成别的模型（如 BAAI/bge-m3）就不带
                if key in _QWEN3_ONLY and "qwen3-embedding" not in (self.embedding_model or "").lower():
                    continue
                if key in type(self).model_fields and getattr(self, key) is None:
                    setattr(self, key, value)
            if not self.embedding_api_key and emb["key_field"]:
                self.embedding_api_key = getattr(self, emb["key_field"])
        return self

    @property
    def llm_tool_choices(self) -> tuple[str, ...]:
        return PROVIDERS[self.llm_provider]["tool_choices"]

    @property
    def llm_ready(self) -> bool:
        return self.llm_enabled and bool(self.llm_api_key)

    @property
    def embedding_ready(self) -> bool:
        return bool(self.embedding_provider and self.embedding_model and self.embedding_base_url
                    and self.embedding_api_key)

    @property
    def wecom_ready(self) -> bool:
        return bool(self.wecom_bot_id.strip() and self.wecom_bot_secret.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=ENV_FILE)  # 改了 .env 之后 get_settings.cache_clear() 就会重新读
