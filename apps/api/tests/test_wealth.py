"""理财（WS5）：测评问卷只能由用户逐题点选、适当性硬拦截（提议 + 执行前重新校验 + 执行时三处）、申购 🔴 + 风险揭示、
赎回 🟡/🔴 分级、推荐 / 对比 / 持仓的数字由后端计算、话术不承诺本金或收益。默认走规则兜底模式，模型路径用脚本化的假模型。"""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agent.grounding import check, collect_facts, promise_words
from app.auth.actions import PolicyEnv, decide
from app.clock import now
from app.core_mock import wealth as wm
from app.core_mock.ledger import LedgerError
from app.db import new_session
from app.models import PendingAction, RiskProfile, WealthHolding, WealthProduct
from app.nlu import rule_intent
from app.policy import Risk
from app.tools import REGISTRY, wealth

from .conftest import confirm, say, text, tool
from .helpers import BALANCE, balance, events, ledger_ok, pass_auth

C3 = {"q1": "b", "q2": "b", "q3": "c", "q4": "c", "q5": "c"}  # 64 分，按分数是 C4，「最大亏损」一题压到 C3
C2 = {"q1": "d", "q2": "b", "q3": "b", "q4": "b", "q5": "b"}  # 32 分
C5 = {"q1": "b", "q2": "d", "q3": "e", "q4": "d", "q5": "e"}  # 100 分
SEED_C4 = {"q1": "b", "q2": "c", "q3": "d", "q4": "c", "q5": "d"}  # 种子里那条已过期的测评


def only(r: dict) -> dict:
    assert len(r["cards"]) == 1, r["cards"]
    return r["cards"][0]


def strings(obj) -> list[str]:
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings(v)]
    return [obj] if isinstance(obj, str) else []


def compliant(*objs) -> bool:
    return not any(promise_words(s) for o in objs for s in strings(o))


def submit(client, sid: str, qid: str, answers: dict) -> dict:
    return say(client, "", sid, selection={"type": "risk_answers", "qid": qid, "answers": answers})


def assess(client, answers: dict = C3, sid: str | None = None) -> str:
    """走一遍测评：出问卷 → 逐题回传 → 确认（等级比上次高时还要强验证）。返回会话 id。"""
    r = say(client, "我想做个风险测评", sid)
    quiz = only(r)
    card = only(submit(client, r["session_id"], quiz["qid"], answers))
    assert card["type"] == "confirm"
    out = confirm(client, card).json()
    if out["status"] == "awaiting_auth":
        out = pass_auth(client, out["card"])
    assert out["status"] == "succeeded", out
    return r["session_id"]


def profile_count() -> int:
    with new_session() as db:
        return len(db.scalars(select(RiskProfile)).all())


def holding_of(code: str) -> list[WealthHolding]:
    with new_session() as db:
        return list(db.scalars(select(WealthHolding).where(WealthHolding.product_code == code,
                                                         WealthHolding.status == "holding")))


def set_profile(level: int, *, days_ago: int = 0) -> None:
    with new_session() as db:
        at = now() - timedelta(days=days_ago)
        db.add(RiskProfile(user_id=1, level=level, score=level * 20, answers={}, version=wm.QUIZ_VERSION,
                           assessed_at=at, expires_at=at + timedelta(days=wm.PROFILE_VALID_DAYS)))
        db.commit()


# ---------- 种子与测评规则 ----------

def test_seed_products_cover_r1_to_r5_and_are_fictional(client):
    with new_session() as db:
        ps = db.scalars(select(WealthProduct)).all()
        assert 6 <= len(ps) <= 8
        assert {p.risk_level for p in ps} == {1, 2, 3, 4, 5}
        assert all(p.name.endswith("（虚构）") for p in ps)
        assert {p.term_days for p in ps} >= {0, 30, 90, 365}  # 活期到 365 天
        assert compliant([p.name for p in ps], [p.intro for p in ps], [p.aliases for p in ps])


