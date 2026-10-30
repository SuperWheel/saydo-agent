"""模拟商户：花店、蛋糕店（技术文档 §7.7 的 GET /catalog、POST /quote、POST /orders、POST /orders/{id}/cancel）。

报价不落库：报价单号里编码了商户、商品和报价时间，价格永远按目录重新取，模型无法改价。
报价 2 小时内有效；下单时再核一次，过期或下架就要重新询价。数据全部虚构。
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

QUOTE_TTL = timedelta(hours=2)


@dataclass(frozen=True)
class Shop:
    id: str
    name: str
    category: str  # flower / cake
    mcc: str
    whitelisted: bool  # 白名单商户：从心愿罐付款、日累计不超限时一键确认 🟡
    refund: str


@dataclass(frozen=True)
class Item:
    sku: str
    shop_id: str
    name: str
    price_fen: int
    for_whom: tuple[str, ...] = ()  # 推荐给谁：妈妈 / 爱人 / 通用


SHOPS = {
    "FL01": Shop("FL01", "花漾花艺", "flower", "5992", True, "配送前 12 小时可免费取消，全额退回原付款账户"),
    "CK01": Shop("CK01", "麦香烘焙", "cake", "5462", True, "配送前 24 小时可免费取消，全额退回原付款账户"),
}
CATALOG = [
    Item("F101", "FL01", "康乃馨花束（19 枝）", 19_900, ("妈", "母亲", "长辈")),
    Item("F102", "FL01", "红玫瑰花束（11 枝）", 29_900, ("爱人", "老婆", "妻子", "媳妇", "女朋友")),
    Item("F103", "FL01", "向日葵混搭花束", 15_900),
    Item("C201", "CK01", "8 寸鲜果奶油蛋糕", 23_800),
    Item("C202", "CK01", "6 寸长寿桃蛋糕", 16_800, ("妈", "爸", "母亲", "父亲", "长辈")),
    Item("C203", "CK01", "6 寸巧克力蛋糕", 18_800, ("爱人", "老婆", "女朋友")),
]
CATEGORY_LABELS = {"flower": "鲜花", "cake": "蛋糕"}


@dataclass(frozen=True)
class Quote:
    quote_id: str
    shop: Shop
    item: Item
    quoted_at: datetime
    delivery_date: date

    @property
    def valid_until(self) -> datetime:
        return self.quoted_at + QUOTE_TTL

    @property
    def delivery(self) -> str:
        return f"{self.delivery_date:%m-%d} 上午 10:00–12:00 送达"

    def view(self) -> dict:
        return {"quote_id": self.quote_id, "shop": self.shop.name, "category": self.shop.category,
                "item": self.item.name, "price_fen": self.item.price_fen, "delivery": self.delivery,
                "valid_until": f"{self.valid_until:%m-%d %H:%M}", "refund": self.shop.refund,
                "whitelisted": self.shop.whitelisted}


def catalog(category: str) -> list[Item]:
    shop_ids = {s.id for s in SHOPS.values() if s.category == category}
    return [i for i in CATALOG if i.shop_id in shop_ids]


def quote(category: str, *, for_whom: str = "", delivery_date: date, at: datetime,
          budget_fen: int | None = None) -> Quote | None:
    """按收礼人挑一件（「我妈」→ 康乃馨），超出预算就换便宜的；没有合适的返回 None。"""
    items = sorted(catalog(category), key=lambda i: i.price_fen)
    if budget_fen is not None:
        items = [i for i in items if i.price_fen <= budget_fen]
    if not items:
        return None
    pick = next((i for i in items if any(w in for_whom for w in i.for_whom)), None)
    pick = pick or next((i for i in items if not i.for_whom), items[0])
    # 每段数字都不到 8 位：发给模型前的脱敏会把 8 位以上的数字串当卡号打码，单号就对不上了
    qid = f"Q{pick.sku}-{at:%y%m%dT%H%M}-{delivery_date:%y%m%d}"
    return Quote(qid, SHOPS[pick.shop_id], pick, at, delivery_date)


def lookup_quote(quote_id: str) -> Quote | None:
    """按报价单号还原报价。格式不对、商品不存在都返回 None；是否过期由调用方按当前时间判断。"""
    try:
        sku, quoted, delivery = quote_id.removeprefix("Q").split("-")
        quoted_at = datetime.strptime(quoted, "%y%m%dT%H%M")
        delivery_date = datetime.strptime(delivery, "%y%m%d").date()
    except ValueError:
        return None
    item = next((i for i in CATALOG if i.sku == sku), None)
    if item is None or not quote_id.startswith("Q"):
        return None
    return Quote(quote_id, SHOPS[item.shop_id], item, quoted_at, delivery_date)
