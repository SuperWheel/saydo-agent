"""联系人管理：表单与原话只负责提议，确认、验证和执行复用统一动作链路。"""

import hashlib
import json
import re

from sqlalchemy import select

from .. import audit, payments
from ..auth import actions
from ..cards import action_display, notice_card
from ..clock import now
from ..core_mock.ledger import LedgerError
from ..models import Account, Contact, PendingAction, ScheduledTransfer, Transaction
from ..nlu.transfer import mask_phone
from ..policy import Risk
from ..security import mask_pii
from . import ToolContext, ToolResult, bank_tool

BANKS = ("工商银行", "建设银行", "农业银行", "中国银行", "招商银行", "交通银行", "邮储银行")
FIELDS = ("name", "account_no", "bank", "phone", "relation")
VERBS = {"add": "添加联系人", "update": "修改联系人", "delete": "删除联系人",
         "bind_intimacy": "绑定亲密关系", "unbind_intimacy": "解除亲密关系"}


def _view(c: Contact) -> dict:
    return {"id": c.id, "relation": c.relation or "", "name_masked": c.masked_name, "bank": c.bank,
            "tail": c.tail, "phone_masked": mask_phone(c.phone) if c.phone else "",
            "intimacy": c.intimacy, "intimacy_verified": c.intimacy_verified_at is not None}


def _notice(message: str) -> ToolResult:
    return ToolResult({"status": "rejected", "message": message}, notice_card(message, "warning"))


@bank_tool(name="contact.manage", domain="contact", base_risk=Risk.GREEN,
           description="展示联系人管理表单，可添加、修改、删除联系人以及绑定或解除亲密关系。表单提交后仍需确认。",
           capability="管理联系人", examples=("我的联系人", "管理联系人"))
def manage(ctx: ToolContext) -> ToolResult:
    items = [_view(c) for c in ctx.db.scalars(select(Contact).where(
        Contact.user_id == ctx.user_id, Contact.status == "active").order_by(Contact.id))]
    card = {"type": "contacts", "items": items, "intimacy_options": list(payments.INTIMACY_OPTIONS), "banks": list(BANKS)}
    return ToolResult({"status": "ok", **card}, card)


def _own(db, user_id: int, contact_id) -> Contact | None:
    c = db.get(Contact, contact_id) if type(contact_id) is int else None
    return c if c and c.user_id == user_id and c.status == "active" else None


def _snapshot(c: Contact) -> str:
    # 摘要绑定出卡时的联系人信息：改卡号后，旧的关系绑定确认卡不能验证新卡号。
    values = [getattr(c, key) for key in (*FIELDS, "status", "intimacy")]
    values.append(c.intimacy_verified_at.isoformat() if c.intimacy_verified_at else None)
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _problem(db, user_id: int, args: dict) -> str | None:
    op, changes = args["operation"], args.get("changes", {})
    c = None if op == "add" else _own(db, user_id, args.get("contact_id"))
    if op != "add" and (c is None or _snapshot(c) != args.get("snapshot")):
        return "联系人信息已变化或已删除，请重新打开联系人卡片"
    if op == "add" or op == "update":
        if not changes or set(changes) - set(FIELDS):
            return "请填写需要修改的联系人字段"
        if op == "add" and any(not changes.get(k) for k in ("name", "account_no", "bank")):
            return "请填写姓名、卡号和开户行"
        for key, value in changes.items():
            if not isinstance(value, str):
                return "联系人字段格式不正确"
            if key == "name" and not re.fullmatch(r"[一-龥·A-Za-z ]{2,20}", value):
                return "姓名为2–20个汉字或字母"
            if key == "account_no" and not re.fullmatch(r"[0-9]{12,19}", value):
                return "卡号须为12–19位数字"
            if key == "phone" and value and not re.fullmatch(r"1[0-9]{10}", value):
                return "手机号须为11位数字且以1开头"
            if key == "bank" and value not in BANKS:
                return "请选择支持的开户行"
            if key == "relation" and len(value) > 10:
                return "称呼不能超过10个字"
            if key in ("relation", "name") and mask_pii(value) != value:
                return "姓名和称呼不能包含卡号或手机号"
        account_no = changes.get("account_no", c.account_no if c else None)
        duplicate = db.scalar(select(Contact).where(Contact.user_id == user_id, Contact.status == "active",
                                                    Contact.account_no == account_no,
                                                    Contact.id != (c.id if c else -1)))
        if duplicate:
            return "这个卡号已在联系人中，请直接修改已有联系人"
    if op == "delete" and db.scalar(select(ScheduledTransfer.id).where(
            ScheduledTransfer.user_id == user_id, ScheduledTransfer.contact_id == c.id,
            ScheduledTransfer.status == "active")):
        return "该联系人还有未执行的定时转账，请先取消定时转账再删除"
    if op == "bind_intimacy" and args.get("intimacy") not in payments.INTIMACY_OPTIONS:
        return "亲密关系只能选择父母、伴侣、子女、兄弟姐妹或密友"
    return None


