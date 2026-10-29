"""模型接入：OpenAI 兼容接口（DeepSeek / 智谱 GLM）。换供应商只改 .env 里的 LLM_PROVIDER 和密钥。"""

from dataclasses import dataclass

import openai
from openai import OpenAI

from ..config import Settings, get_settings


class LLMError(Exception):
    pass


@dataclass
class LLMReply:
    content: str
    tool_calls: list  # openai ChatCompletionMessageToolCall
    usage: dict
    reasoning: str = ""  # 思考模式下的 reasoning_content，工具循环里要原样带回


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url,
                              timeout=settings.llm_timeout_s, max_retries=1)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice: str = "auto") -> LLMReply:
        s = self.settings
        kwargs: dict = {"model": s.llm_model, "messages": messages, "temperature": s.llm_temperature,
                        "max_tokens": s.llm_max_tokens}
        if tools:
            kwargs["tools"] = tools
            if tool_choice in s.llm_tool_choices:  # GLM 只认 auto，传 none 会被拒；不支持的值干脆不传
                kwargs["tool_choice"] = tool_choice
        if s.llm_thinking:
            kwargs["extra_body"] = {"thinking": {"type": s.llm_thinking}}
        if s.llm_reasoning_effort:
            kwargs["reasoning_effort"] = s.llm_reasoning_effort
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except openai.OpenAIError as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        msg = resp.choices[0].message
        usage = resp.usage.model_dump() if resp.usage else {}
        return LLMReply(content=msg.content or "", tool_calls=list(msg.tool_calls or []), usage=usage,
                        reasoning=getattr(msg, "reasoning_content", None) or "")


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None or _client.settings is not get_settings():
        _client = LLMClient(get_settings())
    return _client
