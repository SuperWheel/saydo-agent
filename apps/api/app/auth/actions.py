"""待确认动作的生命周期：生成（预占额度）→ 用户确认 → 强认证 → 签发执行令牌 → 网关执行。

确认和认证都由用户端直接调用接口完成，不经过模型。
"""

import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..cards import auth_card, notice_card, receipt_card
from ..clock import now
from ..config import get_settings
from ..flags import kill_switch_on
from ..models import Account, AuthChallenge, Contact, LimitCounter, PendingAction
from ..money import fmt_yuan
from ..policy import Decision, PolicyContext, Risk, evaluate, load_rules
from ..tools import REGISTRY
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
    c.used_fen += a.args["amount_fen"]
    a.reserved_fen = 0


def expire_stale(db: Session, user_id: int) -> None:
    for a in db.scalars(select(PendingAction).where(
        PendingAction.user_id == user_id, PendingAction.status.in_(ACTIVE), PendingAction.expires_at < now()
    )):
        _release(db, a)
        a.status = "expired"
        audit.record(db, a.trace_id, "action_expired", {"action_id": a.id}, a.session_id)


def transfer_policy_context(db: Session, *, user_id: int, contact: Contact, amount_fen: int, channel: str,
                            injection_flag: bool, exclude: PendingAction | None = None) -> PolicyContext:
    acct = db.scalars(select(Account).where(Account.user_id == user_id, Account.kind == "debit")).one()
    c = _counter(db, user_id, now().date())
    outflow = c.used_fen + c.reserved_fen - (exclude.reserved_fen if exclude else 0)
    window = next(r["window_s"] for r in load_rules()["rules"] if r["id"] == "velocity")
    recent = db.scalar(select(func.count()).select_from(PendingAction).where(
        PendingAction.user_id == user_id, PendingAction.tool == "transfer.create",
        PendingAction.created_at >= now() - timedelta(seconds=window),
        PendingAction.status.not_in(("cancelled", "expired", "invalidated")),
    )) or 0
    return PolicyContext(
        tool="transfer.create", base_level=REGISTRY["transfer.create"].base_risk, amount_fen=amount_fen,
        balance_fen=acct.balance_fen, outflow_today_fen=outflow, payee_known=contact.first_transfer_at is not None,
        recent_transfer_count=recent - (1 if exclude else 0), channel=channel,
        kill_switch=kill_switch_on(db), injection_flag=injection_flag,
    )


# ---------- 生成 ----------

def create_transfer_action(db: Session, *, user_id: int, session_id: str | None, trace_id: str, contact: Contact,
                           amount_fen: int, memo: str, decision: Decision, injection_flag: bool) -> PendingAction:
    acct = db.scalars(select(Account).where(Account.user_id == user_id, Account.kind == "debit")).one()
    args = {"from_account_id": acct.id, "contact_id": contact.id, "account_no": contact.account_no,
            "amount_fen": amount_fen, "memo": memo}
    nonce = secrets.token_hex(8)
    action = PendingAction(
        id="A" + secrets.token_hex(8), user_id=user_id, session_id=session_id, trace_id=trace_id,
        tool="transfer.create", args=args, nonce=nonce, digest=compute_digest("transfer.create", args, user_id, nonce),
        display={"payee": contact.masked_name, "bank": contact.bank, "tail": contact.tail, "amount_fen": amount_fen,
                 "memo": memo, "cumulative_after_fen": decision.cumulative_after_fen, "injection_flag": injection_flag},
        level=decision.level.name, reasons=decision.reasons, reserved_fen=amount_fen, status="pending",
        expires_at=now() + timedelta(seconds=get_settings().action_ttl_s),
    )
    _counter(db, user_id, now().date()).reserved_fen += amount_fen  # 生成确认卡时就原子预占额度
    db.add(action)
    db.flush()
    audit.record(db, trace_id, "action_created", {
        "action_id": action.id, "tool": action.tool, "level": action.level, "reasons": action.reasons,
        "payee": contact.masked_name, "tail": contact.tail, "amount_fen": amount_fen, "digest": action.digest,
    }, session_id)
    return action