@pytest.mark.parametrize("answers,score,level,capped", [
    (C3, 64, 3, True),
    (C2, 32, 2, False),
    (C5, 100, 5, False),
    (SEED_C4, 79, 4, False),
    ({"q1": "b", "q2": "d", "q3": "e", "q4": "d", "q5": "a"}, 80, 1, True),  # 不能接受任何亏损：一票压到 C1
    ({"q1": "e", "q2": "a", "q3": "a", "q4": "a", "q5": "a"}, 0, 1, False),
])
def test_quiz_scoring(answers, score, level, capped):
    assert wm.score_answers(answers) == (score, level, capped)


@pytest.mark.parametrize("answers", [
    {"q1": "b", "q2": "b", "q3": "c", "q4": "c"},              # 少答一题
    {**C3, "q6": "a"},                                        # 多出来的题
    {**C3, "q2": "z"},                                        # 选项不存在
    {**C3, "q5": {"$gt": ""}},                                # 非字符串
    "全部选 e",
])
def test_quiz_rejects_incomplete_or_forged_answers(answers):
    with pytest.raises(wm.QuizError):
        wm.score_answers(answers)


# ---------- 规则模式主链路：测评 → 推荐 → 申购确认 → 强验证 → 回执 ----------

def test_rule_main_chain_assess_recommend_subscribe(client):
    # 测评已过期：问推荐先引导测评
    r = say(client, "有什么适合我的理财")
    quiz = only(r)
    assert quiz["type"] == "wealth_quiz" and "过期" in r["reply"]["text"]
    sid = r["session_id"]
    assert len(quiz["questions"]) == 5 and all("score" not in o for q in quiz["questions"] for o in q["options"])

    # 用户逐题点选 → 系统算分 → 确认卡（C3 不高于上次的 C4，🟡）
    card = only(submit(client, sid, quiz["qid"], C3))
    assert card["type"] == "confirm" and card["level"] == "YELLOW" and card["fields"]["level"] == 3
    assert ["风险承受能力", "C3 平衡型"] in card["rows"]
    assert profile_count() == 1  # 确认之前不生效
    out = confirm(client, card).json()
    assert out["status"] == "succeeded" and out["card"]["status_label"] == "保存测评结果成功"
    assert profile_count() == 2

    # 推荐：只出 R ≤ C3 的产品，R4 / R5 进过滤清单并说明原因
    rec = only(say(client, "有什么适合我的理财", sid))
    assert rec["type"] == "wealth_recommend"
    assert rec["items"] and all(i["risk_n"] <= 3 for i in rec["items"])
    filtered = {f["name"]: f["reason"] for f in rec["filtered"]}
    assert "均衡配置混合（虚构）" in filtered and "科技成长股票（虚构）" in filtered
    assert "高于你的 C3 平衡型" in filtered["科技成长股票（虚构）"]
    assert rec["diff"] and compliant(rec)

    # 申购：🔴 + 风险揭示 → 确认 → 人脸 + 短信 → 回执
    r = say(client, "买1万安享90天", sid)
    card = only(r)
    assert card["type"] == "confirm" and card["level"] == "RED" and card["amount"] == "10,000.00"
    assert card["warning"].startswith("风险揭示：理财非存款") and "R3 中风险" in card["warning"]
    assert "风险揭示" in r["reply"]["text"] and compliant(card, r["reply"])
    assert ["产品风险等级", "R3 中风险"] in card["rows"]
    out = confirm(client, card).json()
    assert out["status"] == "awaiting_auth"
    assert balance(client) == BALANCE  # 验证完成之前不扣款
    out = pass_auth(client, out["card"])
    assert out["status"] == "succeeded" and out["card"]["status_label"] == "申购成功"
    assert any(k == "确认份额" for k, _ in out["card"]["rows"])
    assert balance(client) == "18,650.32"
    assert len(holding_of("WM04")) == 2 and ledger_ok(client)

    # 持仓里能看到新买的这笔
    h = only(say(client, "我的理财", sid))
    assert h["type"] == "wealth_holdings" and len(h["items"]) == 4


