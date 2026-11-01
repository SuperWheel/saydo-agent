"""购物计划预算锁定、提前提醒与补款恢复。"""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app import clock
from app.agent import planner
from app.core_mock import shops
from app.db import new_session
from app.models import Account, MerchantOrder, PendingAction, TaskPlan, User

from .conftest import confirm, say
from .helpers import events, pass_auth
from .test_mandate import enable
from .test_plan import card, fast_forward, op, plan_of, sync
from .test_pot_detail import deposit

REQUEST = '后天给妈妈买双鞋'


def balance(fen, pot_id=None):
    with new_session() as db:
        acct = db.get(Account, planner.auto._own_pot(db, 1, pot_id).account_id) if pot_id else planner.auto._debit(db, 1)
        acct.balance_fen = fen
        db.commit()


def start(client, sentence=REQUEST):
    r = say(client, sentence)
    return r['session_id'], plan_of(op(client, r['session_id'], plan_of(r), 'confirm'))


def scan(at=None):
    # 与快进同一入口；余额测试隔离工资/订阅补流水，明确只测余额变化。
    if at:
        clock.travel(datetime.fromisoformat(at) - clock.now())
    with new_session() as db:
        result = planner.tick(db, user_id=1)
        db.commit()
        return result


def assert_unpaid():
    with new_session() as db:
        assert db.query(MerchantOrder).count() == 0


@pytest.mark.parametrize('draft', [True, False])
def test_lock_budget_then_due_pays_from_pot(client, draft):
    r = say(client, REQUEST)
    sid, p = r['session_id'], plan_of(r)
    if not draft:
        p = plan_of(op(client, sid, p, 'confirm'))
    assert p['budget_lock'] == {'amount': '269.00', 'amount_fen': 26900, 'purpose': '妈妈购物'}
    assert 'lock_budget' in p['ops']
    c = card(op(client, sid, p, 'lock_budget'))
    assert c['level'] == 'RED'
    auth = confirm(client, c).json()['card']
    assert auth['mode'] == 'any'
    assert pass_auth(client, auth)['status'] == 'succeeded'
    response = sync(client, sid)
    p = response['cards'][0]
    assert '已改为从心愿罐「妈妈购物」付款' in response['text']
    assert 'lock_budget' not in p['ops'] and p['budget_lock'] is None
    assert p['payment_source'] == '心愿罐'
    assert p['steps'][-1]['purchase']['payment_source'] == '心愿罐「妈妈购物」'
    assert not any(c['type'] == 'confirm' for c in op(client, sid, p, 'lock_budget')['cards'])
    if draft:
        p = plan_of(op(client, sid, p, 'confirm'))
    assert_unpaid()
    evs = fast_forward(client, p['due_at'])
    assert plan_of(evs[0])['status'] == 'done'
    with new_session() as db:
        order = db.query(MerchantOrder).one()
        pot = planner.auto._own_pot(db, 1, order.pot_id)
        assert pot.purpose == '妈妈购物' and db.get(Account, pot.account_id).balance_fen == 0
    assert 'plan_budget_locked' in events(client)


@pytest.mark.parametrize('phrase', ['锁定预算', '先把预算存进心愿罐'])
def test_lock_budget_text_and_cancel_leaves_plan_unchanged(client, phrase):
    sid, p = start(client)
    c = card(say(client, phrase, sid))
    client.post(f"/api/actions/{c['action_id']}/cancel")
    p2 = sync(client, sid)['cards'][0]
    assert p2['version'] == p['version'] and p2['payment_source'] == '活期借记卡'
    assert 'lock_budget' in p2['ops'] and p2['steps'] == p['steps']


def test_budget_sum_and_birthday_purpose(client):
    p = plan_of(say(client, '11月20号妈妈生日，订鲜花和蛋糕'))
    assert p['budget_lock'] == {'amount': '367.00', 'amount_fen': 36700, 'purpose': '妈妈生日'}


