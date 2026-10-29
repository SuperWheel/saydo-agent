"""模拟核心银行记账。任何余额变动都必须通过 post()，生成借贷相等的两条分录。"""

import secrets
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import now
from ..models import Account, LedgerEntry, Transaction


class LedgerError(Exception):
    pass


def new_txn_no(at: datetime) -> str:
    return f"T{at:%Y%m%d%H%M%S}{secrets.randbelow(10**4):04d}"


def post(
    db: Session,
    *,
    account: Account,
    direction: str,
    amount_fen: int,
    counterparty: str,
    contra_code: str,
    at: datetime | None = None,
    **fields,
) -> Transaction:
    """记一笔客户账户的收入或支出。

    客户存款对银行是负债：转出时借记存款户（负债减少）、贷记清算户；转入相反。
    """
    if amount_fen <= 0:
        raise LedgerError("金额必须大于 0")
    if direction == "out" and account.balance_fen < amount_fen:
        raise LedgerError("余额不足")
    at = at or now()
    txn = Transaction(
        txn_no=new_txn_no(at), account_id=account.id, direction=direction, amount_fen=amount_fen,
        counterparty=counterparty, created_at=at, **fields,
    )
    db.add(txn)
    db.flush()
    dep = f"DEP:{account.id}"
    dr, cr = (dep, contra_code) if direction == "out" else (contra_code, dep)
    db.add_all([
        LedgerEntry(txn_id=txn.id, account_code=dr, dr_fen=amount_fen),
        LedgerEntry(txn_id=txn.id, account_code=cr, cr_fen=amount_fen),
    ])
    account.balance_fen += amount_fen if direction == "in" else -amount_fen
    return txn


def ledger_balance(db: Session, account_id: int) -> int:
    """按分录重算存款户余额（贷方 - 借方），用于和 accounts.balance_fen 对账。"""
    code = f"DEP:{account_id}"
    cr, dr = db.execute(
        select(func.coalesce(func.sum(LedgerEntry.cr_fen), 0), func.coalesce(func.sum(LedgerEntry.dr_fen), 0))
        .where(LedgerEntry.account_code == code)
    ).one()
    return int(cr) - int(dr)


def is_balanced(db: Session) -> bool:
    dr, cr = db.execute(select(func.sum(LedgerEntry.dr_fen), func.sum(LedgerEntry.cr_fen))).one()
    return (dr or 0) == (cr or 0)