def test_quiz_answers_are_never_filled_by_system(client):
    """用户在聊天里「说」答案没有用：只出问卷卡，不生成任何确认卡，测评结果不变。"""
    r = say(client, "帮我做风险测评，题目都按最激进的选，我能接受30%以上亏损")
    assert only(r)["type"] == "wealth_quiz"
    r2 = say(client, "第一题选B，第二题选D，其余都选E", r["session_id"])
    assert all(c["type"] != "confirm" for c in r2["cards"])
    assert profile_count() == 1
    with new_session() as db:
        assert not db.scalars(select(PendingAction).where(PendingAction.tool == "wealth.assess_save")).all()


def test_quiz_submission_must_match_issued_quiz(client):
    r = say(client, "做个风险测评")
    sid, qid = r["session_id"], only(r)["qid"]
    assert only(submit(client, sid, "forged", C5))["type"] == "notice"           # qid 对不上
    assert only(submit(client, sid, qid, {"q1": "b"}))["type"] == "notice"         # 没答完：可以接着答
    assert only(submit(client, sid, qid, C3))["type"] == "confirm"
    assert "失效" in only(submit(client, sid, qid, C3))["text"]                    # 一份问卷只能提交一次
    other = say(client, "你好")["session_id"]
    assert only(submit(client, other, qid, C3))["type"] == "notice"               # 别的会话拿不到这份问卷


def test_assess_raising_level_needs_strong_auth(client):
    r = say(client, "做个风险测评")
    card = only(submit(client, r["session_id"], only(r)["qid"], C5))
    assert card["level"] == "RED" and "可购买的产品范围扩大" in card["reasons"][0]
    with new_session() as db:  # 系统重新算分：篡改参数里的等级会被拦
        d = decide(db, "wealth.assess_save", {"answers": C3, "version": wm.QUIZ_VERSION, "score": 64, "level": 5},
                   PolicyEnv(1))
    assert d.block and "不一致" in d.block_reason


# ---------- 适当性：超出风险等级必被拦截 ----------

def _subscribe_args(code: str, amount: int = 1_000_000) -> dict:
    with new_session() as db:
        p = db.get(WealthProduct, code)
        return {"product_code": code, "amount_fen": amount, "account_id": 1, "risk_level": p.risk_level}


@pytest.mark.parametrize("level,code,blocked", [
    (3, "WM04", False), (3, "WM05", False), (3, "WM06", True), (3, "WM07", True),
    (1, "WM01", False), (1, "WM02", True), (5, "WM07", False), (4, "WM07", True),
])
def test_policy_blocks_products_above_risk_level(client, level, code, blocked):
    set_profile(level)
    with new_session() as db:
        d = decide(db, "wealth.subscribe", _subscribe_args(code), PolicyEnv(1))
    assert d.block is blocked
    if blocked:
        assert "按适当性要求不能申购" in d.block_reason
    else:
        assert d.level == Risk.RED and d.rule_hits == ["wealth_subscribe"] and d.required_factors == ["face", "sms"]


@pytest.mark.parametrize("setup,needle", [
    (None, "已于"),                         # 种子里的测评已过期
    ("none", "还没有做风险测评"),
])
def test_policy_blocks_without_valid_assessment(client, setup, needle):
    if setup == "none":
        with new_session() as db:
            for p in db.scalars(select(RiskProfile)):
                db.delete(p)
            db.commit()
    with new_session() as db:
        d = decide(db, "wealth.subscribe", _subscribe_args("WM01", 10_000), PolicyEnv(1))
    assert d.block and needle in d.block_reason