def contact_context(db, args: dict, env):
    return actions.base_context(db, env, tool=f"contact.{args['operation']}",
                                block_reason=_problem(db, env.user_id, args),
                                facts={"contact_bind_intimacy": args["operation"] == "bind_intimacy"})


def _propose(ctx: ToolContext, op: str, values: dict, *, from_form: bool = False) -> ToolResult:
    changes = {key: values[key] for key in FIELDS if key in values}
    # 表单来自本轮 HTTP selection；模型工具没有 from_form 参数，不能自称由表单提交。
    if not from_form:
        current = ctx.user_texts[0] if ctx.user_texts else ""
        for key in ("account_no", "phone"):
            value = changes.get(key)
            if value and (not isinstance(value, str) or not re.search(rf"(?<![0-9]){re.escape(value)}(?![0-9])", current)):
                audit.record(ctx.db, ctx.trace_id, "provenance_reject", {"field": key}, ctx.session.id)
                return _notice("卡号和手机号必须来自您本轮原话或亲自填写的表单，请打开联系人表单填写。")
    c = _own(ctx.db, ctx.user_id, values.get("contact_id"))
    args = {"operation": op, "changes": changes}
    if op != "add":
        args |= {"contact_id": values.get("contact_id"), "snapshot": _snapshot(c) if c else None}
    if op == "bind_intimacy":
        args["intimacy"] = values.get("intimacy")
    if problem := _problem(ctx.db, ctx.user_id, args):
        return _notice(problem)
    preview = Contact(id=c.id if c else None, name=changes.get("name", c.name if c else ""),
                      account_no=changes.get("account_no", c.account_no if c else ""),
                      bank=changes.get("bank", c.bank if c else ""),
                      relation=changes.get("relation", c.relation if c else ""),
                      phone=changes.get("phone", c.phone if c else ""),
                      intimacy=args.get("intimacy", c.intimacy if c else None),
                      intimacy_verified_at=c.intimacy_verified_at if c else None)
    view = _view(preview)
    rows = [["姓名", view["name_masked"]], ["开户行", view["bank"]], ["卡号", "尾号 " + view["tail"]],
            ["手机号", view["phone_masked"] or "未填写"], ["称呼", view["relation"] or "未填写"]]
    if op == "bind_intimacy":
        rows += [["亲密关系", args["intimacy"]], ["验证", "需人脸识别或短信验证码，任选一种"]]
    if op in ("delete", "unbind_intimacy") or (op == "update" and "account_no" in changes and changes["account_no"] != c.account_no):
        rows.append(["说明", "对该联系人的免密支付立即失效；更换卡号后转账按新收款人验证"])
    display = action_display(title=VERBS[op] + "确认", verb=VERBS[op],
                             summary=f"{VERBS[op]}：{view['name_masked']}（尾号{view['tail']}）",
                             rows=rows, fields=view)
    return actions.propose_action(ctx, tool=f"contact.{op}", args=args, display=display, data=view)


def selection_turn(ctx: ToolContext, selection: dict) -> ToolResult:
    op = selection["type"].removeprefix("contact_")
    if op not in VERBS or set(selection) - {"type", "contact_id", "intimacy", *FIELDS}:
        return _notice("联系人表单格式不正确，请重新填写")
    allowed = {"type", *FIELDS} if op == "add" else ({"type", "contact_id", *FIELDS} if op == "update"
              else {"type", "contact_id", "intimacy"} if op == "bind_intimacy" else {"type", "contact_id"})
    if set(selection) - allowed:
        return _notice("联系人表单包含不支持的字段")
    return _propose(ctx, op, selection, from_form=True)


_PROPERTIES = {key: {"type": "string"} for key in FIELDS}


@bank_tool(name="contact.add_propose", domain="contact", base_risk=Risk.GREEN,
           description="提议添加联系人。卡号和手机号必须照抄本轮用户原话，建议用户在联系人表单填写。",
           parameters={"type": "object", "properties": _PROPERTIES, "required": ["name", "account_no", "bank"]})
def add_propose(ctx: ToolContext, name: str, account_no: str, bank: str,
                phone: str = "", relation: str = "") -> ToolResult:
    return _propose(ctx, "add", {"name": name, "account_no": account_no, "bank": bank, "phone": phone, "relation": relation})


@bank_tool(name="contact.update_propose", domain="contact", base_risk=Risk.GREEN,
           description="提议修改联系人，只传改动字段。卡号和手机号必须来自本轮用户原话。",
           parameters={"type": "object", "properties": {"contact_id": {"type": "integer"}, **_PROPERTIES}, "required": ["contact_id"]})
def update_propose(ctx: ToolContext, contact_id: int, **changes) -> ToolResult:
    return _propose(ctx, "update", {"contact_id": contact_id, **changes})


@bank_tool(name="contact.delete_propose", domain="contact", base_risk=Risk.GREEN,
           description="提议删除联系人，先检查未执行的定时转账，确认后软删除。",
           parameters={"type": "object", "properties": {"contact_id": {"type": "integer"}}, "required": ["contact_id"]})
def delete_propose(ctx: ToolContext, contact_id: int) -> ToolResult:
    return _propose(ctx, "delete", {"contact_id": contact_id})


