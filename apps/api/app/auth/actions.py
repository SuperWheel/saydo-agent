"""待确认动作的生命周期：生成（预占额度）→ 用户确认 → 强认证 → 签发执行令牌 → 网关执行。

确认和认证都由用户端直接调用接口完成，不经过模型。所有写工具共用这一条路径：
提议工具调用 propose_action(ctx, tool=..., args=..., display=...)，其余步骤不用再写。
"""

import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..cards import auth_card, confirm_card, notice_card, receipt_card
from ..clock import now
from ..config import get_settings
from ..flags import kill_switch_on
from ..models import Account, AuthChallenge, LimitCounter, PendingAction
from ..policy import Decision, PolicyContext, Risk, evaluate, load_rules
from ..security import breaker
from ..tools import REGISTRY, ToolContext, ToolResult
from ..tools.gateway import GatewayError, execute_write
from .tokens import compute_digest, issue_execution_token, otp_for

ACTIVE = ("pending", "awaiting_auth")


class ActionError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Outcome:
    status: str
    card: dict
    message: str | None = None


# ---------- 额度 ----------

def _counter(db: Session, user_id: int, day) -> LimitCounter:
    c = db.scalars(
        select(LimitCounter).where(LimitCounter.user_id == user_id, LimitCounter.day == day).with_for_update()
    ).first()
    if c is None:
        c = LimitCounter(user_id=user_id, day=day, used_fen=0, reserved_fen=0)
        db.add(c)
        db.flush()
    return c


def _release(db: Session, a: PendingAction) -> None:
    if a.reserved_fen:
        _counter(db, a.user_id, a.created_at.date()).reserved_fen -= a.reserved_fen
        a.reserved_fen = 0


def _settle(db: Session, a: PendingAction) -> None:
    c = _counter(db, a.user_id, a.created_at.date())
    c.reserved_fen -= a.reserved_fen
    c.used_fen += a.reserved_fen  # 预占额 = 本次转出额
    a.reserved_fen = 0


def expire_stale(db: Session, user_id: int) -> None:
    for a in db.scalars(select(PendingAction).where(
        PendingAction.user_id == user_id, PendingAction.status.in_(ACTIVE), PendingAction.expires_at < now()
    )):
        _release(db, a)
        a.status = "expired"
        audit.record(db, a.trace_id, "action_expired", {"action_id": a.id}, a.session_id)


# ---------- 策略上下文 ----------

@dataclass
class PolicyEnv:
    user_id: int
    channel: str = "app"
    injection_flag: bool = False
    exclude: PendingAction | None = None  # 执行前重新校验时，排除动作自身的预占和计数


def base_context(db: Session, env: PolicyEnv, *, tool: str, amount_fen: int = 0, **extra) -> PolicyContext:
    """所有写工具共用的上下文：余额、今日累计转出（含预占）、同一工具的近期频次、熔断开关。
    工具特有的内容（收款人是否首次、facts）由各自的构造器通过 extra 传入。"""
    spec = REGISTRY[tool]
    acct = db.scalars(select(Account).where(Account.user_id == env.user_id, Account.kind == "debit")).one()
    c = _counter(db, env.user_id, now().date())
    outflow = c.used_fen + c.reserved_fen - (env.exclude.reserved_fen if env.exclude else 0)
    window = next((r["window_s"] for r in load_rules()["rules"] if r["id"] == "velocity"), 300)
    recent = db.scalar(select(func.count()).select_from(PendingAction).where(
        PendingAction.user_id == env.user_id, PendingAction.tool == tool,
        PendingAction.created_at >= now() - timedelta(seconds=window),
        PendingAction.status.not_in(("cancelled", "expired", "invalidated")),
    )) or 0
    return PolicyContext(
        tool=tool, base_level=spec.base_risk, moves_money=spec.moves_money, amount_fen=amount_fen,
        balance_fen=acct.balance_fen, outflow_today_fen=outflow, recent_count=recent - (1 if env.exclude else 0),
        channel=env.channel, kill_switch=kill_switch_on(db), stop_loss=spec.stop_loss,
        injection_flag=env.injection_flag, **extra,
    )


def decide(db: Session, tool: str, args: dict, env: PolicyEnv) -> Decision:
    spec = REGISTRY[tool]
    if spec.policy_context:
        ctx = spec.policy_context(db, args, env)
    else:
        ctx = base_context(db, env, tool=tool, amount_fen=args.get("amount_fen", 0) if spec.moves_money else 0)
    return evaluate(ctx)


# ---------- 生成 ----------

