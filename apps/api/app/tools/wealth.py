"""理财（WS5）：风险测评 F-WM-01、推荐 02、对比 03、持仓与收益 04（🟢），申购 06 🔴、赎回 07 🟡/🔴。

- 适当性：产品风险等级 R 高于客户风险承受能力 C、没做测评或测评过期，一律拦截。拦截写在策略上下文的 block_reason 里，
  提议时和执行前重新校验用同一个构造器，写工具执行时再查一遍；不靠提示词。
- 测评：问卷卡由用户逐题点选，答案只能通过选择卡回传（loop 的 _selection_turn），模型没有提交答案的工具；
  算分由系统完成，结果经用户确认后生效。可购范围扩大（等级比上次高）时上浮 🔴。
- 赎回：默认 🟡；要收赎回费、按当前净值会亏损、单笔 ≥ 5 万元时上浮 🔴（rules.yaml 的 wealth_redeem_*）。
- 数字全部由后端计算，卡片按模板渲染；话术不承诺本金或收益，卡片在出口处再过一遍合规检查（agent/grounding.promise_words）。
"""

import math
import re
import secrets
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.grounding import promise_words
from ..auth.actions import PolicyEnv, base_context, propose_action
from ..cards import notice_card
from ..cards import action_display as _action_display
from ..clock import now
from ..core_mock import wealth as wm
from ..core_mock.ledger import LedgerError, post
from ..models import Account, RiskProfile, Transaction, WealthHolding, WealthProduct
from ..money import fmt_yuan, parse_amount
from ..nlu import amounts_in, card_amount_text
from ..policy import PolicyContext, Risk, load_rules
from . import ToolContext, ToolResult, bank_tool

SOURCE = "理财系统（模拟）"
DISCLAIMER = "产品均为虚构，数据为模拟。理财非存款，产品有风险，投资须谨慎；业绩比较基准和历史收益不代表未来表现。"
HIDDEN = "（该内容未通过话术合规检查，已隐藏）"
RISK_LABEL, LEVEL_LABEL = wm.RISK_LABEL, wm.LEVEL_LABEL

_PRODUCT_PARAM = {"type": "string", "description": "产品名，照抄用户原话，如「活钱宝」「安享90天」；对比时写上所有产品"}
_AMOUNT_PARAM = {"type": "string", "description": "金额照抄用户原话，如「1万」「五千」，不要换算；用户没说就不填"}
_DURATION = re.compile(r"([一二两三四五六七八九十半\d]+\s*个?(?:年|月|天|周|星期))")


# ---------- 合规出口 ----------

def scrub(obj):
    """卡片出口的合规检查：含「保本」「稳赚」这类承诺说法的字符串整段换成提示。模板本身不会出现，这是兜底。"""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return HIDDEN if isinstance(obj, str) and promise_words(obj) else obj


def action_display(**kw) -> dict:
    return scrub(_action_display(**kw))


# ---------- 查找 ----------

def _products(db: Session) -> list[WealthProduct]:
    return list(db.scalars(select(WealthProduct).order_by(WealthProduct.code)))


def _names(p: WealthProduct) -> list[str]:
    return sorted({p.name, p.name.removesuffix("（虚构）"), *p.aliases, p.code}, key=len, reverse=True)


def find_products(db: Session, text: str | None) -> list[WealthProduct]:
    """按在话里出现的先后顺序，找出提到的产品（名称、别名、代码）。"""
    hits = []
    for p in _products(db):
        pos = min((i for n in _names(p) if (i := (text or "").find(n)) >= 0), default=-1)
        if pos >= 0:
            hits.append((pos, p))
    return [p for _, p in sorted(hits, key=lambda x: x[0])]


def _clean(db: Session, text: str) -> str:
    """找金额前先去掉产品名（「安享90天」里的 90）并隔开期限（「5万一年内」不能读成 51000）。"""
    names = sorted({n for p in _products(db) for n in _names(p)}, key=len, reverse=True)
    for n in names:
        text = text.replace(n, " ")
    return _DURATION.sub(r" \1", text)


def amount_text_in(db: Session, text: str) -> str | None:
    return card_amount_text(_clean(db, text))


def _amount_from_user(db: Session, fen: int, texts: list[str]) -> bool:
    """金额必须能在用户最近说的话里找到（去掉产品名、期限之后），不能是模型自己编的。"""
    return any(fen in amounts_in(_clean(db, t)) for t in texts)


def horizon_days(text: str | None) -> int | None:
    """「一年内不用」→ 365，「半年」→ 180，「3个月」→ 90，「随时要用」→ 1。说不出期限就是 None。"""
    t = text or ""
    if re.search(r"随时|随用随取|活期|马上要用", t):
        return 1
    if "半年" in t:
        return 180
    for pat, unit in ((r"([一二两三四五六七八九十\d]+)\s*年", 365), (r"([一二两三四五六七八九十\d]+)\s*个?月", 30),
                      (r"([一二两三四五六七八九十\d]+)\s*(?:周|个?星期)", 7), (r"(\d+)\s*天", 1)):
        if (m := re.search(pat, t)) and (n := parse_amount(m.group(1))):
            return n // 100 * unit
    return None


def latest_profile(db: Session, user_id: int) -> RiskProfile | None:
    return db.scalars(select(RiskProfile).where(RiskProfile.user_id == user_id).order_by(RiskProfile.id.desc())).first()


def _valid(profile: RiskProfile | None) -> bool:
    return profile is not None and profile.expires_at > now()


def _profile_view(profile: RiskProfile | None) -> dict | None:
    if profile is None:
        return None
    return {"level": LEVEL_LABEL[profile.level], "level_n": profile.level, "score": profile.score,
            "assessed_on": f"{profile.assessed_at:%Y-%m-%d}", "expires_on": f"{profile.expires_at:%Y-%m-%d}",
            "valid": _valid(profile), "range": _range(profile.level)}


def _range(level: int) -> str:
    return "R1" if level == 1 else f"R1–R{level}"


def suitability_problem(p: WealthProduct, profile: RiskProfile | None) -> str | None:
    """适当性硬过滤：没测评、测评过期、产品风险等级高于客户承受能力，都不能申购。"""
    if profile is None:
        return "你还没有做风险测评，申购理财前需要先完成测评"
    if not _valid(profile):
        return f"你的风险测评已于 {profile.expires_at:%Y-%m-%d} 过期，申购理财前需要重新测评"
    if p.risk_level > profile.level:
        return (f"{p.name}的风险等级为 {RISK_LABEL[p.risk_level]}，高于你的风险承受能力 {LEVEL_LABEL[profile.level]}，"
                f"按适当性要求不能申购")
    return None


