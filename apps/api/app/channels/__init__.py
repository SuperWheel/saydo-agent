"""IM 渠道的公共部分：身份绑定、渠道降权、卡片转纯文本。具体渠道的收发协议在各自的模块里（如 wecom.py）。

渠道降权：IM 里只做只读查询（余额、账单、卡片状态、订阅、理财持仓、知识问答），不在 IM 里展示任何可确认的东西。
  1. 进 agent 之前：规则意图或跨场景识别判断是写操作（转账、AA、锁卡、申购、取消订阅、订花……）的，不进 agent，直接回引导语；
  2. 进了 agent 之后：这一轮只要生成了待确认动作，或出现只读卡片以外的卡片，就把待确认动作撤销
     （调用 auth.actions.cancel：释放预占额度、写审计），回复换成引导语。
确认、认证、执行仍然只在 App 里走确认卡 → 强验证 → 执行令牌 → 网关，这里不碰那条链路。

身份绑定：演示环境按配置把 IM 账号自动绑定到演示用户。真实环境应由用户在 App 内登录后，
经企业微信 OAuth 网页授权拿到 userid 再写入 im_bindings，未绑定的账号一律只回「请先绑定」。
"""

import re
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..agent import planner
from ..agent.loop import handle_turn
from ..auth.actions import ACTIVE, cancel
from ..config import get_settings
from ..models import ChatSession, ImBinding, PendingAction, User
from ..nlu import rule_intent
from ..security import mask_pii

MAX_REPLY = 200  # IM 回复字数上限：结论加几行要点，明细回 App 看
MORE = "完整明细请在言行 App 查看。"
NOT_BOUND = "你的企业微信账号还没有绑定言行，请先在言行 App 内完成绑定。"
TEXT_ONLY = "目前只支持文字消息，可以问我余额、账单、卡片状态、订阅、理财持仓和业务规则。"

# IM 里能展示的卡片：都是只读查询的结果。确认卡、验证卡、回执、计划卡、收款人选择、理财推荐 / 问卷、订花订蛋糕等一律不展示
READ_CARDS = {"balance", "bill_report", "bill_txns", "bill_categories", "bill_anomalies", "bill_analysis", "card_list",
              "sub_overview", "sub_upcoming", "wealth_holdings", "knowledge", "notice"}
_CLAUSE = re.compile(r"[，,。.？?！!；;\n]|再|另外|然后|顺便")
# App 里的回复会说「详情见卡片」，IM 里没有卡片，这些话去掉：括号里的整个去掉，其余去掉所在的那一小句
_SEE_CARD_PAREN = re.compile(r"（[^（）]*见(下方)?(卡片|提示卡)[^（）]*）")
_SEE_CARD = re.compile(r"[，,]?[^，,。；！？（）]*见(下方)?(卡片|提示卡)[^，,。；！？（）]*")


@dataclass
class ImReply:
    text: str
    downgraded: bool = False  # 这条请求被降权：引导去 App 办理
    trace_id: str | None = None


def app_guide() -> str:
    return f"这项操作需要在言行 App 内确认，请打开言行 App 办理：{get_settings().app_h5_url}"


def wants_write(text: str) -> bool:
    """规则能认出来的写操作。认不出来的照常进 agent，由 im_turn 的事后检查兜底。"""
    if planner.action_kinds(text):  # 订花订蛋糕、心愿罐、定时转账、（多笔）转账
        return True
    for part in [text, *_CLAUSE.split(text)]:
        if not part.strip():
            continue
        it = rule_intent(part.strip())
        op = it.slots.get("op")
        if (it.intent in ("transfer", "aa") or it.intent == "card" and op != "list"
                or it.intent == "wealth" and op != "holdings" or it.intent == "sub" and op == "cancel"):
            return True
    return False


def bind(db: Session, channel: str, external_id: str, *, user_id: int, allowed: set[str] | frozenset = frozenset()
         ) -> ImBinding | None:
    """查 IM 账号绑定的银行用户；演示环境首次来消息时自动绑定到 user_id。allowed 非空时只认名单里的账号。"""
    if not external_id or allowed and external_id not in allowed:
        return None
    b = db.scalars(select(ImBinding).where(ImBinding.channel == channel, ImBinding.external_id == external_id)).first()
    if b is None:
        if db.get(User, user_id) is None:
            return None
        b = ImBinding(channel=channel, external_id=external_id, user_id=user_id)
        db.add(b)
        db.flush()
    return b


