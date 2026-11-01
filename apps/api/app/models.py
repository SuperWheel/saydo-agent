"""数据表（M0 子集）。金额字段一律是整数分。"""

from datetime import date, datetime, timedelta

from sqlalchemy import JSON, BigInteger, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .clock import now
from .db import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32))
    phone_masked: Mapped[str] = mapped_column(String(16))
    elder_mode: Mapped[bool] = mapped_column(default=False)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16))  # debit（Ⅰ类借记）/ credit / pot
    name: Mapped[str] = mapped_column(String(32))
    card_tail: Mapped[str] = mapped_column(String(4))
    balance_fen: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="active")


class Contact(Base):
    """收款人主数据。别名和验证状态只能在这里维护，聊天记忆不能改它。"""

    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(32))
    account_no: Mapped[str] = mapped_column(String(32))
    bank: Mapped[str] = mapped_column(String(32))
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    relation: Mapped[str | None] = mapped_column(String(16))
    phone: Mapped[str | None] = mapped_column(String(16))
    intimacy: Mapped[str | None] = mapped_column(String(16))
    intimacy_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="active")
    note: Mapped[str | None] = mapped_column(String(32))
    first_transfer_at: Mapped[datetime | None] = mapped_column(DateTime)  # 为空 = 新收款人

    @property
    def masked_name(self) -> str:
        n = self.name
        return n[0] + "*" if len(n) <= 2 else n[0] + "*" * (len(n) - 2) + n[-1]

    @property
    def tail(self) -> str:
        return self.account_no[-4:]


class PaymentConsent(Base):
    """支付服务协议与免密邀请偏好，每用户一行。"""

    __tablename__ = "payment_consents"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    agreement_version: Mapped[str | None] = mapped_column(String(16))
    agreement_signed_at: Mapped[datetime | None] = mapped_column(DateTime)
    agreement_revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    mandate_offer_dismissed_at: Mapped[datetime | None] = mapped_column(DateTime)


class PaymentMandate(Base):
    """免密授权；额度或付款卡变更递增版本，使旧确认卡失效。"""

    __tablename__ = "payment_mandates"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"))
    single_max_fen: Mapped[int] = mapped_column(BigInteger)
    daily_max_fen: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")
    signed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    valid_days: Mapped[int] = mapped_column(Integer, default=365)
    valid_until: Mapped[datetime] = mapped_column(DateTime, default=lambda: now() + timedelta(days=365))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    txn_no: Mapped[str] = mapped_column(String(32), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    direction: Mapped[str] = mapped_column(String(4))  # in / out
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    counterparty: Mapped[str] = mapped_column(String(64))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    merchant: Mapped[str | None] = mapped_column(String(64))
    mcc: Mapped[str | None] = mapped_column(String(8))
    category: Mapped[str | None] = mapped_column(String(16))
    memo: Mapped[str | None] = mapped_column(Text)  # 外部输入，不可信，不直接进模型
    channel: Mapped[str] = mapped_column(String(16), default="app")
    status: Mapped[str] = mapped_column(String(16), default="succeeded")
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class LedgerEntry(Base):
    """复式记账分录：每笔交易借贷相等，余额变动必须有分录。"""

    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    txn_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    account_code: Mapped[str] = mapped_column(String(48))  # DEP:<account_id> / CLEARING:<bank> / EQUITY:OPENING
    dr_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    cr_fen: Mapped[int] = mapped_column(BigInteger, default=0)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    channel: Mapped[str] = mapped_column(String(16), default="app")
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # pending_question 等多轮状态
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ImBinding(Base):
    """IM 账号（如企业微信 userid）与银行用户的绑定，以及这个账号在 IM 里的会话。
    演示环境按配置自动绑定；真实环境应由用户在 App 内经 OAuth 授权后写入（见 channels/__init__.py）。"""

    __tablename__ = "im_bindings"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))  # wecom
    external_id: Mapped[str] = mapped_column(String(128), index=True)  # 渠道里的用户标识
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ImHandoff(Base):
    """IM 里被降权的办理请求：引导回 App 的链接只带这个随机码，App 打开后凭码取回原话填进输入框（不自动发送）。
    原话不放进链接：URL 会留在浏览器历史和各级访问日志里。"""

    __tablename__ = "im_handoffs"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PendingAction(Base):
    """待确认动作。确认卡、验证码、执行令牌都绑定它的 digest。"""

    __tablename__ = "pending_actions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str | None] = mapped_column(String(32))
    trace_id: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str] = mapped_column(String(48))
    args: Mapped[dict] = mapped_column(JSON)  # 规范化参数，digest 的输入
    display: Mapped[dict] = mapped_column(JSON)  # 确认卡展示内容（服务端按参数渲染）
    nonce: Mapped[str] = mapped_column(String(32))
    digest: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(8))  # YELLOW / RED
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    reserved_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    # pending → awaiting_auth → executing → succeeded / failed / unknown
    # 以及 cancelled / expired / invalidated
    status: Mapped[str] = mapped_column(String(16), default="pending")
    token_jti: Mapped[str | None] = mapped_column(String(32))
    result: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class AuthChallenge(Base):
    __tablename__ = "auth_challenges"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("pending_actions.id"))
    factors_required: Mapped[list] = mapped_column(JSON)
    factors_passed: Mapped[list] = mapped_column(JSON, default=list)
    otp_issued_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / passed / failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class LimitCounter(Base):
    """额度计数（客户 × 自然日）。used = 已成功，reserved = 确认卡已预占。"""

    __tablename__ = "limit_counters"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    used_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_fen: Mapped[int] = mapped_column(BigInteger, default=0)