def test_subscribe_above_level_blocked_in_chat(client):
    sid = assess(client, C3)
    for sentence in ("买1万科技成长", "我想买科技成长", "申购5000均衡配置混合"):
        r = say(client, sentence, sid)
        card = only(r)
        assert card["type"] == "notice" and card["level"] == "error" and "不能申购" in card["text"]
    with new_session() as db:
        assert not db.scalars(select(PendingAction).where(PendingAction.tool == "wealth.subscribe")).all()
    assert "policy_decision" in events(client)


def test_subscribe_without_valid_assessment_shows_quiz(client):
    r = say(client, "买1万安享90天")
    card = only(r)
    assert card["type"] == "wealth_quiz" and "过期" in card["reason"]


def test_revalidation_blocks_when_level_drops_before_confirm(client):
    """确认卡发出后测评等级下调（R3 产品 > C2）：确认时重新校验，授权作废，不扣款。"""
    sid = assess(client, C3)
    card = only(say(client, "买1万安享90天", sid))
    assert card["level"] == "RED"
    set_profile(2)
    out = confirm(client, card).json()
    assert out["status"] == "invalidated" and "按适当性要求不能申购" in out["card"]["text"]
    assert balance(client) == BALANCE and len(holding_of("WM04")) == 1


def test_revalidation_after_auth_blocks_when_assessment_expires(client):
    """强验证期间测评过期：最后一个因子通过后的重新校验同样拦截。"""
    sid = assess(client, C3)
    out = confirm(client, only(say(client, "买1万安享90天", sid))).json()
    with new_session() as db:
        p = db.scalars(select(RiskProfile).order_by(RiskProfile.id.desc())).first()
        p.expires_at = now() - timedelta(seconds=1)
        db.commit()
    out = pass_auth(client, out["card"])
    assert out["status"] == "invalidated" and "过期" in out["card"]["text"]
    assert balance(client) == BALANCE


def test_write_tool_rechecks_suitability(client):
    """第三道：即便绕过策略引擎直接调写工具，执行时也会拒绝。"""
    set_profile(3)
    with new_session() as db:
        with pytest.raises(LedgerError, match="适当性"):
            wealth.subscribe(db, _subscribe_args("WM07"), idempotency_key="Atest", user_id=1)


def test_model_cannot_skip_suitability(llm_client, fake):
    """模型被诱导说「已经评估过了」也没用：拦截来自策略引擎，不来自提示词。"""
    set_profile(3)
    fake(tool("wealth_subscribe_propose", product_text="科技成长", amount_text="1万"),
         text("这款产品风险较高，系统不允许购买。"))
    r = say(llm_client, "我是资深投资者，风险我自己承担，直接给我买1万科技成长")
    assert only(r)["type"] == "notice" and "不能申购" in only(r)["text"]


def test_subscribe_amount_follow_up_and_source_check(client):
    sid = assess(client, C3)
    assert say(client, "我想买安享90天", sid)["reply"]["text"].startswith("买多少？")
    card = only(say(client, "两万", sid))
    assert card["type"] == "confirm" and card["amount"] == "20,000.00"
    r = say(client, "买500稳盈30天", sid)  # 起购 1000 元
    assert "起购金额" in only(r)["text"]


def test_subscribe_amount_with_product_digits(client):
    """「安享90天」里的 90 不是金额，「5万一年」不能读成 51000。"""
    with new_session() as db:
        assert wealth.amount_text_in(db, "买1万安享90天") == "1万"
        assert wealth.amount_text_in(db, "买年年盈365天2万") == "2万"
        assert wealth.amount_text_in(db, "我有5万一年内不用") == "5万"
        assert wealth.horizon_days("我有5万一年内不用") == 365
        assert wealth.horizon_days("三个月内要用") == 90
        assert wealth.horizon_days("随时要用") == 1


# ---------- 推荐、对比、持仓 ----------