def propose_action(ctx: ToolContext, *, tool: str, args: dict, display: dict, data: dict | None = None) -> ToolResult:
    """提议工具的统一出口：定级 → 拦截或生成待确认动作 → 返回确认卡。data 是给模型看的补充信息（已脱敏）。"""
    db = ctx.db
    expire_stale(db, ctx.user_id)
    if (blocked := breaker.check_propose(ctx, tool, data)) is not None:  # 熔断：只读 / 暂停 / 转人工，止损类放行
        return blocked
    decision = decide(db, tool, args, PolicyEnv(ctx.user_id, ctx.channel, ctx.injection_flag))
    audit.record(db, ctx.trace_id, "policy_decision", {"tool": tool, "summary": display["summary"],
                                                       **decision.as_dict()}, ctx.session.id)
    if decision.block:
        return ToolResult({**(data or {}), "status": "blocked", "reason": decision.block_reason},
                          notice_card(decision.block_reason or "该操作被拒绝", "error"))
    action = create_action(db, user_id=ctx.user_id, session_id=ctx.session.id, trace_id=ctx.trace_id, tool=tool,
                           args=args, display=display, decision=decision, injection_flag=ctx.injection_flag)
    return ToolResult({
        **(data or {}), "status": "pending_confirmation", "level": decision.level.name,
        "level_label": decision.level.label, "reasons": decision.reasons,
        "note": "确认卡已展示给用户。用户点击确认并完成验证之前不会执行，不要说已经完成。",
    }, confirm_card(action))


def create_action(db: Session, *, user_id: int, session_id: str | None, trace_id: str, tool: str, args: dict,
                  display: dict, decision: Decision, injection_flag: bool = False) -> PendingAction:
    spec = REGISTRY[tool]
    if not spec.side_effect:
        raise ValueError(f"{tool} 不是写工具")
    reserve = int(args["amount_fen"]) if spec.moves_money else 0
    display = {**display, "injection_flag": injection_flag}
    if spec.moves_money:
        display["cumulative_after_fen"] = decision.cumulative_after_fen
    nonce = secrets.token_hex(8)
    action = PendingAction(
        id="A" + secrets.token_hex(8), user_id=user_id, session_id=session_id, trace_id=trace_id,
        tool=tool, args=args, nonce=nonce, digest=compute_digest(tool, args, user_id, nonce), display=display,
        level=decision.level.name, reasons=decision.reasons, reserved_fen=reserve, status="pending",
        expires_at=now() + timedelta(seconds=get_settings().action_ttl_s),
    )
    if reserve:
        _counter(db, user_id, now().date()).reserved_fen += reserve  # 生成确认卡时就原子预占额度
    db.add(action)
    db.flush()
    audit.record(db, trace_id, "action_created", {
        "action_id": action.id, "tool": tool, "level": action.level, "reasons": action.reasons,
        "summary": display["summary"], "reserved_fen": reserve, "digest": action.digest,
    }, session_id)
    return action


# ---------- 确认与认证 ----------

def _load(db: Session, action_id: str, user_id: int) -> PendingAction:
    expire_stale(db, user_id)
    a = db.scalars(select(PendingAction).where(PendingAction.id == action_id).with_for_update()).first()
    if a is None or a.user_id != user_id:
        raise ActionError(404, "动作不存在")
    return a


def _unchanged(a: PendingAction) -> str:
    return "未发生资金变动" if REGISTRY[a.tool].moves_money else "账户设置没有变化"


def _invalidate(db: Session, a: PendingAction, reason: str) -> Outcome:
    _release(db, a)
    a.status = "invalidated"
    audit.record(db, a.trace_id, "action_invalidated", {"action_id": a.id, "reason": reason}, a.session_id)
    return Outcome("invalidated", notice_card(f"{reason}。本次授权已作废，请重新发起。", "warning"))


def _revalidate(db: Session, a: PendingAction) -> Outcome | None:
    """执行前用同一个构造器重新取上下文（余额、额度、收款人状态等）；风险比生成确认卡时高，就作废授权。"""
    if (block := breaker.check(db, user_id=a.user_id, tool=a.tool, session_id=a.session_id)) is not None:
        _invalidate(db, a, "安全保护：" + block.reason)
        return Outcome("invalidated", block.card)
    decision = decide(db, a.tool, a.args, PolicyEnv(a.user_id, "app", a.display.get("injection_flag", False), exclude=a))
    audit.record(db, a.trace_id, "policy_revalidated", {"action_id": a.id, **decision.as_dict()}, a.session_id)
    if decision.block:
        return _invalidate(db, a, decision.block_reason or "策略拒绝")
    if decision.level > Risk[a.level]:
        return _invalidate(db, a, "风险等级已变化：" + "；".join(decision.reasons))
    return None


def _sms_preview(a: PendingAction, ch: AuthChallenge) -> str | None:
    if not get_settings().demo_mode or "sms" not in ch.factors_required:
        return None
    return (f"【言行银行·模拟短信】您正在{a.display['summary']}，"
            f"验证码{otp_for(a.digest, ch.otp_issued_at)}，5分钟内有效。如非本人操作，请勿告知他人。")


def _auth_outcome(a: PendingAction, ch: AuthChallenge, message: str | None = None) -> Outcome:
    left = load_rules()["auth"]["sms_max_attempts"] - ch.attempts
    return Outcome("awaiting_auth", auth_card(ch, a, sms_preview=_sms_preview(a, ch), attempts_left=left), message)


