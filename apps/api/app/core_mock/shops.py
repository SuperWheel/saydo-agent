"""模拟商户：花店、蛋糕店和几家常见品类的网店（技术文档 §7.7 的 GET /catalog、POST /quote、POST /orders、POST /orders/{id}/cancel）。

报价不落库：报价单号里编码了商户、商品和报价时间，价格永远按目录重新取，模型无法改价。
报价 2 小时内有效；下单时再核一次，过期或下架就要重新询价。
数据全部虚构：平台名用常见的购物 / 外卖平台，店名、商品都是编的，不对应任何真实店铺。
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

QUOTE_TTL = timedelta(hours=2)
OPTIONS = 3  # 候选卡列几款
INSTANT = "付款后即时发到手机"  # 电子卡没有配送日期


@dataclass(frozen=True)
class Shop:
    id: str
    name: str
    category: str  # 见 CATEGORY_LABELS
    mcc: str
    whitelisted: bool  # 白名单商户：从心愿罐付款、日累计不超限时一键确认 🟡
    refund: str
    platform: str  # 订购平台，卡片上显示「美团 · 花漾花艺」
    slot: str = "上午 10:00–12:00 送达"  # 送达日期后面的时段说明；INSTANT 表示不用配送

    @property
    def label(self) -> str:
        """「美团 · 花漾花艺」；平台自营店本身就带平台名，写「京东自营」。"""
        return self.name if self.name.startswith(self.platform) else f"{self.platform} · {self.name}"


@dataclass(frozen=True)
class Item:
    sku: str
    shop_id: str
    name: str
    price_fen: int
    for_whom: tuple[str, ...] = ()  # 推荐给谁：妈妈 / 爱人 / 通用
    note: str = ""  # 款式、卖点，候选卡上一行小字
    lead_days: int = 1  # 最早几天后送达（次日达 1、快递 3 天 3）


_FLOWER_REFUND = "配送前 12 小时可免费取消，全额退回原付款账户"
_RETURN_7D = "签收后 7 天无理由退货，退款原路返回"
SHOPS = {s.id: s for s in (
    Shop("FL01", "花漾花艺", "flower", "5992", True, _FLOWER_REFUND, "美团"),
    Shop("CK01", "麦香烘焙", "cake", "5462", True, "配送前 24 小时可免费取消，全额退回原付款账户", "美团"),
    Shop("SH01", "京东自营", "shoes", "5661", True, _RETURN_7D, "京东", "18:00 前送达 · 京东物流"),
    Shop("SH02", "步云鞋服旗舰店", "shoes", "5661", False, "签收后 7 天无理由退货（退货运费自理），退款原路返回", "天猫",
         "前送达 · 快递配送"),
    Shop("DG01", "京东自营", "digital", "5732", True, "签收后 7 天无理由退货（激活后不支持），退款原路返回", "京东",
         "18:00 前送达 · 京东物流"),
    Shop("DG02", "声澜数码旗舰店", "digital", "5732", False, _RETURN_7D, "天猫", "前送达 · 快递配送"),
    Shop("GR01", "邻家优选超市", "daily", "5411", True, "配送前可免费取消；生鲜签收后 24 小时内可申请售后", "美团",
         "09:00–11:00 送达 · 即时配送"),
    Shop("GR02", "京东自营", "daily", "5411", True, _RETURN_7D, "京东", "18:00 前送达 · 京东物流"),
    Shop("BK01", "书香小筑", "book", "5942", True, _RETURN_7D, "当当", "前送达 · 快递配送"),
    Shop("BK02", "京东自营", "book", "5942", True, _RETURN_7D, "京东", "18:00 前送达 · 京东物流"),
    Shop("GC01", "京东自营", "giftcard", "5311", True, "未绑定前 7 天内可退，退款原路返回", "京东", INSTANT),
)}
CATALOG = [
    Item("F101", "FL01", "康乃馨花束（19 枝）", 19_900, ("妈", "母亲", "长辈"), "粉色康乃馨 · 适合送长辈"),
    Item("F102", "FL01", "红玫瑰花束（11 枝）", 29_900, ("爱人", "老婆", "妻子", "媳妇", "女朋友"), "经典红玫瑰 · 适合送爱人"),
    Item("F103", "FL01", "向日葵混搭花束", 15_900, note="向日葵配洋桔梗 · 日常祝福"),
    Item("C201", "CK01", "8 寸鲜果奶油蛋糕", 23_800, note="当季鲜果 · 4–6 人份"),
    Item("C202", "CK01", "6 寸长寿桃蛋糕", 16_800, ("妈", "爸", "母亲", "父亲", "长辈"), "寿桃造型 · 适合祝寿"),
    Item("C203", "CK01", "6 寸巧克力蛋糕", 18_800, ("爱人", "老婆", "女朋友"), "浓郁黑巧 · 2–4 人份"),
    Item("S301", "SH01", "轻跑运动鞋（男女同款）", 32_900, note="透气网面 · 次日达"),
    Item("S302", "SH02", "真皮休闲板鞋", 45_900, note="头层牛皮 · 快递 3 天", lead_days=3),
    Item("S303", "SH01", "透气健步鞋（中老年款）", 26_900, ("妈", "爸", "母亲", "父亲", "长辈"), "软底防滑 · 适合长辈"),
    Item("D401", "DG01", "无线降噪耳机", 89_900, note="主动降噪 · 续航 30 小时"),
    Item("D402", "DG02", "20000mAh 快充充电宝", 14_900, note="22.5W 快充 · 快递 2 天", lead_days=2),
    Item("D403", "DG01", "11 英寸学习平板", 219_900, ("孩子", "儿子", "女儿"), "护眼屏 · 128GB"),
    Item("G501", "GR01", "家庭日用组合（纸巾 + 洗衣液）", 8_900, note="即时配送 · 次日上午到"),
    Item("G502", "GR01", "当季水果礼盒（5 斤）", 16_800, ("妈", "爸", "母亲", "父亲", "长辈"), "产地直发 · 适合送长辈"),
    Item("G503", "GR02", "粮油米面套装", 19_900, note="大米 + 食用油 + 挂面"),
    Item("B601", "BK01", "儿童绘本套装（10 册）", 12_800, ("孩子", "儿子", "女儿"), "3–6 岁 · 快递 2 天", lead_days=2),
    Item("B602", "BK02", "理财入门（第 3 版）", 4_900, note="入门读物 · 次日达"),
    Item("B603", "BK02", "唐诗宋词鉴赏（精装）", 8_800, note="精装典藏 · 次日达"),
    Item("K701", "GC01", "电子购物卡 200 元", 20_000, note="即时到账 · 可转赠", lead_days=0),
    Item("K702", "GC01", "电子购物卡 500 元", 50_000, note="即时到账 · 可转赠", lead_days=0),
    Item("K703", "GC01", "电子购物卡 1000 元", 100_000, note="即时到账 · 可转赠", lead_days=0),
]
CATEGORY_LABELS = {"flower": "鲜花", "cake": "蛋糕", "shoes": "鞋服", "digital": "数码", "daily": "日用超市",
                   "book": "图书", "giftcard": "礼品卡"}


def earliest(item: Item, wanted: date | None, at: datetime) -> date:
    """送达日期：用户要的那天，但不早于这件商品最早能送到的日子；电子卡是付款当天。"""
    first = at.date() + timedelta(days=item.lead_days)
    return first if wanted is None or wanted < first else wanted


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
        if self.shop.slot == INSTANT:
            return INSTANT
        return f"{self.delivery_date:%m-%d} {self.shop.slot}"

    def view(self) -> dict:
        return {"quote_id": self.quote_id, "shop": self.shop.name, "platform": self.shop.platform,
                "shop_label": self.shop.label, "category": self.shop.category,
                "item": self.item.name, "price_fen": self.item.price_fen, "delivery": self.delivery,
                "valid_until": f"{self.valid_until:%m-%d %H:%M}", "refund": self.shop.refund,
                "whitelisted": self.shop.whitelisted}


def catalog(category: str) -> list[Item]:
    shop_ids = {s.id for s in SHOPS.values() if s.category == category}
    return [i for i in CATALOG if i.shop_id in shop_ids]


def _ranked(category: str, for_whom: str, budget_fen: int | None) -> list[Item]:
    """推荐顺序：按收礼人挑的那件（「我妈」→ 康乃馨）排第一；没说收礼人时是最便宜的通用款；其余按价格从低到高。"""
    items = sorted(catalog(category), key=lambda i: i.price_fen)
    if budget_fen is not None:
        items = [i for i in items if i.price_fen <= budget_fen]
    if not items:
        return []
    pick = next((i for i in items if any(w in for_whom for w in i.for_whom)), None)
    pick = pick or next((i for i in items if not i.for_whom), items[0])
    return [pick, *(i for i in items if i is not pick)]


def _quote(item: Item, delivery_date: date | None, at: datetime) -> Quote:
    d = earliest(item, delivery_date, at)
    # 每段数字都不到 8 位：发给模型前的脱敏会把 8 位以上的数字串当卡号打码，单号就对不上了
    return Quote(f"Q{item.sku}-{at:%y%m%dT%H%M}-{d:%y%m%d}", SHOPS[item.shop_id], item, at, d)


def quote(category: str, *, for_whom: str = "", delivery_date: date, at: datetime,
          budget_fen: int | None = None) -> Quote | None:
    """按收礼人挑一件（「我妈」→ 康乃馨），超出预算就换便宜的；没有合适的返回 None。计划里的询价步骤用它。"""
    ranked = _ranked(category, for_whom, budget_fen)
    return _quote(ranked[0], delivery_date, at) if ranked else None


def options(category: str, *, for_whom: str = "", delivery_date: date | None, at: datetime,
            n: int = OPTIONS) -> list[Quote]:
    """候选商品：第一款就是 quote() 会挑的那件，后面按价格排，最多 n 款。每款各自报价（送达日期按各自的时效）。"""
    return [_quote(i, delivery_date, at) for i in _ranked(category, for_whom, None)[:n]]


def quote_item(sku: str, *, delivery_date: date | None, at: datetime) -> Quote | None:
    """指定商品重新报价（候选卡上选了一款、确认卡上「修改」换商品或送达日期时用）。价格仍按目录取。"""
    item = next((i for i in CATALOG if i.sku == sku), None)
    return _quote(item, delivery_date, at) if item else None


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
