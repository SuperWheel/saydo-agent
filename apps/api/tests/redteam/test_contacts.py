"""联系人管理红队：模型编造来源与已出免密确认卡失效。"""

import pytest
from sqlalchemy import select

from app.db import new_session
from app.models import AuditLog, Contact, PendingAction

from app.agent.llm import LLMReply

from ..conftest import _ToolCall, confirm, say, text
from ..test_contacts import NEW, added_id, execute, mom, submit
from ..test_mandate import enable


def test_model_fabricated_account_rejected(llm_client, fake):
    fake(LLMReply(content="", tool_calls=[_ToolCall("contact_add_propose", NEW)], usage={}),
         text("请亲自填写联系人表单。"))
    response = say(llm_client, "管理一下联系人")
    assert response["cards"][0]["type"] == "notice"
    with new_session() as db:
        assert db.scalar(select(Contact).where(Contact.account_no == NEW["account_no"])) is None
        assert db.scalar(select(AuditLog).where(AuditLog.event_type == "provenance_reject"))
        assert db.scalar(select(PendingAction).where(PendingAction.tool == "contact.add")) is None


@pytest.mark.parametrize("op", ["update", "delete", "unbind_intimacy"])
def test_pending_nopwd_invalid_after_real_contact_form_change(client, op):
    enable(client)
    c = mom()
    pending = say(client, "给我妈转1500")["cards"][0]
    assert pending["level"] == "YELLOW" and "nopwd" in pending
    values = {"account_no": NEW["account_no"]} if op == "update" else {}
    execute(client, submit(client, op, contact_id=c.id, **values))
    out = confirm(client, pending).json()
    assert out["status"] == "invalidated"
    with new_session() as db:
        action = db.get(PendingAction, pending["action_id"])
        assert action.status == "invalidated" and action.reserved_fen == 0
        contact = db.get(Contact, c.id)
        assert contact.intimacy is None and contact.intimacy_verified_at is None
        if op == "update":
            assert contact.first_transfer_at is None
    if op == "update":
        fresh = say(client, "给我妈转100")["cards"][0]
        assert fresh["level"] == "RED" and "nopwd" not in fresh
        assert any("首次" in reason for reason in fresh["reasons"])


def test_delete_readd_restores_success_history_but_never_intimacy(client):
    enable(client)
    c = mom()
    # 先实际转账，删除后新建，流水外键仍指向旧行。
    execute(client, say(client, "给我妈转100")["cards"][0])
    execute(client, submit(client, "delete", contact_id=c.id))
    new_card = submit(client, "add", name=c.name, account_no=c.account_no, bank=c.bank, relation="妈妈")
    execute(client, new_card)
    new_id = added_id(new_card)
    with new_session() as db:
        restored = db.get(Contact, new_id)
        assert restored.id != c.id
        assert restored.first_transfer_at is not None
        assert restored.intimacy is None and restored.intimacy_verified_at is None
        assert db.get(Contact, c.id).status == "deleted"
    transfer = say(client, "给妈妈转1500")["cards"][0]
    assert transfer["level"] == "RED" and "nopwd" not in transfer
    assert not any("首次" in reason for reason in transfer["reasons"])


def test_delete_readd_without_success_is_still_new_payee(client):
    card = submit(client, "add", **NEW)
    execute(client, card)
    execute(client, submit(client, "delete", contact_id=added_id(card)))
    again = submit(client, "add", **NEW)
    execute(client, again)
    cid = added_id(again)
    with new_session() as db:
        assert db.get(Contact, cid).first_transfer_at is None
    transfer = say(client, "给陈晓转100")["cards"][0]
    assert transfer["level"] == "RED" and "nopwd" not in transfer
    assert any("首次" in reason for reason in transfer["reasons"])
