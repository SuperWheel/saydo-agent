"""订阅商户主数据（模拟，银行维护，可信）：商户侧有没有自己的订阅要取消，以及商户侧取消指引。

取消订阅要分清两件事（技术文档 §7.3）：
- 本行代扣 / 免密支付协议：本行能直接停止，停了以后商户不能再从本行账户扣款；
- 商户侧订阅（会员自动续费）：在商户那里，本行办不了，只能给出取消路径。

商户侧取消指引来自知识库（WS9，F-GN-14）的商户取消指引子库（knowledge/kb/merchant_cancel.yaml），
按登记商户规范名查条目，出处写知识库条款。指引是团队整理的「一般路径」演示数据，未向商户核实。
"""

from dataclasses import dataclass

from ..knowledge import merchant_entry


@dataclass(frozen=True)
class CancelGuide:
    merchant: str  # 登记商户规范名
    where: str  # 在哪里取消，如「爱奇艺 App」
    steps: tuple[str, ...]  # 取消步骤，按顺序
    source: str  # 出处（知识库 · 子库 · 条款 · 版本 · 生效日期）
    updated: str = ""  # 指引的核对日期
    url: str | None = None  # 官方帮助页（有才填）
    tips: tuple[str, ...] = ()  # 补充提醒，如「取消后当期会员仍可用到期末」

    def as_dict(self) -> dict:
        return {"merchant": self.merchant, "where": self.where, "steps": list(self.steps), "source": self.source,
                "updated": self.updated, "url": self.url, "tips": list(self.tips)}


# 登记商户 → 商户侧有没有自己的订阅（会员自动续费）。False 表示扣费只靠本行协议（如话费自动缴费），停了本行代扣就不会再扣
MERCHANT_SIDE: dict[str, bool] = {
    "爱奇艺VIP会员": True,
    "腾讯视频VIP": True,
    "网易云音乐": True,
    "QQ音乐": True,
    "Keep会员": True,
    "中国移动话费": False,
}

# 停止本行代扣之后还要提醒的事（商户侧没有订阅时）
AFTER_STOP: dict[str, str] = {
    "中国移动话费": "以后需要自己缴纳话费，号码和套餐不受影响",
}


def cancel_guide(merchant: str) -> CancelGuide | None:
    """商户侧取消指引。商户侧没有订阅、或知识库没有收录的商户返回 None（由上层建议联系商户客服或转人工）。"""
    if not MERCHANT_SIDE.get(merchant):
        return None
    e = merchant_entry(merchant)
    if e is None:
        return None
    return CancelGuide(merchant=merchant, where=e.where, steps=e.steps, source=e.source, tips=e.tips)  # 出处里已带生效日期
