"""熔断和红队测试共用的辅助函数与假工具。

假工具都注册在 demo / card 领域，用 monkeypatch 注册，测试结束自动撤销：
  demo.nickname   🟡 普通写操作（改昵称，不动钱）
  demo.card_lock  🟡 止损类（stop_loss=True，模拟锁卡），熔断时仍要能办
  demo.limit      🔴 需要强验证的写操作（模拟提额），用来在转账之外制造认证失败
  demo.flaky      只读工具，broken[...] 为真时抛异常，测断路器
  demo.flaky_set  🟡 写工具，broken[...] 为真时写一半再抛异常，测回滚和断路器
"""

from app.auth.actions import propose_action
from app.auth.tokens import otp_for
from app.cards import action_display
from app.db import new_session
from app.flags import get_flag, set_flag
from app.models import AuthChallenge, PendingAction
from app.policy import Risk
from app.tools import REGISTRY, ToolResult, ToolSpec

from .conftest import confirm, say, text, tool

BALANCE = "28,650.32"


def balance(client) -> str:
    return client.get("/api/accounts").json()[0]["balance"]


def after_travel(balance_text: str) -> str:
    """快进以后的预期余额：快进前的余额，加上建库之后补进来的日历流水（工资、日常消费、订阅扣费）的净额。"""
    from datetime import date

    from app.clock import now
    from app.core_mock.seed import CALENDAR_FLAG, _mom, _signed, calendar
    from app.money import fmt_yuan

    with new_session() as db:
        seeded, mom = date.fromisoformat(get_flag(db, CALENDAR_FLAG)["seeded"]), _mom(db)
        delta = sum(_signed(e) for e in calendar(now().date(), mom) if e["at"].date() >= seeded)
    return fmt_yuan(round(float(balance_text.replace(",", "")) * 100) + delta)


def ledger_ok(client) -> bool:
    chk = client.get("/admin/ledger/check").json()
    return chk["balanced"] and all(a["balance_fen"] == a["ledger_fen"] for a in chk["accounts"])


def events(client, trace_id: str | None = None) -> list[str]:
    url = f"/admin/audit?trace_id={trace_id}" if trace_id else "/admin/audit?limit=500"
    return [e["event"] for e in client.get(url).json()]


def sms_code(challenge_id: str) -> str:
    with new_session() as db:
        ch = db.get(AuthChallenge, challenge_id)
        return otp_for(db.get(PendingAction, ch.action_id).digest, ch.otp_issued_at)


def verify(client, auth_card: dict, factor: str, value: str) -> dict:
    r = client.post(f"/api/auth/challenges/{auth_card['challenge_id']}/verify", json={"factor": factor, "value": value})
    assert r.status_code == 200, r.text
    return r.json()


def pass_face(client, auth_card: dict) -> dict:
    return verify(client, auth_card, "face", f"face-ok:{auth_card['digest']}")


def pass_auth(client, auth_card: dict) -> dict:
    pass_face(client, auth_card)
    return verify(client, auth_card, "sms", sms_code(auth_card["challenge_id"]))


def red_auth_card(client, sentence="给赵磊转100", session_id=None) -> dict:
    """说一句会定为 🔴 的话（规则模式），确认后拿到验证卡。"""
    card = say(client, sentence, session_id)["cards"][0]
    assert card["type"] == "confirm" and card["level"] == "RED", card
    out = confirm(client, card).json()
    assert out["status"] == "awaiting_auth", out
    return out["card"]


def order(client, sentence: str, session_id=None, nth: str = "第一个") -> dict:
    """说要买东西 → 候选卡 → 「要第一个」→ 订单确认卡。返回选完这一轮的响应（cards[0] 是订单确认卡）。"""
    r = say(client, sentence, session_id)
    assert r["cards"][0]["type"] == "shop_options", r["cards"]
    return say(client, f"要{nth}", r["session_id"])