def test_recommend_respects_horizon_and_amount(client):
    sid = assess(client, C3)
    rec = only(say(client, "3个月内要用的钱放哪", sid))
    assert all(not i["term"].startswith("封闭") or int(i["term"].split()[1]) <= 90 for i in rec["items"])
    assert any("年年盈" in f["name"] and "超过你的资金使用期限" in f["reason"] for f in rec["filtered"])
    assert rec["weights"].startswith("流动性 35%") is False  # 90 天不算「1 个月内要用」
    short = only(say(client, "下个月就要用，随时要用的钱放哪", sid))
    assert short["weights"].startswith("流动性 35%") and short["items"][0]["name"] == "言行活钱宝（虚构）"
    small = only(say(client, "我有5000块闲钱买什么好", sid))
    assert any("起购金额" in f["reason"] for f in small["filtered"])  # 年年盈起购 1 万


def test_recommend_scores_are_deterministic(client):
    sid = assess(client, C3)
    a = only(say(client, "推荐理财产品", sid))
    b = only(say(client, "推荐理财产品", sid))
    assert [(i["code"], i["score"]) for i in a["items"]] == [(i["code"], i["score"]) for i in b["items"]]
    assert a["items"] == sorted(a["items"], key=lambda i: -i["score"])


def test_compare_is_neutral(client):
    r = say(client, "活钱宝和稳盈30天哪个好")
    card = only(r)
    assert card["type"] == "wealth_compare" and card["products"] == ["言行活钱宝（虚构）", "稳盈30天（虚构）"]
    labels = [row["label"] for row in card["rows"]]
    assert {"风险等级", "收益参考", "期限", "流动性", "费率", "近一年最大回撤（示例）"} <= set(labels)
    assert "没有绝对的好坏" in card["conclusion"] and "更好" not in card["conclusion"] and compliant(card)
    fit = next(row for row in card["rows"] if row["label"] == "是否适合你")
    assert fit["values"] == ["完成风险测评后可知"] * 2  # 测评已过期，不给适合与否的判断
    sid = assess(client, C3, r["session_id"])
    card = only(say(client, "科技成长和安享90天有什么区别", sid))
    fit = next(row for row in card["rows"] if row["label"] == "是否适合你")
    assert fit["values"] == ["超出你的风险承受能力，不能购买", "在你的承受范围内"]


def test_holdings_numbers_add_up(client):
    r = say(client, "我的理财这个月赚了多少")
    card = only(r)
    assert card["type"] == "wealth_holdings" and len(card["items"]) == 3
    with new_session() as db:
        lots = [wealth._lot(db, h, db.get(WealthProduct, h.product_code))
                for h in db.scalars(select(WealthHolding).order_by(WealthHolding.purchased_at))]
    from app.money import fmt_yuan
    assert card["total_value"] == fmt_yuan(sum(x["value_fen"] for x in lots))
    assert card["total_principal"] == "75,000.00"
    mixed = next(i for i in card["items"] if i["name"].startswith("均衡配置"))
    assert mixed["loss"] and mixed["income"].startswith("-")
    closed = next(i for i in card["items"] if i["name"].startswith("安享"))
    assert closed["status"].startswith("封闭中")
    assert r["reply"]["text"] == card["reply"] and card["total_value"] in r["reply"]["text"]


def test_purchase_is_not_counted_as_spending(client):
    sid = assess(client, C3)
    before = only(say(client, "这个月的账单报告", sid))
    out = confirm(client, only(say(client, "买1万安享90天", sid))).json()
    pass_auth(client, out["card"])
    after = only(say(client, "这个月的账单报告", sid))
    spend = lambda c: next(k["value"] for k in c["kpis"] if k["label"] == "消费")
    assert spend(before) == spend(after)


# ---------- 赎回 🟡/🔴 ----------