def im_turn(db: Session, *, channel: str, external_id: str, text: str, user_id: int,
            allowed: set[str] | frozenset = frozenset()) -> ImReply:
    """IM 渠道的一轮对话：绑定 → 降权检查 → agent → 事后检查 → 纯文本回复。"""
    b = bind(db, channel, external_id, user_id=user_id, allowed=allowed)
    if b is None:
        db.commit()
        return ImReply(NOT_BOUND)
    text = (text or "").strip()
    if not text:
        db.commit()
        return ImReply(TEXT_ONLY)
    if wants_write(text):
        trace_id = secrets.token_hex(8)
        audit.record(db, trace_id, "channel_downgraded", {"channel": channel, "stage": "intent",
                                                          "text": mask_pii(text)}, b.session_id)
        db.commit()
        return ImReply(app_guide(), downgraded=True, trace_id=trace_id)

    result = handle_turn(db, user_id=b.user_id, session_id=b.session_id, text=text, channel=channel)
    b.session_id = result.session_id
    actions = db.scalars(select(PendingAction).where(PendingAction.trace_id == result.trace_id)).all()
    other_cards = [c["type"] for c in result.cards if c["type"] not in READ_CARDS]
    session = db.get(ChatSession, result.session_id)
    asking = (session.state or {}).get("pending_question")  # 追问都是写操作的（「要转多少钱？」「锁哪张卡？」）
    if actions or other_cards or asking:
        for a in actions:
            if a.status in ACTIVE:
                cancel(db, action_id=a.id, user_id=a.user_id)
        if asking:  # 追问不留在 IM 会话里，下一句「500」不会接着办
            session.state = {k: v for k, v in session.state.items() if k != "pending_question"}
        audit.record(db, result.trace_id, "channel_downgraded", {
            "channel": channel, "stage": "reply", "cards": other_cards, "cancelled": [a.id for a in actions],
        }, result.session_id)
        db.commit()
        return ImReply(app_guide(), downgraded=True, trace_id=result.trace_id)
    db.commit()
    return ImReply(render(result.text, result.cards), trace_id=result.trace_id)


# ---------- 卡片转纯文本 ----------

def _card_lines(c: dict) -> list[str]:
    match c["type"]:
        case "balance":
            return [f"{a['name']}（尾号{a['tail']}）：{a['balance']} 元" for a in c["accounts"]]
        case "card_list":
            return [f"{x['name']}（尾号{x['tail']}）：{x['status_label']}" for x in c["cards"]]
        case "bill_txns":
            return [f"{i['time']} {i['name']} {i['amount']} 元" for i in c["items"][:3]]
        case "bill_categories":
            return [f"{r['category']} {r['amount']} 元，占 {r['share']}" for r in c["rows"][:3]]
        case "bill_anomalies":
            return [f"{i['time']} {i['name']} {i['amount']} 元：{i['reason']}" for i in c["items"][:3]]
        case "bill_report":
            return list(c.get("insights") or [])[:2]
        case "sub_overview" | "sub_upcoming":
            return [f"{i['name']} {i['amount']} 元/{i['cycle']}，下次 {i['next']}" for i in c["items"][:3]]
        case "wealth_holdings":
            return [f"{i['name']}：市值 {i['value']} 元，累计收益 {i['income']} 元" for i in c["items"][:3]]
        case "notice":
            return [c["text"]]
    return [c["reply"]] if c.get("reply") else []


def _no_card(line: str) -> str:
    line = _SEE_CARD.sub("", _SEE_CARD_PAREN.sub("", line.strip()))
    return re.sub(r"([。；！？])[。；]+", r"\1", line).lstrip("。；，")


def _clip(line: str, limit: int) -> str:
    """超长的一句按句号截断，截不出来就硬截加省略号。"""
    if len(line) <= limit:
        return line
    cut = line[:limit]
    end = max(cut.rfind(p) for p in "。！？；")
    return cut[:end + 1] if end > limit // 2 else cut[:limit - 1] + "…"


def render(text: str, cards: list[dict], limit: int = MAX_REPLY) -> str:
    """回复文字在前，卡片要点每行一条（文字里已经说过的不重复），总长不超过 limit，放不下的提示回 App 看。"""
    head = _no_card(text or "")
    lines = [head] if head else []
    for ln in (_no_card(ln) for c in cards for ln in _card_lines(c)):
        if ln and ln not in head and ln not in lines:
            lines.append(ln)
    out: list[str] = []
    for ln in lines:
        ln = mask_pii(ln)
        if len("\n".join([*out, ln])) <= limit:
            out.append(ln)
            continue
        if not out:
            out.append(_clip(ln, limit))
        out.append(MORE)
        break
    return "\n".join(out)
