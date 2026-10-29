"""模拟的手机号转账目录：手机号 → 绑定的收款账户（对应现实里的「手机号码支付」账户查询）。数据全部虚构。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PhoneBinding:
    phone: str
    name: str
    bank: str
    account_no: str

    @property
    def masked_name(self) -> str:
        """只露最后一个字（如「*明」），让用户核对户名，又不暴露全名。"""
        return "*" * (len(self.name) - 1) + self.name[-1]


_BINDINGS = [
    PhoneBinding("13912345678", "王秀英", "工商银行", "6222021234561234"),  # 用户的妈妈，已在收款人里
    PhoneBinding("13687654321", "刘明", "建设银行", "6217009876544321"),  # 陌生人，从没转过账
    PhoneBinding("15800001111", "周小红", "交通银行", "6222601234560011"),
]
_BY_PHONE = {b.phone: b for b in _BINDINGS}
_BY_ACCOUNT = {b.account_no: b for b in _BINDINGS}


def lookup(phone: str) -> PhoneBinding | None:
    return _BY_PHONE.get(phone)


def by_account(account_no: str) -> PhoneBinding | None:
    return _BY_ACCOUNT.get(account_no)
