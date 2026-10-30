"""模拟理财核心：产品目录、净值模型、风险测评规则（技术文档 §7.5）。产品全部虚构，不冒用真实产品名称。

净值是确定性公式：成立以来按年化 drift 线性增长，叠加一个余弦波动（amp / period / peak，单位：基点 / 天）。
成立日在种子数据里设为「建库当天往前 730 天」，所以同一份种子在哪天生成，净值走势相对今天都一样。
金额一律整数分，份额是整数「0.01 份」（units_c），净值是 Decimal（8 位小数）。
"""

import math
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

INCEPTION_DAYS_AGO = 730
PROFILE_VALID_DAYS = 365  # 测评有效期 12 个月

RISK_LABEL = {1: "R1 低风险", 2: "R2 中低风险", 3: "R3 中风险", 4: "R4 中高风险", 5: "R5 高风险"}
LEVEL_LABEL = {1: "C1 保守型", 2: "C2 稳健型", 3: "C3 平衡型", 4: "C4 成长型", 5: "C5 进取型"}

# code, name, kind, risk, term_days（0 = 开放式）, settle_days（赎回 T+n）, min_fen,
# 业绩比较基准（基点，开放式净值型为空）, 申购费（基点）, 赎回费（基点）, 持有不足多少天收赎回费, 净值模型, 别名, 简介
PRODUCTS = [
    ("WM01", "言行活钱宝（虚构）", "现金管理类", 1, 0, 0, 100, None, 0, 0, 0,
     {"drift_bp": 165}, ("活钱宝",),
     "主要投资货币市场工具，申购赎回灵活，净值波动较小，收益随市场变化"),
    ("WM02", "稳盈30天（虚构）", "固定收益类", 2, 30, 1, 100_000, (210, 250), 0, 0, 0,
     {"drift_bp": 230}, ("稳盈30天", "稳盈"),
     "主要投资存款、债券等固定收益资产，封闭 30 天，到期自动赎回"),
    ("WM03", "年年盈365天（虚构）", "固定收益类", 2, 365, 1, 1_000_000, (280, 330), 0, 0, 0,
     {"drift_bp": 305}, ("年年盈365天", "年年盈"),
     "主要投资中高等级债券，封闭 365 天，期间不能赎回"),
    ("WM04", "安享90天固收+（虚构）", "固收增强类", 3, 90, 1, 100_000, (260, 360), 0, 0, 0,
     {"drift_bp": 310, "amp_bp": 15, "period": 45, "peak": 600}, ("安享90天", "安享"),
     "以债券为主，少量配置权益资产增强收益，封闭 90 天，净值会有波动"),
    ("WM05", "稳健增利（虚构）", "固收增强类", 3, 0, 1, 100_000, None, 0, 150, 7,
     {"drift_bp": 330, "amp_bp": 60, "period": 60, "peak": 650}, ("稳健增利", "增利"),
     "开放式净值型产品，以债券为主、少量权益，持有不足 7 天赎回收 1.5% 赎回费"),
    ("WM06", "均衡配置混合（虚构）", "混合类", 4, 0, 2, 100_000, None, 15, 50, 30,
     {"drift_bp": 500, "amp_bp": 450, "period": 120, "peak": 705}, ("均衡配置混合", "均衡配置", "均衡混合"),
     "股债均衡配置，净值波动较大，可能出现亏损，持有不足 30 天赎回收 0.5% 赎回费"),
    ("WM07", "科技成长股票（虚构）", "权益类", 5, 0, 3, 100_000, None, 15, 150, 30,
     {"drift_bp": 700, "amp_bp": 1200, "period": 200, "peak": 560}, ("科技成长股票", "科技成长"),
     "主要投资科技行业股票，净值波动大，可能出现较大亏损，持有不足 30 天赎回收 1.5% 赎回费"),
]

_EIGHT = Decimal("0.00000001")


def nav_at(model: dict, inception: date, day: date) -> Decimal:
    """某天的单位净值。成立日之前按成立日算（净值 1）。"""
    t = max((day - inception).days, 0)
    v = 1 + model.get("drift_bp", 0) / 10_000 * t / 365
    if amp := model.get("amp_bp", 0):
        w = 2 * math.pi / model["period"]
        v += amp / 10_000 * (math.cos(w * (t - model["peak"])) - math.cos(w * -model["peak"]))
    return Decimal(repr(round(v, 8))).quantize(_EIGHT)