def _debit(db: Session, user_id: int) -> Account:
    return db.scalars(select(Account).where(Account.user_id == user_id, Account.kind == "debit")).one()


# ---------- 产品指标（全部由净值公式算出） ----------

def _today() -> date:
    return now().date()


def nav(p: WealthProduct, day: date | None = None) -> Decimal:
    return wm.nav_at(p.nav_model, p.inception_on, day or _today())


def _return_bp(p: WealthProduct, days: int) -> float:
    return float(nav(p) / nav(p, _today() - timedelta(days=days)) - 1) * 10_000


def _drawdown_bp(p: WealthProduct, days: int = 365) -> float:
    peak, worst = Decimal(0), 0.0
    for d in range(days, -1, -1):
        v = nav(p, _today() - timedelta(days=d))
        peak = max(peak, v)
        worst = max(worst, float(1 - v / peak) * 10_000)
    return worst


def yield_ref(p: WealthProduct) -> tuple[str, str, float]:
    """收益参考：(标签, 文字, 用于排序的基点数)。封闭式看业绩比较基准，现金管理类看 7 日年化，净值型看近一年。"""
    if p.benchmark_low_bp is not None:
        return ("业绩比较基准", f"{wm.pct(p.benchmark_low_bp)}–{wm.pct(p.benchmark_high_bp)}",
                (p.benchmark_low_bp + p.benchmark_high_bp) / 2)
    if p.risk_level == 1:
        bp = _return_bp(p, 7) * 365 / 7
        return "7日年化（示例）", wm.pct(round(bp)), bp
    bp = _return_bp(p, 365)
    return "近一年收益率（示例）", wm.pct(round(bp)), bp


def _term_label(p: WealthProduct) -> str:
    return "开放式，随时可赎回" if not p.term_days else f"封闭 {p.term_days} 天"


def _liquidity_label(p: WealthProduct) -> str:
    if p.term_days:
        return f"封闭期内不能赎回，到期后 T+{p.settle_days} 到账"
    return "随时赎回，实时到账" if p.settle_days == 0 else f"随时赎回，T+{p.settle_days} 到账"


def _fee_label(p: WealthProduct) -> str:
    sub = f"申购费 {wm.pct(p.sub_fee_bp)}" if p.sub_fee_bp else "免申购费"
    red = (f"持有不足 {p.redeem_fee_days} 天赎回收 {wm.pct(p.redeem_fee_bp)}" if p.redeem_fee_bp else "无赎回费")
    return f"{sub}；{red}"


def _risk_note(p: WealthProduct) -> str:
    if p.risk_level >= 4:
        return "净值波动大，可能出现较大亏损"
    if p.risk_level == 3:
        return "净值会有波动，可能出现亏损"
    return "收益不确定，业绩比较基准和示例收益不代表未来表现"


def _disclosure(p: WealthProduct) -> str:
    return (f"风险揭示：理财非存款，产品有风险，投资须谨慎。本产品为{p.kind}产品，风险等级 {RISK_LABEL[p.risk_level]}，"
            f"{_risk_note(p)}；不承诺本金和收益，{'封闭期内不能赎回' if p.term_days else '赎回按赎回当日净值计算'}。"
            f"产品为虚构，仅供演示。")


def _product_view(p: WealthProduct) -> dict:
    label, text, _ = yield_ref(p)
    return {"code": p.code, "name": p.name, "kind": p.kind, "risk": RISK_LABEL[p.risk_level], "risk_n": p.risk_level,
            "term": _term_label(p), "liquidity": _liquidity_label(p), "yield_label": label, "yield": text,
            "min_purchase": fmt_yuan(p.min_purchase_fen), "fee": _fee_label(p), "risk_note": _risk_note(p)}


def _meta() -> dict:
    return {"as_of": now().strftime("%Y-%m-%d %H:%M"), "source": SOURCE, "disclaimer": DISCLAIMER}


def _set_question(ctx: ToolContext, question: dict | None) -> None:
    state = dict(ctx.session.state or {})
    if question:
        state["pending_question"] = question
    elif (state.get("pending_question") or {}).get("kind") == "wealth":
        state.pop("pending_question", None)
    ctx.session.state = state  # 整体赋值，JSON 列才会被识别为已修改


# ---------- F-WM-01 风险测评 ----------

def _quiz(ctx: ToolContext, reason: str | None = None) -> ToolResult:
    """展示问卷卡。qid 记在会话里，回传答案时必须对上，答案只能来自用户在卡片上的点选。"""
    qid = secrets.token_hex(6)
    ctx.session.state = {**(ctx.session.state or {}), "risk_quiz": qid}
    profile = _profile_view(latest_profile(ctx.db, ctx.user_id))
    if reason:
        reply = reason + "。请在问卷卡上逐题选择。"
    elif profile and profile["valid"]:
        reply = f"你当前的风险承受能力是 {profile['level']}，有效期至 {profile['expires_on']}。重新测评请在问卷卡上逐题选择。"
    else:
        reply = "请在问卷卡上逐题选择，提交后系统按规则计算你的风险承受能力。"
    card = scrub({
        "type": "wealth_quiz", "qid": qid, "version": wm.QUIZ_VERSION, "questions": wm.quiz_view(), "profile": profile,
        "reason": reason, "reply": reply,
        "note": "请本人逐题选择，系统和 AI 都不会替你作答，也不会根据聊天内容推断答案。提交后按规则算出 C1–C5，"
                "由你确认后生效，有效期 12 个月。", **_meta()})
    return ToolResult({"status": "quiz_shown", "profile": profile, "reason": reason,
                       "note": "问卷卡已展示。答案只能由用户在卡片上逐题点选，你不能代填、不能根据聊天内容推断答案，"
                               "也不要把题目列出来让用户在对话里回答。"}, card)


@bank_tool(
    name="wealth.assessment", domain="wealth", base_risk=Risk.GREEN,
    description="风险测评：展示风险测评问卷卡（5 道题），并返回用户当前的风险承受能力（C1–C5）和有效期。"
                "用户要做测评、问自己的风险等级时调用。答案只能由用户在卡片上逐题点选，你不能代填，也不能根据聊天内容推断。",
    capability="理财风险测评", examples=("我想做个风险测评",),
)
def assessment(ctx: ToolContext) -> ToolResult:
    return _quiz(ctx)


