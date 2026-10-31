"""模拟核心系统的卡片资料：已有卡的开户信息，以及演示用的可绑定卡池（银联卡库，户名都是演示用户本人）。

卡片表（models.Card）只存卡号、状态、限额这些会变的东西；开户行、卡种、开户日期这类开户时就定了的资料按卡号在这里查，
不改表结构（演示库没有迁移，加列会让已有的 dev.db 起不来）。卡号是虚构的，格式按银联卡：62 开头、Luhn 校验位合法。
"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class CardProfile:
    bank: str
    branch: str
    card_class: str  # 卡种全称，如「Ⅰ类借记卡」「信用卡（金卡）」
    opened_on: date


@dataclass(frozen=True)
class PoolCard:
    """可绑定的卡：用户在别的银行（或本行另一张）开的卡，绑卡时按卡号后四位在这里匹配。"""

    id: str
    bank: str
    kind: str  # debit / credit
    card_class: str
    card_no: str
    branch: str
    opened_on: date
    balance_fen: int | None = None  # 借记卡：发卡行返回的余额（模拟）
    credit_limit_fen: int | None = None  # 信用卡：信用额度
    available_fen: int | None = None  # 信用卡：可用额度

    @property
    def tail(self) -> str:
        return self.card_no[-4:]

    @property
    def name(self) -> str:
        """卡片列表上的名字，如「招商银行信用卡」「工商银行借记卡」。"""
        return SHORT_BANK.get(self.bank, self.bank) + ("借记卡" if self.kind == "debit" else "信用卡")


SHORT_BANK = {"中国工商银行": "工商银行", "中国建设银行": "建设银行", "中国农业银行": "农业银行", "中国邮政储蓄银行": "邮储银行"}


HOME_BANK = "言行银行"

# 种子里的两张卡（seed.CARDS）
PROFILES = {
    "6230581000006688": CardProfile(HOME_BANK, "言行银行北京朝阳支行", "Ⅰ类借记卡", date(2019, 3, 15)),
    "6259651000003721": CardProfile(HOME_BANK, "言行银行信用卡中心", "信用卡（金卡）", date(2021, 7, 8)),
}

D, C = "debit", "credit"
BIND_POOL = [
    PoolCard("P01", "中国工商银行", D, "Ⅰ类借记卡", "6222028385608944150", "中国工商银行北京海淀支行", date(2016, 9, 1),
             balance_fen=1_256_080),
    PoolCard("P02", "中国工商银行", C, "信用卡（普卡）", "6253304622210422", "中国工商银行牡丹卡中心", date(2020, 4, 12),
             credit_limit_fen=3_000_000, available_fen=2_814_600),
    PoolCard("P03", "中国建设银行", D, "Ⅰ类借记卡", "6217005786510756722", "中国建设银行北京西客站支行", date(2017, 6, 20),
             balance_fen=532_011),
    PoolCard("P04", "中国建设银行", C, "信用卡（金卡）", "6259649346892200", "中国建设银行信用卡中心", date(2022, 1, 5),
             credit_limit_fen=4_000_000, available_fen=3_906_250),
    PoolCard("P05", "中国农业银行", D, "Ⅱ类借记卡", "6228487124793291171", "中国农业银行北京朝阳支行", date(2018, 11, 3),
             balance_fen=88_890),
    PoolCard("P06", "中国银行", D, "Ⅰ类借记卡", "6216613176790231164", "中国银行北京中关村支行", date(2015, 8, 28),
             balance_fen=2_013_400),
    PoolCard("P07", "中国银行", C, "信用卡（白金卡）", "6259075113694357", "中国银行信用卡中心", date(2023, 3, 16),
             credit_limit_fen=8_000_000, available_fen=7_725_000),
    PoolCard("P08", "交通银行", D, "Ⅰ类借记卡", "6222621494843880459", "交通银行上海徐汇支行", date(2014, 9, 9),
             balance_fen=36_550),
    PoolCard("P09", "招商银行", D, "Ⅰ类借记卡", "6214832109577259", "招商银行北京望京支行", date(2019, 12, 2),
             balance_fen=4_620_018),
    PoolCard("P10", "招商银行", C, "信用卡（金卡）", "6225750064561554", "招商银行信用卡中心", date(2020, 10, 30),
             credit_limit_fen=5_000_000, available_fen=4_288_900),
    PoolCard("P11", "中国邮政储蓄银行", D, "Ⅰ类借记卡", "6217992369213144697", "中国邮政储蓄银行河北石家庄支行", date(2012, 7, 1),
             balance_fen=120_000),
    PoolCard("P12", "浦发银行", C, "信用卡（普卡）", "6225212453744416", "浦发银行信用卡中心", date(2021, 5, 18),
             credit_limit_fen=2_000_000, available_fen=1_973_300),
    PoolCard("P13", "中信银行", D, "Ⅱ类借记卡", "6217712350854275", "中信银行北京国贸支行", date(2022, 8, 8),
             balance_fen=10_066),
    PoolCard("P14", "中信银行", C, "信用卡（金卡）", "6226902524210608", "中信银行信用卡中心", date(2019, 2, 14),
             credit_limit_fen=4_500_000, available_fen=4_500_000),
    PoolCard("P15", "兴业银行", D, "Ⅰ类借记卡", "622909085431321110", "兴业银行福州五四支行", date(2013, 10, 21),
             balance_fen=307_700),
    PoolCard("P16", "平安银行", C, "信用卡（普卡）", "6258232278367433", "平安银行信用卡中心", date(2024, 6, 6),
             credit_limit_fen=1_500_000, available_fen=1_388_000),
    PoolCard("P17", "民生银行", D, "Ⅰ类借记卡", "6226229993840008", "民生银行北京亚运村支行", date(2020, 3, 9),
             balance_fen=59_900),
    PoolCard("P18", "光大银行", C, "信用卡（金卡）", "6259782321914481", "光大银行信用卡中心", date(2023, 11, 11),
             credit_limit_fen=3_500_000, available_fen=3_102_400),
    PoolCard("P19", HOME_BANK, D, "Ⅱ类借记卡", "6230583430797781", "言行银行北京海淀支行", date(2025, 2, 20),
             balance_fen=500_000),
    PoolCard("P20", "北京银行", D, "Ⅰ类借记卡", "6214685593269951", "北京银行中关村分行", date(2018, 4, 25),
             balance_fen=740_315),
]
POOL_BY_ID = {p.id: p for p in BIND_POOL}
POOL_BY_NO = {p.card_no: p for p in BIND_POOL}

MAX_CARDS = 20  # 用户名下最多 20 张卡（含虚拟卡）


def profile_of(card_no: str) -> CardProfile | None:
    if card_no in PROFILES:
        return PROFILES[card_no]
    if p := POOL_BY_NO.get(card_no):
        return CardProfile(p.bank, p.branch, p.card_class, p.opened_on)
    return None


def masked_no(card_no: str) -> str:
    """前 4 后 4，中间按 4 位一组打码：6230 **** **** 6688。"""
    middle = len(card_no) - 8
    groups = ["****"] * (middle // 4) + (["*" * (middle % 4)] if middle % 4 else [])
    return " ".join([card_no[:4], *groups, card_no[-4:]])


def grouped_no(card_no: str) -> str:
    return " ".join(card_no[i:i + 4] for i in range(0, len(card_no), 4))


def mask_holder(name: str) -> str:
    return name[0] + "*" * (len(name) - 1) if len(name) <= 2 else name[0] + "*" * (len(name) - 2) + name[-1]
