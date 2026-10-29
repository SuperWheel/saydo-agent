"""账户工具：查余额。转账领域（查收款人、转账、AA）在 tools/transfer.py。"""

from sqlalchemy import select

from ..cards import balance_card
from ..models import Account
from ..money import fmt_yuan
from ..policy import Risk
from . import ToolContext, ToolResult, bank_tool


@bank_tool(
    name="account.balance", domain="account", base_risk=Risk.GREEN,
    description="查询用户名下所有账户的余额。", capability="查余额", examples=("我还有多少钱",),
)
def get_balance(ctx: ToolContext) -> ToolResult:
    accts = ctx.db.scalars(select(Account).where(Account.user_id == ctx.user_id, Account.status == "active")).all()
    rows = [{"name": a.name, "card_tail": a.card_tail, "balance_fen": a.balance_fen} for a in accts]
    card = balance_card(rows)
    data = {
        "accounts": [{"name": r["name"], "card_tail": r["card_tail"], "balance_yuan": fmt_yuan(r["balance_fen"])} for r in rows],
        "total_yuan": card["total"], "as_of": card["as_of"],
    }
    return ToolResult(data, card)
