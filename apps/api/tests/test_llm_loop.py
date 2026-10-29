"""模型路径：用脚本化的假模型验证工具循环、参数校验、事实校验和故障降级。"""

import json

import pytest

from app.agent import loop
from app.agent.llm import LLMError, LLMReply

from .conftest import say


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _ToolCall:
    def __init__(self, name, args, id="call_1"):
        self.id, self.type = id, "function"
        self.function = _Fn(name, json.dumps(args, ensure_ascii=False))

    def model_dump(self, **_):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


def tool(name, **args):
    return LLMReply(content="", tool_calls=[_ToolCall(name, args)], usage={})


def text(content):
    return LLMReply(content=content, tool_calls=[], usage={})


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

        class _S:
            llm_model = "fake"
        self.settings = _S()

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def fake(monkeypatch):
    def install(*script):
        f = FakeLLM(script)
        monkeypatch.setattr(loop, "get_llm", lambda: f)
        return f
    return install


def test_llm_proposes_transfer(llm_client, fake):
    f = fake(tool("transfer_propose", payee_hint="我妈", amount_text="500"), text("已为你生成给王*英 500 元的转账确认卡，请核对。"))
    r = say(llm_client, "给我妈转500")
    assert r["reply"]["source"] == "llm" and r["reply"]["ai_generated"]
    assert r["cards"][0]["type"] == "confirm" and r["cards"][0]["level"] == "YELLOW"
    tool_names = {t["function"]["name"] for t in f.calls[0]["tools"]}
    assert tool_names == {"account_balance", "contact_search", "transfer_propose"}  # 模型看不到写工具


def test_hallucinated_number_falls_back_to_template(llm_client, fake):
    fake(tool("account_balance"), text("你的余额是 99,999.00 元"), text("你的余额是 99,999.00 元"))
    r = say(llm_client, "我还有多少钱")
    assert r["reply"]["source"] == "template" and r["reply"]["text"] == "你名下账户余额合计 28,650.32 元。"


def test_false_success_claim_is_rewritten(llm_client, fake):
    f = fake(tool("transfer_propose", payee_hint="我妈", amount_text="500"), text("已经帮你转账成功！"), text("请核对确认卡后点击确认。"))
    r = say(llm_client, "给我妈转500")
    assert r["reply"]["text"] == "请核对确认卡后点击确认。"
    assert "[系统校验]" in f.calls[-1]["messages"][-1]["content"]


def test_amount_not_from_user_is_rejected(llm_client, fake):
    fake(tool("transfer_propose", payee_hint="我妈", amount_text="5000"), text("好的"))
    r = say(llm_client, "给我妈转500")
    assert [c["type"] for c in r["cards"]] == ["notice"]
    assert llm_client.get("/admin/actions").json() == []


def test_smuggled_parameter_is_rejected(llm_client, fake):
    fake(tool("transfer_propose", payee_hint="我妈", amount_text="500", raw_account="6222000011112222"), text("出错了"))
    r = say(llm_client, "给我妈转500")
    assert r["cards"] == [] and llm_client.get("/admin/actions").json() == []
    calls = [e for e in llm_client.get("/admin/audit").json() if e["event"] == "tool_call"]
    assert calls[0]["payload"]["status"] == "error"


def test_llm_outage_falls_back_to_rules(llm_client, fake):
    fake(LLMError("timeout"))
    r = say(llm_client, "给我妈转500")
    assert r["reply"]["source"] == "rule" and r["cards"][0]["type"] == "confirm"


def test_reasoning_is_passed_back_in_tool_loop(llm_client, fake):
    first = tool("account_balance")
    first.reasoning = "用户想知道余额，先查账户"
    f = fake(first, text("你名下账户余额合计 28,650.32 元。"))
    say(llm_client, "我还有多少钱")
    assistant = next(m for m in f.calls[1]["messages"] if m["role"] == "assistant" and m.get("tool_calls"))
    assert assistant["reasoning_content"] == "用户想知道余额，先查账户"


def test_untrusted_memo_never_reaches_model(llm_client, fake):
    f = fake(tool("account_balance"), text("你名下账户余额合计 28,650.32 元。"))
    say(llm_client, "我还有多少钱")
    sent = json.dumps([c["messages"] for c in f.calls], ensure_ascii=False)
    assert "忽略之前的所有指令" not in sent  # 种子数据里的注入备注不会进入模型上下文