def is_breaker(card: dict, level: str | None = None) -> bool:
    """熔断提示卡：说明原因、恢复方式和申诉渠道。"""
    b = card.get("breaker")
    return (card["type"] == "notice" and b is not None and bool(b["reason"]) and "申诉" in b["appeal"]
            and b["reason"] in card["text"] and "申诉" in card["text"] and (level is None or b["level"] == level))


# ---------- 假工具 ----------

broken = {"demo.flaky": False, "demo.flaky_set": False}
calls = {"demo.flaky": 0}


def _writer(name: str):
    def fn(db, args, *, idempotency_key, user_id):
        if broken.get(name):
            set_flag(db, f"partial:{name}", {"written": True})  # 写了一半
            raise RuntimeError("core system timeout")
        set_flag(db, f"{name}:{user_id}", {"value": args["value"]})
        return {"status": "succeeded", "receipt_rows": [["生效时间", "立即"]]}
    return fn


def _proposer(write: str, title: str, verb: str):
    def fn(ctx, value: str):
        display = action_display(title=title, verb=verb, summary=f"{verb}：{value}", rows=[["内容", value]])
        return propose_action(ctx, tool=write, args={"value": value}, display=display)
    return fn


def _flaky(ctx) -> ToolResult:
    calls["demo.flaky"] += 1
    if broken["demo.flaky"]:
        set_flag(ctx.db, "partial:demo.flaky", {"written": True})
        raise RuntimeError("downstream 503")
    return ToolResult({"status": "ok", "value": "正常"})


_WRITES = [  # 写工具名、领域、风险、额外属性、确认卡标题、动作名
    ("demo.nickname", "demo", Risk.YELLOW, {}, "昵称修改确认", "修改昵称"),
    ("demo.card_lock", "card", Risk.YELLOW, {"stop_loss": True}, "锁卡确认", "锁卡"),
    ("demo.limit", "demo", Risk.RED, {}, "提额确认", "提额"),
    ("demo.flaky_set", "demo", Risk.YELLOW, {}, "测试确认", "测试写入"),
]


def install_demo_tools(monkeypatch) -> None:
    for k in broken:
        broken[k] = False
    calls["demo.flaky"] = 0
    for write, domain, risk, extra, title, verb in _WRITES:
        monkeypatch.setitem(REGISTRY, write, ToolSpec(
            name=write, domain=domain, base_risk=risk, side_effect=True, llm_visible=False, description="",
            parameters={}, fn=_writer(write), **extra))
        monkeypatch.setitem(REGISTRY, f"{write}_propose", ToolSpec(
            name=f"{write}_propose", domain=domain, base_risk=Risk.GREEN, side_effect=False, llm_visible=True,
            description=f"{verb}，生成确认卡。", fn=_proposer(write, title, verb), capability=verb,
            parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}))
    monkeypatch.setitem(REGISTRY, "demo.flaky", ToolSpec(
        name="demo.flaky", domain="demo", base_risk=Risk.GREEN, side_effect=False, llm_visible=True,
        description="查一个下游接口。", parameters={"type": "object", "properties": {}}, fn=_flaky, capability="查下游"))


def written(name: str, user_id: int = 1):
    with new_session() as db:
        return get_flag(db, f"{name}:{user_id}").get("value")


def partial(name: str) -> bool:
    with new_session() as db:
        return bool(get_flag(db, f"partial:{name}"))


def llm_name(write: str) -> str:
    return f"{write}_propose".replace(".", "_")


def propose_via_llm(client, fake, write: str, value: str = "x", session_id=None) -> dict:
    """让假模型调用 <write>_propose，返回这一轮的响应。"""
    fake(tool(llm_name(write), value=value), text("请核对确认卡。"))
    return say(client, f"帮我{value}", session_id)


def transfer_via_llm(client, fake, payee="我妈", amount="100", session_id=None) -> dict:
    fake(tool("transfer_propose", payee_hint=payee, amount_text=amount), text("请核对确认卡。"))
    return say(client, f"给{payee}转{amount}", session_id)