def confirm(db: Session, *, action_id: str, digest: str, user_id: int) -> Outcome:
    a = _load(db, action_id, user_id)
    if a.status == "expired":
        return Outcome("expired", notice_card(f"确认卡已过期，{_unchanged(a)}。请重新发起。", "warning"))
    if a.status != "pending":
        raise ActionError(409, f"当前状态为 {a.status}，不能再次确认")
    if not hmac.compare_digest(digest, a.digest):
        audit.record(db, a.trace_id, "digest_mismatch", {"action_id": a.id}, a.session_id)
        raise ActionError(409, "确认内容与原动作不一致，已拒绝")
    audit.record(db, a.trace_id, "action_confirmed", {"action_id": a.id, "level": a.level}, a.session_id)
    if (bad := _revalidate(db, a)) is not None:
        return bad
    if a.level == Risk.RED.name:
        ch = AuthChallenge(id="C" + secrets.token_hex(8), action_id=a.id,
                           factors_required=list(load_rules()["auth"]["RED"]), factors_passed=[], otp_issued_at=now())
        db.add(ch)
        a.status = "awaiting_auth"
        a.expires_at = max(a.expires_at, now() + timedelta(seconds=get_settings().otp_window_s))
        db.flush()
        audit.record(db, a.trace_id, "auth_challenge", {"action_id": a.id, "challenge_id": ch.id,
                                                        "factors": ch.factors_required}, a.session_id)
        return _auth_outcome(a, ch)
    return _execute(db, a)


def verify_factor(db: Session, *, challenge_id: str, factor: str, value: str, user_id: int) -> Outcome:
    ch = db.get(AuthChallenge, challenge_id)
    if ch is None:
        raise ActionError(404, "认证不存在")
    a = _load(db, ch.action_id, user_id)
    if a.status == "expired":
        return Outcome("expired", notice_card(f"验证超时，本次{a.display['verb']}已取消，{_unchanged(a)}。", "warning"))
    if a.status != "awaiting_auth" or ch.status != "pending":
        raise ActionError(409, "当前不在等待验证的状态")
    if factor not in ch.factors_required:
        raise ActionError(400, "不需要这个验证因子")
    if factor in ch.factors_passed:
        return _auth_outcome(a, ch)

    if factor == "face":  # 模拟活体检测：断言必须绑定本笔交易的 digest
        ok = hmac.compare_digest(value, f"face-ok:{a.digest}")
    else:
        fresh = now() - ch.otp_issued_at <= timedelta(seconds=get_settings().otp_window_s)
        ok = fresh and hmac.compare_digest(value.strip(), otp_for(a.digest, ch.otp_issued_at))
    if not ok:
        ch.attempts += 1
        audit.record(db, a.trace_id, "auth_failed", {"challenge_id": ch.id, "factor": factor,
                                                     "attempts": ch.attempts}, a.session_id)
        if (block := breaker.on_auth_failed(db, a)) is not None:  # 触发熔断：本次直接取消
            ch.status = "failed"
            _release(db, a)
            a.status = "cancelled"
            return Outcome("cancelled", block.card)
        if ch.attempts >= load_rules()["auth"]["sms_max_attempts"]:
            ch.status = "failed"
            _release(db, a)
            a.status = "cancelled"
            return Outcome("cancelled", notice_card(
                f"验证失败次数过多，本次{a.display['verb']}已取消，{_unchanged(a)}。", "error"))
        return _auth_outcome(a, ch, "验证未通过，请重试")

    ch.factors_passed = [*ch.factors_passed, factor]
    audit.record(db, a.trace_id, "auth_passed", {"challenge_id": ch.id, "factor": factor}, a.session_id)
    breaker.on_auth_passed(db, a)  # 「连续失败」从这里重新计数
    if set(ch.factors_passed) < set(ch.factors_required):
        return _auth_outcome(a, ch)
    ch.status = "passed"
    if (bad := _revalidate(db, a)) is not None:
        return bad
    return _execute(db, a)


def cancel(db: Session, *, action_id: str, user_id: int) -> Outcome:
    a = _load(db, action_id, user_id)
    if a.status not in ACTIVE:
        raise ActionError(409, f"当前状态为 {a.status}，不能取消")
    _release(db, a)
    a.status = "cancelled"
    audit.record(db, a.trace_id, "action_cancelled", {"action_id": a.id}, a.session_id)
    return Outcome("cancelled", notice_card(f"已取消，{_unchanged(a)}。"))


def _execute(db: Session, a: PendingAction) -> Outcome:
    token, jti = issue_execution_token(a.id, a.digest)
    a.token_jti = jti
    a.status = "executing"
    audit.record(db, a.trace_id, "token_issued", {"action_id": a.id, "jti": jti}, a.session_id)
    try:
        result = execute_write(a, token, db)
    except GatewayError as e:
        _release(db, a)
        a.status = "failed"
        a.result = {"status": "failed", "error": str(e)}
        audit.record(db, a.trace_id, "execution_failed", {"action_id": a.id, "error": str(e)}, a.session_id)
        return Outcome("failed", notice_card(f"{a.display['verb']}没有完成：{e}。{_unchanged(a)}。", "error"))
    _settle(db, a)
    a.status = result["status"]
    a.result = result
    audit.record(db, a.trace_id, "execution", {"action_id": a.id, **result}, a.session_id)
    return Outcome(a.status, receipt_card(a, result))