class AuditLog(Base):
    """审计日志：只追加，hash = SHA256(prev_hash ‖ 规范化事件)。"""

    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str | None] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SystemFlag(Base):
    __tablename__ = "system_flags"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class AABill(Base):
    """AA 收款单（F-TR-06）。状态只做「已发送 / 已付清」，部分已付、拒绝、过期不做。"""

    __tablename__ = "aa_bills"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(32))
    total_fen: Mapped[int] = mapped_column(BigInteger)
    remainder_rule: Mapped[str] = mapped_column(String(16))  # front：名单前几位各多 1 分 / initiator：发起人承担
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent / paid
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AAShare(Base):
    __tablename__ = "aa_shares"
    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[str] = mapped_column(ForeignKey("aa_bills.id"))
    seq: Mapped[int] = mapped_column(Integer)  # 名单顺序，尾差按它分配
    name: Mapped[str] = mapped_column(String(32))
    is_initiator: Mapped[bool] = mapped_column(default=False)
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="sent")  # self（发起人自己那份）/ sent / paid
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)


class Card(Base):
    """银行卡（借记 / 信用 / 虚拟）。完整卡号只存在这里，给模型、前端和审计的一律只露尾号。"""

    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16))  # debit / credit / virtual
    name: Mapped[str] = mapped_column(String(32))
    card_no: Mapped[str] = mapped_column(String(19))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))  # 借记卡、虚拟卡的扣款账户
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / locked（临时锁定）/ lost（已挂失）
    # 交易限制：overseas / online / atm 三个开关，per_txn_fen / daily_fen 单笔 / 单日限额
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    credit_limit_fen: Mapped[int | None] = mapped_column(BigInteger)  # 信用卡：信用额度
    available_fen: Mapped[int | None] = mapped_column(BigInteger)  # 信用卡：可用额度
    merchant: Mapped[str | None] = mapped_column(String(32))  # 虚拟卡：限定商户，为空表示不限
    one_time: Mapped[bool] = mapped_column(default=False)  # 虚拟卡：用一次后失效
    expires_on: Mapped[date | None] = mapped_column(Date)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)  # 由哪个动作创建（虚拟卡）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    @property
    def tail(self) -> str:
        return self.card_no[-4:]