# ---------- 确认与认证 ----------

def _load(db: Session, action_id: str, user_id: int) -> PendingAction:
    expire_stale(db, user_id)
    a = db.scalars(select(PendingAction).where(PendingAction.id == action_id).with_for_update()).first()
    if a is None or a.user_id != user_id:
        raise ActionError(404, "动作不存在")
    return a


def _invalidate(db: Session, a: PendingAction, reason: str) -> Outcome:
    _release(db, a)
    a.status = "invalidated"
    audit.record(db, a.trace_id, "action_invalidated", {"action_id": a.id, "reason": reason}, a.session_id)
    return Outcome("invalidated", notice_card(f"{reason}。本次授权已作废，请重新发起。", "warning"))


def _revalidate(db: Session, a: PendingAction) -> Outcome | None:
    """执行前重新读取余额、额度、收款人状态；和生成确认卡时相比风险变高，就作废授权。"""
    contact = db.get(Contact, a.args["contact_id"])
    if contact is None or contact.account_no != a.args["account_no"]:
        return _invalidate(db, a, "收款人信息已变化")
    decision = evaluate(transfer_policy_context(
        db, user_id=a.user_id, contact=contact, amount_fen=a.args["amount_fen"], channel="app",
        injection_flag=a.display.get("injection_flag", False), exclude=a,
    ))
    audit.record(db, a.trace_id, "policy_revalidated", {"action_id": a.id, **decision.as_dict()}, a.session_id)
    if decision.block:
        return _invalidate(db, a, decision.block_reason or "策略拒绝")
    if decision.level > Risk[a.level]:
        return _invalidate(db, a, "风险等级已变化：" + "；".join(decision.reasons))
    return None


def _sms_preview(a: PendingAction, ch: AuthChallenge) -> str | None:
    if not get_settings().demo_mode or "sms" not in ch.factors_required:
        return None
    d = a.display
    return (f"【言行银行·模拟短信】您正在向{d['payee']}（尾号{d['tail']}）转账{fmt_yuan(d['amount_fen'])}元，"
            f"验证码{otp_for(a.digest, ch.otp_issued_at)}，5分钟内有效。如非本人操作，请勿告知他人。")


def _auth_outcome(a: PendingAction, ch: AuthChallenge, message: str | None = None) -> Outcome:
    left = load_rules()["auth"]["sms_max_attempts"] - ch.attempts
    return Outcome("awaiting_auth", auth_card(ch, a, sms_preview=_sms_preview(a, ch), attempts_left=left), message)


def confirm(db: Session, *, action_id: str, digest: str, user_id: int) -> Outcome:
    a = _load(db, action_id, user_id)
    if a.status == "expired":
        return Outcome("expired", notice_card("确认卡已过期，未发生资金变动。请重新发起。", "warning"))
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
        return Outcome("expired", notice_card("验证超时，本次转账已取消，未发生资金变动。", "warning"))
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
        if ch.attempts >= load_rules()["auth"]["sms_max_attempts"]:
            ch.status = "failed"
            _release(db, a)
            a.status = "cancelled"
            return Outcome("cancelled", notice_card("验证失败次数过多，本次转账已取消，未发生资金变动。", "error"))
        return _auth_outcome(a, ch, "验证未通过，请重试")

    ch.factors_passed = [*ch.factors_passed, factor]
    audit.record(db, a.trace_id, "auth_passed", {"challenge_id": ch.id, "factor": factor}, a.session_id)
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
    return Outcome("cancelled", notice_card("已取消，未发生资金变动。"))


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
        return Outcome("failed", notice_card(f"转账没有完成：{e}。未发生资金变动。", "error"))
    _settle(db, a)
    a.status = result["status"]
    a.result = result
    audit.record(db, a.trace_id, "execution", {"action_id": a.id, **result}, a.session_id)
    return Outcome(a.status, receipt_card(a, result))