def value_fen(units_c: int, nav: Decimal) -> int:
    """份额（0.01 份）× 净值 → 金额（分），四舍五入。"""
    return int((Decimal(units_c) * nav).quantize(Decimal(1), ROUND_HALF_UP))


def units_for(amount_fen: int, nav: Decimal) -> int:
    """买入：金额能买到的份额，向下取整到 0.01 份。"""
    return int((Decimal(amount_fen) / nav).to_integral_value(ROUND_FLOOR))


def units_to_cover(amount_fen: int, nav: Decimal) -> int:
    """赎回指定金额需要的份额，向上取整，保证赎回金额不少于用户要的数。"""
    return int((Decimal(amount_fen) / nav).to_integral_value(ROUND_CEILING))


def bp_of(amount_fen: int, bp: int) -> int:
    """按基点计费，四舍五入到分。"""
    return int((Decimal(amount_fen) * bp / 10_000).quantize(Decimal(1), ROUND_HALF_UP))


def pct(bp: float | int) -> str:
    """基点 → 百分比字符串，两位小数：215 → '2.15%'。"""
    return f"{Decimal(bp) / 100:.2f}%"


# ---------- 风险测评（模拟规则）：5 题，每题 0–20 分，满分 100，按 20 分一档映射 C1–C5 ----------
# 第 5 题「最大可承受亏损」是一票保守约束：选了哪一档，等级最高就到哪一档

QUIZ_VERSION = "rq-2026-10"
QUESTIONS = [
    {"id": "q1", "text": "你的年龄是？", "options": [
        ("a", "18–30 岁", 15), ("b", "31–45 岁", 20), ("c", "46–55 岁", 10), ("d", "56–65 岁", 5), ("e", "65 岁以上", 0)]},
    {"id": "q2", "text": "你的投资经验是？", "options": [
        ("a", "只存过银行存款", 0), ("b", "买过银行理财或货币基金", 8), ("c", "买过债券基金或混合基金", 14),
        ("d", "有 3 年以上股票或股票基金投资经验", 20)]},
    {"id": "q3", "text": "这笔钱多久之内不会用到？", "options": [
        ("a", "随时可能要用", 0), ("b", "半年以内", 6), ("c", "半年到 1 年", 12), ("d", "1 到 3 年", 16),
        ("e", "3 年以上", 20)]},
    {"id": "q4", "text": "你的收入情况是？", "options": [
        ("a", "没有稳定收入", 0), ("b", "收入稳定，每月结余不多", 8), ("c", "收入稳定，每月有较多结余", 14),
        ("d", "收入较高且来源多样", 20)]},
    {"id": "q5", "text": "投资出现亏损时，你最多能接受亏多少？", "veto": True, "options": [
        ("a", "不能接受任何亏损", 0), ("b", "5% 以内", 5), ("c", "5%–15%", 10), ("d", "15%–30%", 15),
        ("e", "30% 以上", 20)]},
]
VETO_CAP = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}  # 第 5 题各选项对应的最高等级


class QuizError(ValueError):
    pass


def score_answers(answers: dict) -> tuple[int, int, bool]:
    """答案 → (总分, 等级 1–5, 是否被一票保守约束压低)。答案不全、选项不存在都拒绝，不猜。"""
    if not isinstance(answers, dict) or set(answers) != {q["id"] for q in QUESTIONS}:
        raise QuizError("问卷需要逐题作答，5 道题都要选")
    total = 0
    for q in QUESTIONS:
        opt = next((o for o in q["options"] if o[0] == answers[q["id"]]), None)
        if opt is None:
            raise QuizError("答案不在选项范围内")
        total += opt[2]
    by_score = min(5, max(1, math.ceil(total / 20)))
    cap = VETO_CAP[answers["q5"]]
    return total, min(by_score, cap), cap < by_score


def quiz_view() -> list[dict]:
    """给前端的问卷：只有题目和选项，不带分值。"""
    return [{"id": q["id"], "text": q["text"], "options": [{"id": o[0], "label": o[1]} for o in q["options"]]}
            for q in QUESTIONS]