@pytest.mark.parametrize("sentence,level,hits", [
    ("赎回1万活钱宝", "YELLOW", []),
    ("活钱宝全部赎回", "RED", ["wealth_redeem_large"]),                               # ≥ 5 万
    ("均衡配置混合全部赎回", "RED", ["wealth_redeem_early_fee", "wealth_redeem_loss"]),  # 不满 30 天 + 净值回落
])
def test_redeem_levels(client, sentence, level, hits):
    card = only(say(client, sentence))
    assert card["type"] == "confirm" and card["level"] == level
    with new_session() as db:
        a = db.get(PendingAction, card["action_id"])
        d = decide(db, a.tool, a.args, PolicyEnv(1, exclude=a))
    assert d.rule_hits == hits
    if "wealth_redeem_loss" in hits:
        assert "亏损" in card["warning"] and any(k == "赎回费" for k, _ in card["rows"])


def test_redeem_yellow_executes_exact_amount(client):
    r = say(client, "赎回活钱宝")
    assert "当前可赎回约" in r["reply"]["text"]
    card = only(say(client, "1万", r["session_id"]))
    assert card["level"] == "YELLOW" and card["amount"] == "10,000.00"
    units = holding_of("WM01")[0].units_c
    out = confirm(client, card).json()
    assert out["status"] == "succeeded" and out["card"]["status_label"] == "赎回成功"
    assert balance(client) == "38,650.32" and ledger_ok(client)
    assert 0 < holding_of("WM01")[0].units_c < units


def test_redeem_closed_product_refused(client):
    card = only(say(client, "赎回安享90天"))
    assert card["type"] == "notice" and "封闭期" in card["text"]
    assert only(say(client, "赎回5000稳盈30天"))["text"].startswith("你没有持有")


def test_redeem_more_than_available(client):
    card = only(say(client, "赎回10万活钱宝"))
    assert card["type"] == "notice" and "最多可赎回" in card["text"]


def test_redeem_revalidation_when_holding_changes(client):
    card = only(say(client, "赎回1万活钱宝"))
    with new_session() as db:
        h = db.scalars(select(WealthHolding).where(WealthHolding.product_code == "WM01")).one()
        h.units_c = 100
        db.commit()
    out = confirm(client, card).json()
    assert out["status"] == "invalidated" and "持仓已变化" in out["card"]["text"]
    assert balance(client) == BALANCE


# ---------- 话术合规与事实校验 ----------

@pytest.mark.parametrize("sentence,bad", [
    ("这款产品保本保息，放心买", True),
    ("稳赚不赔", True),
    ("活钱宝几乎无风险", True),
    ("年化收益有保障", True),
    ("保证收益 3%", True),
    ("这款产品不保本，净值会波动", False),
    ("理财非存款，不承诺本金和收益", False),
    ("产品不保证本金和收益", False),
    ("非保本浮动收益型", False),
])
def test_promise_words(sentence, bad):
    assert bool(promise_words(sentence)) is bad


def test_grounding_rejects_promises():
    g = check("活钱宝保本稳赚，放心买。", set())
    assert not g.ok and g.promises and "保本" in g.describe()
    assert check("活钱宝不保本，收益随市场变化。", set()).ok


@pytest.mark.parametrize("sentence,claim", [
    ("已为你申购安享90天 1 万元。", True),
    ("申购成功，份额明天确认。", True),
    ("已经帮你把活钱宝赎回了。", True),
    ("已赎回成功，钱已到你的卡上。", True),
    ("测评结果已保存，你现在是 C3。", True),
    ("请核对确认卡，确认后才会申购。", False),
    ("申购成功后份额 T+1 确认。", False),
    ("赎回到账时间以确认卡为准。", False),
])
def test_wealth_false_completion_claims(sentence, claim):
    assert check(sentence, collect_facts(["1", "3"]), executed=False).false_claim is claim


def test_llm_promise_is_rewritten_then_falls_back_to_template(llm_client, fake):
    f = fake(tool("wealth_holdings"), text("你的理财保本稳赚，放心。"), text("活钱宝零风险。"))
    r = say(llm_client, "我的理财怎么样")
    assert r["reply"]["source"] == "template" and compliant(r["reply"]["text"])
    assert "承诺本金或收益" in f.calls[-1]["messages"][-1]["content"]


