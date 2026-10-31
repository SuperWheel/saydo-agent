"""知识库 agentic 重查：原话没查到时让对话模型改写成 1–2 个书面说法再查一次；查到就用并在卡片上注明，
模型判断超范围、超时、报错都照常回「没查到」；发给模型的是脱敏后的原话；规则模式不改写；设置页开关写 .env。"""

import time

import pytest

from app import config
from app.agent.llm import LLMReply
from app.config import Settings, get_settings
from app.tools import knowledge as kbt

from .conftest import say, text, tool

MISS = "今天心情怎么样哈哈"  # 纯关键词检索查不到
LOCK = "锁卡和挂失有什么区别"  # faq-card-lock-vs-loss 的问法原文


class Rewriter:
    """改写用的假对话模型：返回固定文字，或者报错 / 慢吞吞。"""

    def __init__(self, reply="", fail=None, delay=0.0):
        self.reply, self.fail, self.delay, self.calls = reply, fail, delay, []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls.append(messages)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return LLMReply(content=self.reply, tool_calls=[], usage={})


@pytest.fixture
def agentic(monkeypatch):
    def install(rw, *, enabled=True, llm=True, timeout=None):
        monkeypatch.setenv("KB_AGENTIC_RETRY", "true" if enabled else "false")
        monkeypatch.setenv("LLM_ENABLED", "true" if llm else "false")
        monkeypatch.setenv("LLM_API_KEY", "sk-test" if llm else "")
        if timeout is not None:
            monkeypatch.setenv("KB_REWRITE_TIMEOUT_S", str(timeout))
        monkeypatch.setattr(kbt, "get_llm", lambda: rw)
        get_settings.cache_clear()
        return rw
    yield install
    get_settings.cache_clear()


def test_on_by_default_but_needs_a_ready_llm(agentic):
    assert Settings.model_fields["kb_agentic_retry"].default is True
    rw = agentic(Rewriter(LOCK), llm=False)  # 规则模式（没有可用的对话模型）不改写
    r = kbt.lookup(MISS)
    assert r.data["status"] == "no_hit" and rw.calls == []


def test_no_hit_is_rewritten_and_searched_again(agentic):
    rw = agentic(Rewriter(f"1. {LOCK}\n2. 临时锁卡与挂失的差别"))
    r = kbt.lookup(MISS)
    assert r.data["status"] == "ok" and r.data["rewritten_query"] == LOCK
    assert r.card["rewritten"] == LOCK and r.card["query"] == MISS  # 卡片上原话和改写都在
    assert r.card["items"][0]["id"] == "faq-card-lock-vs-loss"
    system, user = rw.calls[0]
    assert system["role"] == "system" and "知识库" in system["content"] and user["content"] == MISS


def test_hit_on_first_try_never_calls_the_model(agentic):
    rw = agentic(Rewriter(LOCK))
    r = kbt.lookup(LOCK)
    assert r.data["status"] == "ok" and "rewritten" not in r.card and rw.calls == []


def test_out_of_scope_error_and_timeout_stay_no_hit(agentic):
    agentic(Rewriter("无"))  # 模型判断不在知识库范围
    assert kbt.lookup(MISS).card.get("kb_no_hit")
    agentic(Rewriter(fail=RuntimeError("boom")))
    assert kbt.rewrite_query(MISS) == ([], "RuntimeError")
    assert kbt.lookup(MISS).data["status"] == "no_hit"
    agentic(Rewriter(LOCK, delay=0.5), timeout=0.05)
    started = time.monotonic()
    assert kbt.rewrite_query(MISS) == ([], "timeout")
    assert time.monotonic() - started < 0.4  # 不等模型慢慢返回


def test_query_is_masked_before_it_reaches_the_model(agentic):
    rw = agentic(Rewriter("无"))
    kbt.lookup("我手机13812345678今天心情怎么样")
    sent = rw.calls[0][1]["content"]
    assert "13812345678" not in sent and "今天心情怎么样" in sent


def test_rewrites_are_cleaned_deduplicated_and_capped(agentic):
    agentic(Rewriter(f"- 「{LOCK}」\n2) {LOCK}\n无\n{MISS}\n第三个说法\n第四个说法"))
    assert kbt.rewrite_query(MISS) == ([LOCK, "第三个说法"], "")  # 去编号和引号、去重、去掉「无」和原话、最多 2 个


def test_llm_turn_shows_rewrite_on_card_and_audits_it(llm_client, fake, agentic):
    rw = agentic(Rewriter(LOCK))
    fake(tool("kb_search", query=MISS), text("锁卡可以自己随时解锁，挂失要补办新卡。出处：锁卡和挂失的区别。"))
    r = say(llm_client, MISS)
    card = next(c for c in r["cards"] if c["type"] == "knowledge")
    assert card["rewritten"] == LOCK and rw.calls
    events = llm_client.get(f"/admin/audit?trace_id={r['trace_id']}").json()
    assert [e["payload"] for e in events if e["event"] == "kb_query_rewrite"] == [
        {"rewrites": [LOCK], "hit": LOCK, "reason": ""}]


def test_settings_switch_writes_env_and_takes_effect(client, tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("APP_SECRET=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    monkeypatch.delenv("KB_AGENTIC_RETRY", raising=False)  # 进程环境变量会盖住 .env
    get_settings.cache_clear()
    c = client.get("/admin/agentic-config").json()
    assert c["enabled"] and not c["llm_ready"] and not c["active"]  # 默认开，但规则模式下不生效
    c = client.post("/admin/agentic-config", json={"enabled": False}).json()
    assert not c["enabled"] and "KB_AGENTIC_RETRY=false" in path.read_text(encoding="utf-8")
    assert "APP_SECRET=keep-me" in path.read_text(encoding="utf-8")
    assert not get_settings().kb_agentic_retry
    events = client.get("/admin/audit").json()
    assert any(e["event"] == "kb_agentic_config" and e["payload"] == {"enabled": False} for e in events)