@pytest.mark.parametrize('source', ['debit', 'pot'])
def test_reminder_once_and_no_execution(client, source):
    pid = None
    if source == 'pot':
        _, pid = deposit(client)
    sid, p = start(client, REQUEST if not pid else '后天用心愿罐的钱给妈妈买鞋')
    balance(100, pid)
    at = (datetime.fromisoformat(p['due_at']) - timedelta(hours=24)).isoformat()
    evs = scan(at)
    assert len(evs) == 1 and evs[0]['cards'][0]['type'] == 'notice'
    assert '269.00' in evs[0]['text'] and ('心愿罐' if pid else '活期') in evs[0]['text']
    assert scan() == []
    assert events(client).count('plan_funds_reminder') == 1
    assert_unpaid()


def test_enough_balance_no_reminder(client):
    sid, p = start(client)
    assert scan((datetime.fromisoformat(p['due_at']) - timedelta(hours=12)).isoformat()) == []
    assert 'plan_funds_reminder' not in events(client)


@pytest.mark.parametrize('how', ['text', 'op'])
def test_insufficient_then_resume_pays_once(client, how):
    enable(client)
    sid, p = start(client)
    balance(100)
    evs = scan(p['due_at'])
    p = plan_of(evs[0])
    assert p['status'] == 'running' and p['steps'][-1]['status'] == 'awaiting_funds'
    assert p['steps'][-1]['status_label'] == '待补款' and '268.00' in p['steps'][-1]['detail']
    assert 'resume' in p['ops'] and 'plan_funds_reminder' not in events(client)
    assert scan() == []  # 不每次扫描都重试或打扰用户
    r = op(client, sid, p, 'resume')
    assert plan_of(r)['steps'][-1]['status'] == 'awaiting_funds'
    assert_unpaid()
    balance(50000)
    r = say(client, '继续', sid) if how == 'text' else op(client, sid, p, 'resume')
    assert plan_of(r)['status'] == 'done' and card(r, 'receipt')['status'] == 'succeeded'
    assert scan() == []
    with new_session() as db:
        order = db.query(MerchantOrder).one()
        assert order.pot_id is None and order.amount_fen == 26900
        assert planner.auto._debit(db, 1).balance_fen == 23100
    assert 'plan_awaiting_funds' in events(client) and 'plan_resumed' in events(client)


def test_fast_forward_skips_reminder(client):
    _, pid = deposit(client)
    sid, p = start(client, '后天用心愿罐的钱给妈妈买鞋')
    balance(0, pid)
    p = plan_of(fast_forward(client, p['due_at'])[0])
    assert p['steps'][-1]['status'] == 'awaiting_funds'
    assert 'plan_funds_reminder' not in events(client)


@pytest.mark.parametrize('change', ['none', 'price', 'risk'])
def test_resume_refreshes_expired_quote_and_escalates_changes(client, monkeypatch, change):
    enable(client)
    sid, p = start(client)
    balance(0)
    p = plan_of(scan(p['due_at'])[0])
    clock.travel(timedelta(hours=3))
    balance(100000)
    if change == 'price':
        monkeypatch.setattr(shops, 'CATALOG', [replace(i, price_fen=i.price_fen + 10000) if i.sku == 'S303' else i
                                             for i in shops.CATALOG])
    if change == 'risk':
        monkeypatch.setitem(shops.SHOPS, 'SH01', replace(shops.SHOPS['SH01'], whitelisted=False))
    r = op(client, sid, p, 'resume')
    if change == 'none':
        assert plan_of(r)['status'] == 'done'
    else:
        assert plan_of(r)['steps'][-1]['status'] == 'awaiting'
        assert card(r)['type'] == 'confirm'
        assert_unpaid()


@pytest.mark.parametrize('trigger', ['resume', 'scan'])
def test_after_delivery_date_expires_without_payment(client, trigger):
    enable(client)
    sid, p = start(client)
    balance(0)
    p = plan_of(scan(p['due_at'])[0])
    delivery = p['steps'][-1]['purchase']['delivery_date']
    clock.travel(datetime.fromisoformat(delivery) + timedelta(days=1) - clock.now())
    balance(100000)
    r = op(client, sid, p, 'resume') if trigger == 'resume' else scan()[0]
    assert plan_of(r)['status'] == 'expired' and plan_of(r)['steps'][-1]['status'] == 'expired'
    assert_unpaid()