def submit_quiz(ctx: ToolContext, selection: dict) -> ToolResult:
    """问卷卡回传的答案（用户在卡片上逐题点选）→ 系统算分 → 生成确认卡，确认后才保存。"""
    if not selection.get("qid") or selection.get("qid") != (ctx.session.state or {}).get("risk_quiz"):
        return ToolResult({"status": "quiz_expired"}, notice_card("这份问卷已经失效，请重新发起风险测评。", "warning"))
    answers = selection.get("answers")
    try:
        score, level, capped = wm.score_answers(answers)
    except wm.QuizError as e:
        return ToolResult({"status": "quiz_invalid", "message": str(e)}, notice_card(f"{e}。", "warning"))
    ctx.session.state = {k: v for k, v in (ctx.session.state or {}).items() if k != "risk_quiz"}  # 一份问卷只能提交一次
    prev = latest_profile(ctx.db, ctx.user_id)
    expires = now() + timedelta(days=wm.PROFILE_VALID_DAYS)
    rows = [["测评得分", f"{score} 分（满分 100）"], ["风险承受能力", LEVEL_LABEL[level]],
            ["可购买产品", f"风险等级 {_range(level)}"], ["有效期至", f"{expires:%Y-%m-%d}"]]
    if capped:
        by_score = min(5, max(1, math.ceil(score / 20)))
        rows.append(["说明", f"按得分为 {LEVEL_LABEL[by_score]}；「最大可承受亏损」一题是保守约束，按 {LEVEL_LABEL[level]} 确定"])
    if prev:
        rows.append(["上次测评", f"{LEVEL_LABEL[prev.level]}（{prev.assessed_at:%Y-%m-%d}，{'有效' if _valid(prev) else '已过期'}）"])
    display = action_display(title="风险测评结果确认", verb="保存测评结果", summary=f"保存风险测评结果：{LEVEL_LABEL[level]}",
                             rows=rows, fields={"op": "assess", "level": level, "score": score})
    args = {"answers": {k: answers[k] for k in sorted(answers)}, "version": wm.QUIZ_VERSION, "score": score, "level": level}
    return propose_action(ctx, tool="wealth.assess_save", args=args, display=display,
                          data={"score": score, "level": LEVEL_LABEL[level]})


def _assess_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    try:
        score, level, _ = wm.score_answers(args["answers"])
        same = (score, level, args["version"]) == (args["score"], args["level"], wm.QUIZ_VERSION)
        problem = None if same else "测评结果与答案不一致"
    except wm.QuizError as e:
        level, problem = 0, str(e)
    prev = latest_profile(db, env.user_id)
    return base_context(db, env, tool="wealth.assess_save", block_reason=problem,
                        facts={"op": "assess", "raises": prev is not None and level > prev.level})