@bank_tool(name="contact.bind_intimacy_propose", domain="contact", base_risk=Risk.GREEN,
           description="提议绑定亲密关系；需确认并完成人脸或短信验证，不会直接生效。",
           parameters={"type": "object", "properties": {"contact_id": {"type": "integer"},
                       "intimacy": {"type": "string", "enum": list(payments.INTIMACY_OPTIONS)}},
                       "required": ["contact_id", "intimacy"]})
def bind_intimacy_propose(ctx: ToolContext, contact_id: int, intimacy: str) -> ToolResult:
    return _propose(ctx, "bind_intimacy", {"contact_id": contact_id, "intimacy": intimacy})


@bank_tool(name="contact.unbind_intimacy_propose", domain="contact", base_risk=Risk.GREEN, stop_loss=True,
           description="提议解除亲密关系，确认后免密对该联系人立即失效。",
           parameters={"type": "object", "properties": {"contact_id": {"type": "integer"}}, "required": ["contact_id"]})
def unbind_intimacy_propose(ctx: ToolContext, contact_id: int) -> ToolResult:
    return _propose(ctx, "unbind_intimacy", {"contact_id": contact_id})


def _first_transfer(db, user_id: int, account_no: str):
    # 新建行保留旧外键。历史动作的不可变账号 + 成功流水，避免旧行改卡号后把旧流水误认给新账号。
    times = [txn.created_at for txn, action in db.execute(select(Transaction, PendingAction).join(
        PendingAction, Transaction.idempotency_key == PendingAction.id).join(Account, Transaction.account_id == Account.id)
        .where(Account.user_id == user_id, Transaction.status == "succeeded", Transaction.direction == "out",
               PendingAction.status == "succeeded", PendingAction.tool == "transfer.create"))
        if action.args.get("account_no") == account_no]
    # 种子及历史导入流水没有动作记录；只认仍保有该卡成功转账标记的联系人行。
    for c in db.scalars(select(Contact).where(Contact.user_id == user_id, Contact.account_no == account_no,
                                              Contact.first_transfer_at.is_not(None))):
        txn = db.scalar(select(Transaction).join(Account, Transaction.account_id == Account.id).where(
            Account.user_id == user_id, Transaction.contact_id == c.id, Transaction.direction == "out",
            Transaction.status == "succeeded").order_by(Transaction.created_at))
        if txn:
            times.append(txn.created_at)
    return min(times) if times else None


def _execute(db, args: dict, user_id: int) -> dict:
    if problem := _problem(db, user_id, args):
        raise LedgerError(problem)
    op, changes = args["operation"], args.get("changes", {})
    c = _own(db, user_id, args.get("contact_id"))
    if op == "add":
        c = Contact(user_id=user_id, **changes, aliases=[],
                    first_transfer_at=_first_transfer(db, user_id, changes["account_no"]))
        db.add(c)
    elif op == "update":
        if "account_no" in changes and changes["account_no"] != c.account_no:
            c.first_transfer_at, c.intimacy, c.intimacy_verified_at = None, None, None
        for key, value in changes.items():
            setattr(c, key, value)
    elif op == "delete":
        c.status, c.intimacy, c.intimacy_verified_at = "deleted", None, None
    elif op == "bind_intimacy":
        c.intimacy, c.intimacy_verified_at = args["intimacy"], now()
    else:
        c.intimacy, c.intimacy_verified_at = None, None
    db.flush()
    view = _view(c)
    result = {"status": "succeeded", "contact_id": c.id, **view,
              "receipt_rows": [["状态", VERBS[op] + "成功"], ["联系人", c.masked_name], ["卡号", "尾号 " + c.tail]]}
    if op == "update":
        result["fields"] = sorted(changes)
    return result


# 表单提议与执行共用校验；写工具始终不暴露给模型，执行令牌保证一次性。
@bank_tool(name="contact.add", domain="contact", base_risk=Risk.YELLOW, side_effect=True,
           llm_visible=False, policy_context=contact_context)
def add(db, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    return _execute(db, args, user_id)


@bank_tool(name="contact.update", domain="contact", base_risk=Risk.YELLOW, side_effect=True,
           llm_visible=False, policy_context=contact_context)
def update(db, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    return _execute(db, args, user_id)


@bank_tool(name="contact.delete", domain="contact", base_risk=Risk.YELLOW, side_effect=True,
           llm_visible=False, policy_context=contact_context)
def delete(db, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    return _execute(db, args, user_id)


@bank_tool(name="contact.bind_intimacy", domain="contact", base_risk=Risk.RED, side_effect=True,
           llm_visible=False, policy_context=contact_context)
def bind_intimacy(db, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    return _execute(db, args, user_id)


@bank_tool(name="contact.unbind_intimacy", domain="contact", base_risk=Risk.YELLOW, side_effect=True,
           llm_visible=False, stop_loss=True, policy_context=contact_context)
def unbind_intimacy(db, args: dict, *, idempotency_key: str, user_id: int) -> dict:
    return _execute(db, args, user_id)