@pytest.mark.parametrize('operation', ['lock_budget', 'resume'])
def test_other_users_plan_rejected(client, operation):
    sid, p = start(client)
    with new_session() as db:
        db.add(User(id=999, name='他人', phone_masked='138****9999'))
        db.flush()
        db.get(TaskPlan, p['plan_id']).user_id = 999
        db.commit()
    r = op(client, sid, p, operation)
    assert r['cards'][0]['type'] == 'notice' and '失效' in r['reply']['text']
    with new_session() as db:
        assert db.query(PendingAction).filter_by(tool='pot.lock').count() == 0


def test_failed_lock_does_not_change_plan(client):
    sid, p = start(client)
    balance(0)
    r = op(client, sid, p, 'lock_budget')
    assert r['cards'][0]['type'] == 'notice'
    with new_session() as db:
        p2 = planner.plan_card(db.get(TaskPlan, p['plan_id']))
        assert p2['steps'] == p['steps'] and p2['version'] == p['version']


def test_lock_action_fails_after_proposal_without_switching_source(client):
    sid, p = start(client)
    c = card(op(client, sid, p, 'lock_budget'))
    balance(0)
    assert confirm(client, c).json()['status'] != 'succeeded'
    p2 = sync(client, sid)['cards'][0]
    assert p2['payment_source'] == '活期借记卡' and p2['version'] == p['version']


def test_pending_lock_pauses_due_orders_and_is_cancelled_with_plan(client):
    enable(client)
    sid, p = start(client)
    c = card(op(client, sid, p, 'lock_budget'))
    # 快进不足确认卡有效期，恰好跨过购物执行时点。
    with new_session() as db:
        plan = db.get(TaskPlan, p['plan_id'])
        plan.due_at = clock.now() + timedelta(seconds=10)
        db.commit()
    assert scan((clock.now() + timedelta(seconds=20)).isoformat()) == []
    assert_unpaid()
    op(client, sid, p, 'cancel')
    with new_session() as db:
        assert db.get(PendingAction, c['action_id']).status == 'cancelled'


def test_run_authorized_balance_recheck_awaits_funds(client, monkeypatch):
    enable(client)
    sid, p = start(client)
    original = planner.auto.run_authorized

    def run(db, **kwargs):
        planner.auto._debit(db, 1).balance_fen = 0
        return original(db, **kwargs)

    monkeypatch.setattr(planner.auto, 'run_authorized', run)
    p = plan_of(scan(p['due_at'])[0])
    assert p['steps'][-1]['status'] == 'awaiting_funds'
    assert_unpaid()


def test_expired_pot_plan_releases_reservation(client):
    from app.tools.pot import _plans

    _, pid = deposit(client)
    sid, p = start(client, '后天用心愿罐的钱给妈妈买鞋')
    balance(0, pid)
    p = plan_of(scan(p['due_at'])[0])
    with new_session() as db:
        assert _plans(db, 1, pid)[1] == 26900
    delivery = p['steps'][-1]['purchase']['delivery_date']
    scan((datetime.fromisoformat(delivery) + timedelta(days=1)).isoformat())
    with new_session() as db:
        assert _plans(db, 1, pid)[1] == 0


def test_cancel_draft_after_budget_deposited_offers_release(client):
    r = say(client, REQUEST)
    sid, p = r['session_id'], plan_of(r)
    c = card(op(client, sid, p, 'lock_budget'))
    assert pass_auth(client, confirm(client, c).json()['card'])['status'] == 'succeeded'
    p = sync(client, sid)['cards'][0]
    r = op(client, sid, p, 'cancel')
    assert plan_of(r)['status'] == 'rolling_back'
    c = card(r)
    with new_session() as db:
        assert db.get(PendingAction, c['action_id']).tool == 'pot.release'
    assert confirm(client, c).json()['status'] == 'succeeded'
    assert sync(client, sid)['cards'][0]['status'] == 'cancelled'