@bank_tool(
    name="wealth.assess_save", domain="wealth", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    policy_context=_assess_context, description="保存风险测评结果。只能由工具网关凭执行令牌调用。",
)
def assess_save(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    profile = db.scalars(select(RiskProfile).where(RiskProfile.idempotency_key == idempotency_key)).first()
    if profile is None:  # 幂等：同一个动作只保存一次
        score, level, _ = wm.score_answers(args["answers"])  # 不信参数里的分数，重新算
        if (score, level) != (args["score"], args["level"]):
            raise LedgerError("测评结果与答案不一致")
        at = now()
        profile = RiskProfile(user_id=user_id, level=level, score=score, answers=args["answers"], version=args["version"],
                              assessed_at=at, expires_at=at + timedelta(days=wm.PROFILE_VALID_DAYS),
                              idempotency_key=idempotency_key)
        db.add(profile)
        db.flush()
    return {"status": "succeeded", "level": LEVEL_LABEL[profile.level],
            "receipt_rows": [["生效时间", f"{profile.assessed_at:%Y-%m-%d %H:%M:%S}"]]}


# ---------- F-WM-02 推荐 ----------

def _scores(cands: list[WealthProduct], level: int, horizon: int | None) -> dict[str, float]:
    """技术文档 §7.5：0.25 收益匹配 + 0.25 风险偏好匹配 + 0.20 流动性 + 0.20 期限 + 0.10 费率；
    1 个月内要用钱时流动性权重调到 0.35、收益降到 0.10。"""
    w_yield, w_risk, w_liq, w_term, w_fee = (0.10, 0.25, 0.35, 0.20, 0.10) if horizon and horizon <= 30 else (
        0.25, 0.25, 0.20, 0.20, 0.10)
    top = max((yield_ref(p)[2] for p in cands), default=0) or 1
    out = {}
    for p in cands:
        y = max(yield_ref(p)[2], 0) / top if top > 0 else 0
        r = 1 - (level - p.risk_level) / 4
        if p.term_days:
            liq = max(0.0, 0.5 - p.term_days / 800)
        else:
            liq = {0: 1.0, 1: 0.8}.get(p.settle_days, 0.7)
        term = (p.term_days / horizon if p.term_days else 0.6) if horizon else 0.5
        fee = 1 - min(1, (p.sub_fee_bp + p.redeem_fee_bp) / 200)
        out[p.code] = round(100 * (w_yield * y + w_risk * r + w_liq * liq + w_term * term + w_fee * fee))
    return out


def _why(p: WealthProduct, level: int, horizon: int | None) -> list[str]:
    label, text, _ = yield_ref(p)
    why = [f"风险等级 {RISK_LABEL[p.risk_level]}，在你的 {LEVEL_LABEL[level]} 承受范围内", f"{label} {text}"]
    if p.term_days and horizon:
        why.append(f"封闭 {p.term_days} 天，不超过你的资金使用期限")
    else:
        why.append(_liquidity_label(p))
    return why


def _diff(a: WealthProduct, b: WealthProduct) -> str:
    """前两名的差别：只陈述事实，不下「哪个更好」的结论。"""
    parts = []
    if a.risk_level != b.risk_level:
        lo, hi = (a, b) if a.risk_level < b.risk_level else (b, a)
        parts.append(f"{lo.name}风险等级更低（{RISK_LABEL[lo.risk_level]}），{hi.name}为 {RISK_LABEL[hi.risk_level]}")
    if (a.term_days, a.settle_days) != (b.term_days, b.settle_days):
        parts.append(f"流动性上，{a.name}{_liquidity_label(a)}，{b.name}{_liquidity_label(b)}")
    ya, yb = yield_ref(a), yield_ref(b)
    parts.append(f"收益参考上，{a.name}{ya[0]} {ya[1]}，{b.name}{yb[0]} {yb[1]}")
    return "；".join(parts) + "。"


@bank_tool(
    name="wealth.recommend", domain="wealth", base_risk=Risk.GREEN,
    description="理财产品推荐：只推荐风险等级不高于用户风险承受能力的在售产品，按收益、风险匹配、流动性、期限、费率打分，"
                "给出推荐理由、被过滤的产品和原因。用户问有什么理财、买什么好、闲钱怎么放时调用。"
                "没测评或测评过期时会展示问卷卡。不承诺收益，不要说保本、稳赚。",
    capability="理财产品推荐", examples=("有什么适合我的理财",),
    parameters={"type": "object", "properties": {
        "horizon_text": {"type": "string", "description": "这笔钱多久不用，照抄用户原话，如「一年内不用」「随时要用」；没说就不填"},
        "amount_text": _AMOUNT_PARAM,
    }},
)
def recommend(ctx: ToolContext, horizon_text: str | None = None, amount_text: str | None = None) -> ToolResult:
    db = ctx.db
    profile = latest_profile(db, ctx.user_id)
    if not _valid(profile):
        reason = (f"你的风险测评已于 {profile.expires_at:%Y-%m-%d} 过期，推荐理财前需要重新测评" if profile
                  else "推荐理财前需要先完成风险测评")
        return _quiz(ctx, reason)
    amount = parse_amount(amount_text) if amount_text else None
    if amount is not None and not _amount_from_user(db, amount, ctx.user_texts):
        amount = None  # 金额不是用户说的，不用它过滤
    horizon = horizon_days(horizon_text)
    balance = _debit(db, ctx.user_id).balance_fen
    funds = amount or balance
    cands, filtered = [], []
    for p in _products(db):
        if p.status != "on_sale":
            filtered.append({"name": p.name, "reason": "暂停销售"})
        elif p.risk_level > profile.level:
            filtered.append({"name": p.name, "reason": f"风险等级 {RISK_LABEL[p.risk_level]} 高于你的 {LEVEL_LABEL[profile.level]}"})
        elif p.min_purchase_fen > funds:
            filtered.append({"name": p.name, "reason": f"起购金额 {fmt_yuan(p.min_purchase_fen)} 元，高于"
                                                       f"{'你打算投入的金额' if amount else '可用余额'}"})
        elif horizon and p.term_days > horizon:
            filtered.append({"name": p.name, "reason": f"封闭 {p.term_days} 天，超过你的资金使用期限（约 {horizon} 天）"})
        elif horizon and p.redeem_fee_bp and p.redeem_fee_days > horizon:
            filtered.append({"name": p.name, "reason": f"持有不足 {p.redeem_fee_days} 天赎回要收 {wm.pct(p.redeem_fee_bp)} "
                                                       f"赎回费，不适合短期内要用的钱"})
        else:
            cands.append(p)
    scores = _scores(cands, profile.level, horizon)
    ranked = sorted(cands, key=lambda p: (-scores[p.code], p.code))[:3]
    items = [{**_product_view(p), "score": scores[p.code], "reasons": _why(p, profile.level, horizon)} for p in ranked]
    diff = _diff(ranked[0], ranked[1]) if len(ranked) >= 2 else ""
    if ranked:
        reply = (f"按你的风险承受能力 {LEVEL_LABEL[profile.level]}，筛出 {len(cands)} 款可购产品，"
                 f"排在前面的是{'、'.join(p.name for p in ranked[:2])}。")
    else:
        reply = f"按你的风险承受能力 {LEVEL_LABEL[profile.level]}和资金条件，暂时没有合适的在售产品。"
    if filtered:
        reply += f"另有 {len(filtered)} 款不符合条件，原因见卡片。"
    weights = ("流动性 35%、风险匹配 25%、期限 20%、收益 10%、费率 10%" if horizon and horizon <= 30
               else "收益 25%、风险匹配 25%、流动性 20%、期限 20%、费率 10%")
    card = scrub({"type": "wealth_recommend", "profile": _profile_view(profile), "items": items, "filtered": filtered,
                  "diff": diff, "weights": weights, "reply": reply,
                  "condition": "，".join(x for x in (f"约 {horizon} 天内不用" if horizon and horizon > 1 else
                                                    "随时可能要用" if horizon else "",
                                                    f"投入 {fmt_yuan(amount)} 元" if amount else "") if x),
                  **_meta()})
    return ToolResult({"profile": LEVEL_LABEL[profile.level], "summary": reply, "diff": diff,
                       "items": [{k: i[k] for k in ("name", "risk", "yield_label", "yield", "term", "score")} for i in items],
                       "filtered": filtered,
                       "note": "推荐卡已展示。不要承诺收益，不能说保本、稳赚；用户要买就调用 wealth_subscribe_propose。"}, card)


# ---------- F-WM-03 对比 ----------

@bank_tool(
    name="wealth.compare", domain="wealth", base_risk=Risk.GREEN,
    description="对比 2–3 款理财产品的收益参考、风险等级、期限、流动性、最大回撤、起购金额和费率，结论中立。"
                "用户问「A 和 B 哪个好」「有什么区别」时调用。",
    capability="理财产品对比", examples=("活钱宝和稳盈30天哪个好",),
    parameters={"type": "object", "properties": {"products_text": _PRODUCT_PARAM}, "required": ["products_text"]},
)
def compare(ctx: ToolContext, products_text: str) -> ToolResult:
    db = ctx.db
    ps = find_products(db, products_text)[:3]
    if len(ps) < 2:
        names = "、".join(p.name for p in _products(db))
        return ToolResult({"status": "need_product", "ask": "要对比哪两款产品？可以说产品名，如「活钱宝和稳盈30天」。",
                           "products": names})
    profile = latest_profile(db, ctx.user_id)
    ok = _valid(profile)

    def fit(p: WealthProduct) -> str:
        if not ok:
            return "完成风险测评后可知"
        return "在你的承受范围内" if p.risk_level <= profile.level else "超出你的风险承受能力，不能购买"

    views = [_product_view(p) for p in ps]
    rows = [
        {"label": "产品类型", "values": [p.kind for p in ps]},
        {"label": "风险等级", "values": [RISK_LABEL[p.risk_level] for p in ps]},
        {"label": "收益参考", "values": [f"{v['yield_label']} {v['yield']}" for v in views]},
        {"label": "近一年最大回撤（示例）", "values": [wm.pct(round(_drawdown_bp(p))) for p in ps]},
        {"label": "期限", "values": [_term_label(p) for p in ps]},
        {"label": "流动性", "values": [_liquidity_label(p) for p in ps]},
        {"label": "起购金额", "values": [f"{fmt_yuan(p.min_purchase_fen)} 元" for p in ps]},
        {"label": "费率", "values": [_fee_label(p) for p in ps]},
        {"label": "是否适合你", "values": [fit(p) for p in ps]},
    ]
    lo, hi = min(ps, key=lambda p: (p.risk_level, p.code)), max(ps, key=lambda p: (p.risk_level, p.code))
    parts = []
    if lo.risk_level != hi.risk_level:
        parts.append(f"{lo.name}风险等级更低、净值更稳，{hi.name}风险等级更高，净值波动也更大")
    liquid = [p for p in ps if not p.term_days]
    if liquid and len(liquid) < len(ps):
        parts.append(f"{'、'.join(p.name for p in liquid)}可以随时赎回，其余在封闭期内不能取出")
    conclusion = ("；".join(parts) + "。" if parts else "") + "没有绝对的好坏，取决于这笔钱多久要用、能承受多大波动。"
    reply = f"已对比{'和'.join(p.name for p in ps)}，各项指标见对比卡。没有绝对的好坏，取决于这笔钱多久要用、能承受多大波动。"
    card = scrub({"type": "wealth_compare", "products": [p.name for p in ps], "rows": rows, "conclusion": conclusion,
                  "profile": _profile_view(profile), "reply": reply, **_meta()})
    return ToolResult({"products": [{k: v[k] for k in ("name", "risk", "yield_label", "yield", "term", "liquidity")}
                                    for v in views],
                       "conclusion": conclusion, "note": "对比卡已展示。结论保持中立，不要替用户选，不能说保本、稳赚。"}, card)


# ---------- F-WM-04 持仓与收益 ----------

def _lot(db: Session, h: WealthHolding, p: WealthProduct) -> dict:
    today = _today()
    value = wm.value_fen(h.units_c, nav(p))
    month_base_day = today.replace(day=1) - timedelta(days=1)
    base = h.principal_fen if h.purchased_at.date() > month_base_day else wm.value_fen(h.units_c, nav(p, month_base_day))
    closed = h.matures_on is not None and today < h.matures_on
    return {"id": h.id, "code": p.code, "name": p.name, "risk": RISK_LABEL[p.risk_level], "principal_fen": h.principal_fen,
            "value_fen": value, "income_fen": value - h.principal_fen, "month_income_fen": value - base,
            "held_days": (today - h.purchased_at.date()).days, "purchased_on": f"{h.purchased_at:%Y-%m-%d}",
            "status": f"封闭中，{h.matures_on:%Y-%m-%d} 到期" if closed else "可赎回", "redeemable": not closed}


def _signed(fen: int) -> str:
    return ("+" if fen > 0 else "") + fmt_yuan(fen)


def _holding_lots(db: Session, user_id: int) -> list[tuple[WealthHolding, WealthProduct]]:
    rows = db.scalars(select(WealthHolding).where(WealthHolding.user_id == user_id, WealthHolding.status == "holding")
                      .order_by(WealthHolding.purchased_at, WealthHolding.id))
    return [(h, db.get(WealthProduct, h.product_code)) for h in rows]


@bank_tool(
    name="wealth.holdings", domain="wealth", base_risk=Risk.GREEN,
    description="查询理财持仓：每笔产品的本金、市值、累计收益、本月收益、是否在封闭期，以及合计。"
                "用户问「我的理财」「这个月赚了多少」「理财收益」时调用。",
    capability="查理财持仓和收益", examples=("我的理财这个月赚了多少",),
)
def holdings(ctx: ToolContext) -> ToolResult:
    lots = [_lot(ctx.db, h, p) for h, p in _holding_lots(ctx.db, ctx.user_id)]
    total = {k: sum(x[k] for x in lots) for k in ("principal_fen", "value_fen", "income_fen", "month_income_fen")}
    if lots:
        reply = (f"你持有 {len(lots)} 笔理财，市值合计 {fmt_yuan(total['value_fen'])} 元，"
                 f"本月收益 {_signed(total['month_income_fen'])} 元，累计收益 {_signed(total['income_fen'])} 元。")
    else:
        reply = "你目前没有持有理财产品。"
    items = [{"name": x["name"], "risk": x["risk"], "principal": fmt_yuan(x["principal_fen"]),
              "value": fmt_yuan(x["value_fen"]), "income": _signed(x["income_fen"]),
              "month_income": _signed(x["month_income_fen"]), "purchased_on": x["purchased_on"], "status": x["status"],
              "loss": x["income_fen"] < 0} for x in lots]
    card = scrub({"type": "wealth_holdings", "items": items, "total_value": fmt_yuan(total["value_fen"]),
                  "total_principal": fmt_yuan(total["principal_fen"]), "total_income": _signed(total["income_fen"]),
                  "month_income": _signed(total["month_income_fen"]), "month": f"{_today():%Y-%m}", "reply": reply,
                  "note": "收益按当日净值估算，赎回时以实际成交为准。", **_meta()})
    return ToolResult({"summary": reply, "holdings": [{k: v for k, v in x.items() if k != "id"} for x in lots], **total},
                      card)


# ---------- F-WM-06 申购 🔴 ----------

def _pick_product(ctx: ToolContext, text: str | None, pool: list[WealthProduct] | None = None,
                  verb: str = "申购") -> WealthProduct | ToolResult:
    found = find_products(ctx.db, text)
    if pool is not None:
        found = [p for p in found if p.code in {q.code for q in pool}]
    if len(found) == 1:
        return found[0]
    choices = pool if pool is not None else _products(ctx.db)
    names = "、".join(p.name.removesuffix("（虚构）") for p in (found or choices))
    if found:
        return ToolResult({"status": "need_product", "ask": f"要{verb}哪一款？{names}。"})
    if pool is not None and not pool:
        msg = "你目前没有可以赎回的理财持仓。"
        return ToolResult({"status": "not_applicable", "message": msg}, notice_card(msg))
    msg = (f"你没有持有这款产品。可以{verb}的有：{names}。" if pool is not None
           else f"没有找到这款产品。可以{verb}的有：{names}。")
    return ToolResult({"status": "product_not_found", "message": msg}, notice_card(msg, "warning"))


@bank_tool(
    name="wealth.subscribe_propose", domain="wealth", base_risk=Risk.GREEN,
    description="申购（购买）理财产品，生成带风险揭示的确认卡，用户确认后还要人脸 + 短信验证。"
                "系统会做适当性校验：没测评或测评过期先展示问卷卡；产品风险等级高于用户风险承受能力会被直接拦截，你无法改变。"
                "没说金额就不填 amount_text，系统会追问。",
    capability="申购理财", examples=("买1万安享90天",),
    parameters={"type": "object", "properties": {"product_text": _PRODUCT_PARAM, "amount_text": _AMOUNT_PARAM},
                "required": ["product_text"]},
)
def propose_subscribe(ctx: ToolContext, product_text: str, amount_text: str | None = None) -> ToolResult:
    db = ctx.db
    picked = _pick_product(ctx, product_text)
    if isinstance(picked, ToolResult):
        return picked
    p = picked
    profile = latest_profile(db, ctx.user_id)
    if not _valid(profile):
        return _quiz(ctx, suitability_problem(p, profile))
    if p.risk_level > profile.level:  # 没说金额时提前告知；有金额时由策略引擎拦截（见 _subscribe_context）
        if not amount_text:
            msg = suitability_problem(p, profile) + "。可以说「推荐理财」看看适合你的产品。"
            return ToolResult({"status": "blocked", "reason": msg}, notice_card(msg, "error"))
    amount = parse_amount(amount_text) if amount_text else None
    if amount is None:
        _set_question(ctx, {"kind": "wealth", "op": "subscribe", "code": p.code})
        return ToolResult({"status": "need_amount", "product": p.name,
                           "ask": f"买多少？{p.name}起购 {fmt_yuan(p.min_purchase_fen)} 元。"})
    _set_question(ctx, None)
    if not _amount_from_user(db, amount, ctx.user_texts):
        return ToolResult({"status": "amount_not_from_user", "message": "金额必须来自用户原话，请向用户确认金额"},
                          notice_card("请再说一下要买多少。", "warning"))
    acct = _debit(db, ctx.user_id)
    v = _product_view(p)
    fee = wm.bp_of(amount, p.sub_fee_bp)
    units = wm.units_for(amount - fee, nav(p))
    rows = [["产品", p.name], ["产品类型", p.kind], ["产品风险等级", RISK_LABEL[p.risk_level]],
            ["你的风险承受能力", f"{LEVEL_LABEL[profile.level]}（有效期至 {profile.expires_at:%Y-%m-%d}）"],
            ["期限", _term_label(p)], [v["yield_label"], v["yield"]],
            ["申购费", f"¥{fmt_yuan(fee)}" if fee else "免申购费"],
            ["份额确认", f"按当日净值 {nav(p):.4f} 确认，约 {Decimal(units) / 100:,.2f} 份"],
            ["扣款账户", f"{acct.name}（尾号{acct.card_tail}）"]]
    if p.term_days:
        rows.append(["到期日", f"{_today() + timedelta(days=p.term_days):%Y-%m-%d}，到期后 T+{p.settle_days} 到账"])
    else:
        rows.append(["赎回", _liquidity_label(p)])
    display = action_display(title="理财申购确认", verb="申购", summary=f"申购{p.name}{fmt_yuan(amount)}元", rows=rows,
                             amount_fen=amount, fields={"op": "subscribe", "product": p.code, "risk_level": p.risk_level,
                                                        "profile_level": profile.level})
    display["warning"] = scrub(_disclosure(p))
    args = {"product_code": p.code, "amount_fen": amount, "account_id": acct.id, "risk_level": p.risk_level}
    return propose_action(ctx, tool="wealth.subscribe", args=args, display=display, data={
        "product": p.name, "risk": RISK_LABEL[p.risk_level], "profile": LEVEL_LABEL[profile.level],
        "amount_fen": amount, "fee_fen": fee, "disclosure": display["warning"]})


def _subscribe_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    """申购的策略上下文：适当性（R ≤ C、测评有效）、在售、起购金额、扣款账户。提议和执行前重新校验都走这里。"""
    p = db.get(WealthProduct, args["product_code"])
    acct = db.get(Account, args["account_id"])
    if p is None or p.status != "on_sale":
        problem = "该产品暂停销售"
    elif acct is None or acct.user_id != env.user_id or acct.kind != "debit":
        problem = "扣款账户校验失败"
    elif args.get("risk_level") != p.risk_level:
        problem = "产品风险等级已调整，请重新发起申购"
    elif problem := suitability_problem(p, latest_profile(db, env.user_id)):
        pass
    elif args["amount_fen"] < p.min_purchase_fen:
        problem = f"{p.name}起购金额 {fmt_yuan(p.min_purchase_fen)} 元"
    return base_context(db, env, tool="wealth.subscribe", amount_fen=args["amount_fen"], block_reason=problem,
                        facts={"op": "subscribe", "product_risk": p.risk_level if p else 0})


@bank_tool(
    name="wealth.subscribe", domain="wealth", base_risk=Risk.RED, side_effect=True, llm_visible=False, moves_money=True,
    policy_context=_subscribe_context, description="申购理财。只能由工具网关凭执行令牌调用。",
)
def subscribe(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    p = db.get(WealthProduct, args["product_code"])
    h = db.scalars(select(WealthHolding).where(WealthHolding.idempotency_key == idempotency_key)).first()
    if h is None:  # 幂等：同一个动作只扣一次款、只记一笔持仓
        if p is None or (problem := suitability_problem(p, latest_profile(db, user_id))):  # 第三道：执行时再查适当性
            raise LedgerError(f"适当性校验未通过：{problem or '产品不存在'}")
        acct = db.get(Account, args["account_id"])
        if acct is None or acct.user_id != user_id:
            raise LedgerError("扣款账户校验失败")
        amount = args["amount_fen"]
        fee = wm.bp_of(amount, p.sub_fee_bp)
        post(db, account=acct, direction="out", amount_fen=amount, counterparty=f"理财申购·{p.name}",
             contra_code=f"WEALTH:{p.code}", category="理财", channel="agent", idempotency_key=idempotency_key)
        at = now()
        h = WealthHolding(user_id=user_id, product_code=p.code, units_c=wm.units_for(amount - fee, nav(p, at.date())),
                          principal_fen=amount, purchased_at=at, idempotency_key=idempotency_key,
                          matures_on=(at + timedelta(days=p.term_days)).date() if p.term_days else None)
        db.add(h)
        db.flush()
    txn = db.scalars(select(Transaction).where(Transaction.idempotency_key == idempotency_key)).one()
    rows = [["确认份额", f"{Decimal(h.units_c) / 100:,.2f} 份"], ["确认净值", f"{nav(p, h.purchased_at.date()):.4f}"]]
    if h.matures_on:
        rows.append(["到期日", f"{h.matures_on:%Y-%m-%d}"])
    rows += [["流水号", txn.txn_no], ["时间", f"{txn.created_at:%Y-%m-%d %H:%M:%S}"]]
    return {"status": "succeeded", "txn_no": txn.txn_no, "receipt_rows": rows}


# ---------- F-WM-07 赎回 🟡/🔴 ----------

def _lot_details(db: Session, p: WealthProduct, plan: list[list[int]], user_id: int, target_fen: int = 0) -> list[dict]:
    """按计划（[[持仓 id, 份额], ...]）和当日净值算每笔的赎回金额、成本、赎回费。持仓不符时抛 LedgerError。
    target_fen：按金额赎回时用户要的金额。份额向上取整会多出一两分钱，从最后一笔扣掉，赎回金额与用户说的一致。"""
    today, unit_nav, out = _today(), nav(p), []
    for lot_id, units in plan:
        h = db.get(WealthHolding, lot_id)
        if h is None or h.user_id != user_id or h.product_code != p.code or h.status != "holding" or h.units_c < units:
            raise LedgerError("持仓已变化")
        if h.matures_on and today < h.matures_on:
            raise LedgerError(f"{p.name}还在封闭期，{h.matures_on:%Y-%m-%d} 到期")
        gross = wm.value_fen(units, unit_nav)
        principal = h.principal_fen if units == h.units_c else int(
            (Decimal(h.principal_fen) * units / h.units_c).to_integral_value())
        held = (today - h.purchased_at.date()).days
        fee = wm.bp_of(gross, p.redeem_fee_bp) if p.redeem_fee_bp and held < p.redeem_fee_days else 0
        out.append({"lot": h, "units": units, "gross": gross, "principal": principal, "fee": fee, "held": held})
    if target_fen and out and (extra := sum(d["gross"] for d in out) - target_fen) > 0:
        last = out[-1]
        last["gross"] -= extra
        last["fee"] = wm.bp_of(last["gross"], p.redeem_fee_bp) if last["fee"] else 0
    return out


def _totals(details: list[dict]) -> dict:
    t = {k: sum(d[k] for d in details) for k in ("units", "gross", "principal", "fee")}
    return t | {"net": t["gross"] - t["fee"], "pnl": t["gross"] - t["principal"]}


def _rule(rule_id: str) -> dict:
    return next(r for r in load_rules()["rules"] if r["id"] == rule_id)


@bank_tool(
    name="wealth.redeem_propose", domain="wealth", base_risk=Risk.GREEN,
    description="赎回理财到本人借记卡，生成确认卡，展示赎回金额、赎回费、到账时间和盈亏。封闭期内不能赎回。"
                "要收赎回费、按当前净值会亏损、或金额较大时，系统会要求强验证。没说金额也没说全部就都不填，系统会追问。",
    capability="赎回理财", examples=("赎回1万活钱宝",),
    parameters={"type": "object", "properties": {
        "product_text": _PRODUCT_PARAM, "amount_text": _AMOUNT_PARAM,
        "redeem_all": {"type": "boolean", "description": "用户说全部赎回、都取出来时填 true"},
    }, "required": ["product_text"]},
)
def propose_redeem(ctx: ToolContext, product_text: str, amount_text: str | None = None,
                   redeem_all: bool = False) -> ToolResult:
    db = ctx.db
    lots = _holding_lots(db, ctx.user_id)
    held = list({p.code: p for _, p in lots}.values())
    picked = _pick_product(ctx, product_text, held, verb="赎回")
    if isinstance(picked, ToolResult):
        return picked
    p = picked
    mine = [h for h, q in lots if q.code == p.code]
    ready = [h for h in mine if not (h.matures_on and _today() < h.matures_on)]
    if not ready:
        due = min(h.matures_on for h in mine)
        msg = f"{p.name}还在封闭期，{due:%Y-%m-%d} 到期后自动赎回到你的账户，期间不能提前赎回。"
        return ToolResult({"status": "not_redeemable", "message": msg}, notice_card(msg))
    unit_nav = nav(p)
    available = sum(wm.value_fen(h.units_c, unit_nav) for h in ready)
    target = 0
    if redeem_all:
        plan = [[h.id, h.units_c] for h in ready]
    else:
        amount = parse_amount(amount_text) if amount_text else None
        if amount is None:
            _set_question(ctx, {"kind": "wealth", "op": "redeem", "code": p.code})
            return ToolResult({"status": "need_amount", "product": p.name, "available_fen": available,
                               "ask": f"赎回多少？{p.name}当前可赎回约 {fmt_yuan(available)} 元，也可以说「全部」。"})
        if not _amount_from_user(db, amount, ctx.user_texts):
            return ToolResult({"status": "amount_not_from_user", "message": "金额必须来自用户原话，请向用户确认金额"},
                              notice_card("请再说一下要赎回多少。", "warning"))
        if amount > available:
            msg = f"{p.name}当前最多可赎回约 {fmt_yuan(available)} 元，可以说「全部赎回」。"
            return ToolResult({"status": "over_available", "message": msg, "available_fen": available},
                              notice_card(msg, "warning"))
        target, need, plan = amount, wm.units_to_cover(amount, unit_nav), []
        for h in ready:  # 先买先赎
            if need <= 0:
                break
            take = min(h.units_c, need)
            plan.append([h.id, take])
            need -= take
    _set_question(ctx, None)
    acct = _debit(db, ctx.user_id)
    details = _lot_details(db, p, plan, ctx.user_id, target)
    t = _totals(details)
    whole = all(d["units"] == d["lot"].units_c for d in details) and len(details) == len(ready)
    rows = [["产品", p.name], ["赎回份额", f"{Decimal(t['units']) / 100:,.2f} 份（{'全部' if whole else '部分'}）"],
            ["参考净值", f"{unit_nav:.4f}（{_today():%Y-%m-%d}）"], ["赎回金额", f"¥{fmt_yuan(t['gross'])}"]]
    if t["fee"]:
        rows.append(["赎回费", f"¥{fmt_yuan(t['fee'])}（持有不足 {p.redeem_fee_days} 天，按 {wm.pct(p.redeem_fee_bp)} 收取）"])
    rows += [["这部分盈亏", f"{'亏损' if t['pnl'] < 0 else '收益'} ¥{fmt_yuan(abs(t['pnl']))}"],
             ["到账账户", f"{acct.name}（尾号{acct.card_tail}）"],
             ["到账时间", "实时到账" if p.settle_days == 0 else f"T+{p.settle_days}（模拟环境确认后即时入账）"]]
    display = action_display(title="理财赎回确认", verb="赎回", summary=f"赎回{p.name}，预计到账{fmt_yuan(t['net'])}元",
                             rows=rows, amount_fen=t["net"], fields={"op": "redeem", "product": p.code})
    if t["pnl"] < 0 or t["fee"]:
        display["warning"] = "提示：" + "；".join(x for x in (
            f"按当前净值赎回，这部分亏损 ¥{fmt_yuan(-t['pnl'])}" if t["pnl"] < 0 else "",
            f"提前赎回要收赎回费 ¥{fmt_yuan(t['fee'])}" if t["fee"] else "") if x) + "。赎回后不能撤销。"
    args = {"product_code": p.code, "lots": plan, "target_fen": target, "account_id": acct.id}
    return propose_action(ctx, tool="wealth.redeem", args=args, display=display, data={
        "product": p.name, "gross_fen": t["gross"], "fee_fen": t["fee"], "net_fen": t["net"], "pnl_fen": t["pnl"]})


def _redeem_context(db: Session, args: dict, env: PolicyEnv) -> PolicyContext:
    """赎回的策略上下文：持仓没变、不在封闭期；按当日净值重新算赎回费、盈亏和金额，决定要不要上浮 🔴。"""
    p = db.get(WealthProduct, args["product_code"])
    acct = db.get(Account, args["account_id"])
    facts, problem = {"op": "redeem"}, None
    if p is None or acct is None or acct.user_id != env.user_id or acct.kind != "debit":
        problem = "产品或到账账户校验失败"
    else:
        try:
            t = _totals(_lot_details(db, p, args["lots"], env.user_id, args["target_fen"]))
            facts |= {"early_fee": t["fee"] > 0, "at_loss": t["pnl"] < 0,
                      "large": t["gross"] >= _rule("wealth_redeem_large")["min_fen"]}
        except LedgerError as e:
            problem = str(e)
    return base_context(db, env, tool="wealth.redeem", block_reason=problem, facts=facts)


@bank_tool(
    name="wealth.redeem", domain="wealth", base_risk=Risk.YELLOW, side_effect=True, llm_visible=False,
    policy_context=_redeem_context, description="赎回理财到本人账户。只能由工具网关凭执行令牌调用。",
)
def redeem(db: Session, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    p = db.get(WealthProduct, args["product_code"])
    txn = db.scalars(select(Transaction).where(Transaction.idempotency_key == idempotency_key)).first()
    if txn is None:  # 幂等：同一个动作只入账一次
        acct = db.get(Account, args["account_id"])
        if p is None or acct is None or acct.user_id != user_id:
            raise LedgerError("产品或到账账户校验失败")
        details = _lot_details(db, p, args["lots"], user_id, args["target_fen"])
        t = _totals(details)
        for d in details:
            h = d["lot"]
            h.units_c -= d["units"]
            h.principal_fen -= d["principal"]
            if h.units_c == 0:
                h.status, h.principal_fen = "redeemed", 0
        txn = post(db, account=acct, direction="in", amount_fen=t["net"], counterparty=f"理财赎回·{p.name}",
                   contra_code=f"WEALTH:{p.code}", category="理财", channel="agent", idempotency_key=idempotency_key,
                   memo=f"赎回金额{fmt_yuan(t['gross'])}元，赎回费{fmt_yuan(t['fee'])}元")
    return {"status": "succeeded", "txn_no": txn.txn_no,
            "receipt_rows": [["实际到账", f"¥{fmt_yuan(txn.amount_fen)}"], ["流水号", txn.txn_no],
                             ["时间", f"{txn.created_at:%Y-%m-%d %H:%M:%S}"]]}


# ---------- 规则兜底（模型不可用时）----------

_ALL = re.compile(r"全部|全赎|都赎|所有|全都|清仓")


def rule_turn(ctx: ToolContext, intent, text: str) -> str | None:
    """理财意图，以及「买多少」「赎回多少」追问的回答。不是理财相关的话返回 None。"""
    db = ctx.db
    if intent.intent == "wealth":
        match intent.slots["op"]:
            case "assess":
                r = assessment(ctx)
            case "recommend":
                r = recommend(ctx, horizon_text=text, amount_text=amount_text_in(db, text))
            case "compare":
                r = compare(ctx, text)
            case "holdings":
                r = holdings(ctx)
            case "subscribe":
                r = propose_subscribe(ctx, text, amount_text_in(db, text))
            case "redeem":
                r = propose_redeem(ctx, text, amount_text_in(db, text), redeem_all=bool(_ALL.search(text)))
            case _:
                return None
        return _rule_text(ctx, r)
    pq = (ctx.session.state or {}).get("pending_question") or {}
    if intent.intent != "unknown" or pq.get("kind") != "wealth":
        return None
    p = db.get(WealthProduct, pq["code"])
    amount = amount_text_in(db, text)
    if pq["op"] == "redeem" and _ALL.search(text):
        return _rule_text(ctx, propose_redeem(ctx, p.name, redeem_all=True))
    if not amount:
        return None
    if pq["op"] == "subscribe":
        return _rule_text(ctx, propose_subscribe(ctx, p.name, amount))
    return _rule_text(ctx, propose_redeem(ctx, p.name, amount))


def _rule_text(ctx: ToolContext, r: ToolResult) -> str:
    if r.card:
        ctx.cards.append(r.card)
    status = r.data.get("status")
    if status in ("need_amount", "need_product"):
        return r.data["ask"]
    if status == "pending_confirmation":
        red = "这一步需要完成人脸和短信验证。" if r.data["level"] == "RED" else ""
        lead = "请先阅读确认卡上的风险揭示，核对无误后点击确认。" if "disclosure" in r.data else "请核对确认卡，无误后点击确认。"
        return lead + red
    if r.card and r.card["type"] == "notice":
        return r.card["text"]
    return (r.card or {}).get("reply") or r.data.get("message") or "好的。"