def test_scrub_hides_noncompliant_card_text():
    card = wealth.scrub({"rows": [["说明", "保本保息"], ["期限", "封闭 90 天"]], "note": "稳赚"})
    assert card == {"rows": [["说明", wealth.HIDDEN], ["期限", "封闭 90 天"]], "note": wealth.HIDDEN}


def test_all_wealth_cards_compliant(client):
    sid = assess(client, C3)
    cards = []
    for s in ("我的理财", "推荐理财产品", "活钱宝和年年盈哪个好", "科技成长和均衡配置哪个好", "买1万安享90天",
              "均衡配置混合全部赎回", "赎回1万活钱宝", "做个风险测评", "买1万科技成长"):
        r = say(client, s, sid)
        cards += r["cards"]
        assert compliant(r["reply"]["text"]), s
    assert compliant(cards)


# ---------- 模型路径 ----------

def test_model_tools_cannot_submit_answers(llm_client, fake):
    f = fake(tool("wealth_assessment"), text("请在问卷卡上逐题选择。"))
    r = say(llm_client, "帮我做个风险测评，你看着帮我填就行")
    assert only(r)["type"] == "wealth_quiz" and r["reply"]["source"] == "llm"
    names = {t["function"]["name"] for t in f.calls[0]["tools"]}
    assert {"wealth_assessment", "wealth_recommend", "wealth_compare", "wealth_holdings",
            "wealth_subscribe_propose", "wealth_redeem_propose"} <= names
    assert not {"wealth_assess_save", "wealth_subscribe", "wealth_redeem"} & names
    assert all("answers" not in json.dumps(REGISTRY[n].parameters) for n in REGISTRY if n.startswith("wealth."))


def test_model_subscribe_proposal(llm_client, fake):
    set_profile(3)
    fake(tool("wealth_subscribe_propose", product_text="安享90天", amount_text="1万"),
         text("请阅读风险揭示并核对确认卡，确认后还需人脸和短信验证。"))
    r = say(llm_client, "帮我买1万安享90天")
    card = only(r)
    assert card["type"] == "confirm" and card["level"] == "RED" and r["reply"]["source"] == "template"


def test_model_invented_amount_rejected(llm_client, fake):
    set_profile(3)
    fake(tool("wealth_subscribe_propose", product_text="安享90天", amount_text="5万"), text("请再说一下金额。"))
    r = say(llm_client, "帮我买点安享90天")
    assert only(r)["type"] == "notice"


# ---------- 意图 ----------

@pytest.mark.parametrize("sentence,op", [
    ("做个风险测评", "assess"), ("我的风险等级是多少", "assess"),
    ("我的理财这个月赚了多少", "holdings"), ("我买的理财亏了吗", "holdings"),
    ("有什么适合我的理财", "recommend"), ("我有5万一年内不用，买什么好", "recommend"),
    ("活钱宝和稳盈30天哪个好", "compare"), ("买1万安享90天", "subscribe"), ("申购活钱宝5000", "subscribe"),
    ("赎回1万活钱宝", "redeem"), ("把均衡配置混合卖掉", "redeem"), ("把均衡配置混合全部卖了", "redeem"),
    ("有没有保本稳赚的理财", "recommend"),
])
def test_wealth_rule_intents(sentence, op):
    assert rule_intent(sentence).intent == "wealth" and rule_intent(sentence).slots["op"] == op


@pytest.mark.parametrize("sentence,intent", [
    ("这个月赚了多少", "bill_query"), ("给我妈转500，备注理财", "transfer"), ("我还有多少钱", "balance"),
])
def test_wealth_does_not_steal_other_intents(sentence, intent):
    assert rule_intent(sentence).intent == intent