class SecurityEvent(Base):
    """熔断器计数用的安全事件（认证失败、疑似注入、发起转账、越权调用）。审计哈希链里另有一份完整记录。"""

    __tablename__ = "security_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))  # auth_failed / auth_passed / injection / injection_high / ...
    tool: Mapped[str | None] = mapped_column(String(48))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class BreakerTrip(Base):
    """熔断处置：readonly 会话只读 / freeze 暂停办理 / handoff 转人工。到期或在运营后台解除后失效。"""

    __tablename__ = "breaker_trips"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str | None] = mapped_column(String(32))  # 有值 = 只限这个会话
    trigger: Mapped[str] = mapped_column(String(32))  # breaker.yaml 里的触发条件 id
    level: Mapped[str] = mapped_column(String(16))
    tools: Mapped[list] = mapped_column(JSON)  # 拦截范围：["*"] 全部写工具 / ["@moves_money"] 资金类 / 具体工具名
    reason: Mapped[str] = mapped_column(String(64))  # 给用户看的原因类别
    trace_id: Mapped[str | None] = mapped_column(String(32))
    watermark: Mapped[int] = mapped_column(Integer, default=0)  # 已计入的最后一条 security_events.id，解除后从这里重新计数
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)  # 为空 = 只能人工解除
    released_at: Mapped[datetime | None] = mapped_column(DateTime)
    released_by: Mapped[str | None] = mapped_column(String(32))
    release_note: Mapped[str | None] = mapped_column(String(200))


# ---------- 跨场景联动（WS7）：心愿罐、模拟商户订单、任务计划、定时转账 ----------

class WishPot(Base):
    """心愿罐（F-XS-02）：从活期划到专项子账户（accounts.kind = pot），到期前不参与日常支出。余额以子账户为准。"""

    __tablename__ = "wish_pots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    purpose: Mapped[str] = mapped_column(String(32))
    until: Mapped[date | None] = mapped_column(Date)  # 到期日：过了这天剩余资金自动退回活期
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / released
    locked_fen: Mapped[int] = mapped_column(BigInteger, default=0)  # 累计存入
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)  # 由哪个动作创建
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime)


class MerchantOrder(Base):
    """在模拟商户（花店、蛋糕店）下的订单。商户侧的取消 / 退款也记在这里。"""

    __tablename__ = "merchant_orders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    shop_id: Mapped[str] = mapped_column(String(16))
    sku: Mapped[str] = mapped_column(String(16))
    item: Mapped[str] = mapped_column(String(32))
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    pot_id: Mapped[str | None] = mapped_column(String(32))  # 从哪个心愿罐付款；为空 = 活期
    delivery: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="paid")  # paid / cancelled（已退款）
    txn_no: Mapped[str | None] = mapped_column(String(32))
    refund_txn_no: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TaskPlan(Base):
    """多步任务计划（技术文档 §4.4）。steps 是校验过的 DAG，每步带状态、关联的待确认动作和输出。"""

    __tablename__ = "task_plans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str | None] = mapped_column(String(32))
    goal: Mapped[str] = mapped_column(String(64))
    source_text: Mapped[str] = mapped_column(Text)  # 用户原话：参数来源校验的依据
    source: Mapped[str] = mapped_column(String(16))  # llm / rule
    event_date: Mapped[date | None] = mapped_column(Date)
    due_at: Mapped[datetime | None] = mapped_column(DateTime)  # 临期步骤（at = due）的触发时间
    # draft → running → waiting（等临期触发）→ done；以及 cancelled / handoff
    status: Mapped[str] = mapped_column(String(16), default="draft")
    steps: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str | None] = mapped_column(String(200))  # 最近一次重新规划 / 回退的说明
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ScheduledTransfer(Base):
    """定时 / 周期转账（F-TR-05）。创建时按单次金额 + 收款人风险定级并确认，之后每期按这份授权执行，
    执行前重新校验余额、限额和熔断；风险比授权时高就转为普通确认卡。收款人和金额固定，改了就是新指令。"""

    __tablename__ = "scheduled_transfers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str | None] = mapped_column(String(32))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    account_no: Mapped[str] = mapped_column(String(32))
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    memo: Mapped[str | None] = mapped_column(String(20))
    recurrence: Mapped[str] = mapped_column(String(16))  # once / monthly
    day_of_month: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    level: Mapped[str] = mapped_column(String(8))  # 创建时授权的等级：执行时风险超过它就要用户重新确认
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / cancelled / completed
    runs: Mapped[list] = mapped_column(JSON, default=list)  # 每期结果 [{at, status, note, action_id}]
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------- 理财（WS5）：产品、风险测评、持仓。产品全部虚构 ----------

