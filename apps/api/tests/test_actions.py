"""通用待确认动作：新场景只注册「提议工具 + 写工具」，确认、验证、令牌、网关、审计都不用再写。

这里注册一个假的写工具（改昵称，不动钱）走完整条路径。
"""

import pytest

from app.auth.actions import propose_action
from app.cards import action_display
from app.db import new_session
from app.flags import get_flag, set_flag
from app.policy import Risk
from app.tools import REGISTRY, ToolSpec

from .conftest import confirm, say, text, tool


def _propose_nickname(ctx, nickname: str):
    display = action_display(title="昵称修改确认", verb="修改昵称", summary=f"把昵称改成「{nickname}」",
                             rows=[["新昵称", nickname]], fields={"nickname": nickname})
    return propose_action(ctx, tool="demo.nickname_set", args={"nickname": nickname}, display=display)


def _set_nickname(db, args, *, idempotency_key, user_id):
    set_flag(db, f"nickname:{user_id}", {"value": args["nickname"]})
    return {"status": "succeeded", "receipt_rows": [["生效时间", "立即"]]}


@pytest.fixture
def nickname_tools(monkeypatch):
    def install(**write_overrides):
        monkeypatch.setitem(REGISTRY, "demo.nickname_set", ToolSpec(
            name="demo.nickname_set", domain="demo", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
            description="", parameters={}, fn=_set_nickname, **write_overrides))
        monkeypatch.setitem(REGISTRY, "demo.nickname_propose", ToolSpec(
            name="demo.nickname_propose", domain="demo", base_risk=Risk.GREEN, side_effect=False, llm_visible=True,
            description="修改用户昵称，生成确认卡。", fn=_propose_nickname, capability="修改昵称",
            parameters={"type": "object", "properties": {"nickname": {"type": "string"}}, "required": ["nickname"]}))
    return install


def _nickname() -> str | None:
    with new_session() as db:
        return get_flag(db, "nickname:1").get("value")


def test_new_write_tool_goes_through_shell(llm_client, fake, nickname_tools):
    nickname_tools()
    fake(tool("demo_nickname_propose", nickname="小李"), text("请核对确认卡。"))
    r = say(llm_client, "把我的昵称改成小李")
    card = r["cards"][0]
    assert card["type"] == "confirm" and card["title"] == "昵称修改确认" and card["level"] == "YELLOW"
    assert card["rows"] == [["新昵称", "小李"]] and "amount" not in card and "today_after" not in card
    assert card["reasons"] == ["需要你确认后才会执行"]
    assert _nickname() is None  # 确认之前不会改

    out = confirm(llm_client, card).json()
    assert out["status"] == "succeeded"
    assert out["card"]["status_label"] == "修改昵称成功"
    assert out["card"]["rows"] == [["新昵称", "小李"], ["生效时间", "立即"]]
    assert _nickname() == "小李"
    events = [e["event"] for e in llm_client.get("/admin/audit").json()]
    assert {"action_created", "action_confirmed", "token_issued", "execution"} <= set(events)
    assert llm_client.post("/admin/audit/verify").json()["ok"]
    assert confirm(llm_client, card).status_code == 409  # 同一张卡不能用两次


def test_kill_switch_blocks_new_tool(llm_client, fake, nickname_tools):
    nickname_tools()
    llm_client.post("/admin/kill-switch", json={"enabled": True, "reason": "演练"})
    fake(tool("demo_nickname_propose", nickname="小李"), text("暂时办不了。"))
    r = say(llm_client, "把我的昵称改成小李")
    assert [c["type"] for c in r["cards"]] == ["notice"] and "只读保护" in r["cards"][0]["text"]


def test_stop_loss_tool_works_under_kill_switch(llm_client, fake, nickname_tools):
    nickname_tools(stop_loss=True)  # 止损类（如锁卡）在只读保护下仍可执行
    llm_client.post("/admin/kill-switch", json={"enabled": True, "reason": "演练"})
    fake(tool("demo_nickname_propose", nickname="小李"), text("请核对确认卡。"))
    card = say(llm_client, "把我的昵称改成小李")["cards"][0]
    assert card["type"] == "confirm"
    assert confirm(llm_client, card).json()["status"] == "succeeded" and _nickname() == "小李"


def test_new_tool_is_visible_to_model(llm_client, fake, nickname_tools):
    nickname_tools()
    f = fake(text("你好"))
    say(llm_client, "你好")
    names = {t["function"]["name"] for t in f.calls[0]["tools"]}
    assert "demo_nickname_propose" in names and "demo_nickname_set" not in names  # 写工具永远不给模型
    assert "- 修改昵称：调用 demo_nickname_propose。" in f.calls[0]["messages"][0]["content"]  # 能力清单自动生成


def test_help_text_lists_registered_capabilities(client, nickname_tools):
    nickname_tools()
    reply = say(client, "你好")["reply"]["text"]
    assert reply.startswith("我现在可以帮你查余额、") and "修改昵称" in reply and "「给我妈转500」" in reply