class WealthProduct(Base):
    """模拟理财产品。净值按 nav_model 和成立日用公式算（core_mock/wealth.py），不存每日净值。"""

    __tablename__ = "wealth_products"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # 现金管理类 / 固定收益类 / 固收增强类 / 混合类 / 权益类
    risk_level: Mapped[int] = mapped_column(Integer)  # 1–5 对应 R1–R5
    term_days: Mapped[int] = mapped_column(Integer, default=0)  # 封闭期天数，0 = 开放式
    settle_days: Mapped[int] = mapped_column(Integer, default=1)  # 赎回到账 T+n
    min_purchase_fen: Mapped[int] = mapped_column(BigInteger)
    benchmark_low_bp: Mapped[int | None] = mapped_column(Integer)  # 业绩比较基准（基点），开放式净值型为空
    benchmark_high_bp: Mapped[int | None] = mapped_column(Integer)
    sub_fee_bp: Mapped[int] = mapped_column(Integer, default=0)  # 申购费率（基点）
    redeem_fee_bp: Mapped[int] = mapped_column(Integer, default=0)  # 赎回费率（基点），持有不足 redeem_fee_days 天时收
    redeem_fee_days: Mapped[int] = mapped_column(Integer, default=0)
    nav_model: Mapped[dict] = mapped_column(JSON)
    inception_on: Mapped[date] = mapped_column(Date)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    intro: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="on_sale")  # on_sale / closed


class RiskProfile(Base):
    """风险测评结果（F-WM-01）。只追加：每次测评一条，以最新一条为准；答案由用户在问卷卡上逐题点选。"""

    __tablename__ = "risk_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    level: Mapped[int] = mapped_column(Integer)  # 1–5 对应 C1–C5
    score: Mapped[int] = mapped_column(Integer)
    answers: Mapped[dict] = mapped_column(JSON)
    version: Mapped[str] = mapped_column(String(16))
    assessed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)


class WealthHolding(Base):
    """理财持仓（按笔）。units_c 是份额 × 100；principal_fen 是剩余份额对应的成本（含申购费）。"""

    __tablename__ = "wealth_holdings"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    product_code: Mapped[str] = mapped_column(ForeignKey("wealth_products.code"))
    units_c: Mapped[int] = mapped_column(BigInteger)
    principal_fen: Mapped[int] = mapped_column(BigInteger)
    purchased_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    matures_on: Mapped[date | None] = mapped_column(Date)  # 封闭式产品的到期日
    status: Mapped[str] = mapped_column(String(16), default="holding")  # holding / redeemed
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)


class DebitAgreement(Base):
    """本行代扣 / 免密支付协议（订阅 WS6）。停止后商户不能再从本行账户扣款，但商户侧的订阅（会员自动续费）不受影响。"""

    __tablename__ = "debit_agreements"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    merchant: Mapped[str] = mapped_column(String(64))  # 登记商户规范名（core_mock/merchants.py），按它和流水对上
    agreement_no: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # withhold（代扣 / 自动缴费）/ nopwd（免密支付）
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / stopped
    signed_at: Mapped[datetime] = mapped_column(DateTime)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)


class CardReveal(Base):
    """查看完整卡号的模拟人脸验证。一次一用：人脸通过后返回一次完整卡号，详情弹层关掉再看要重新验证。"""

    __tablename__ = "card_reveals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"))
    digest: Mapped[str] = mapped_column(String(64))  # 人脸断言要绑定的摘要（卡片 + 随机数）
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / passed / failed
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
