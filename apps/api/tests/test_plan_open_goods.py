"""开放商品的未来购物：规则/模型入口、目录拒绝、到期选品和合作商户。"""

import json
from datetime import timedelta

import pytest

from app import clock, config
from app.agent import planner
from app.agent.loop import _session
from app.core_mock import shops
from app.db import new_session
from app.models import MerchantOrder, PendingAction, TaskPlan
from app.nlu import rule_intent
from app.tools import ToolContext
from app.tools import automation as auto

from .conftest import confirm, say, text, tool
from .helpers import ledger_ok, pass_auth
from .test_plan import card, fast_forward, op, plan_of
from .test_pot_detail import deposit


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'ENV_FILE', tmp_path / '.env')
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.mark.parametrize('mode', ['rule', 'valid', 'now', 'bad_json', 'unrelated'])
@pytest.mark.parametrize('sentence,item,who,budget,sku', [
    ('明天给我爸买一个1200元的按摩椅', '按摩椅', '爸爸', 120000, 'A801'),
    ('明天给我妈买一个2000元的手机', '手机', '妈妈', 200000, 'D404'),
    ('后天给我弟弟买个按摩仪', '按摩仪', '弟弟', 29900, 'A803'),
])
def test_open_goods_plan_and_due_execution(request, fake, mode, sentence, item, who, budget, sku):
    client = request.getfixturevalue('client' if mode == 'rule' else 'llm_client')
    if mode != 'rule':
        raw = planner.template_plan(sentence, clock.now())
        if mode == 'now':
            raw['steps'][0]['at'] = 'now'
        elif mode == 'unrelated':
            raw['steps'] = [{'id': 's1', 'tool': 'account.balance', 'args': {}, 'at': 'now'}]
        f = fake(text('这不是 JSON' if mode == 'bad_json' else json.dumps(raw)))
    r = say(client, sentence)
    p = plan_of(r)
    assert p['source'] == ('llm' if mode == 'valid' else 'rule')
    purchase = p['steps'][-1]['purchase']
    assert item in purchase['item'] and purchase['sku'] == sku
    assert purchase['for_whom'] == who and purchase['budget_fen'] == budget
    assert purchase['payment_source'] == '活期借记卡'
    with new_session() as db:
        steps = db.get(TaskPlan, p['plan_id']).steps
        assert [(s['tool'], s['at']) for s in steps] == [('merchant.quote', 'due'), ('order.propose', 'due')]
        assert steps[0]['args']['item_hint'] == item
        assert steps[-1]['args']['payment_source'] == 'debit'
        assert db.query(PendingAction).count() == db.query(MerchantOrder).count() == 0
    if mode != 'rule':
        assert len(f.calls) == 1 and f.calls[0]['tools'] is None
    p = plan_of(op(client, r['session_id'], p, 'confirm'))
    assert p['status'] == 'waiting'
    evs = fast_forward(client, p['due_at'])
    with new_session() as db:
        quote = db.get(TaskPlan, p['plan_id']).steps[0]['output']
        assert item in quote['item'] and quote['quote_id'].startswith('Q' + sku)
    c = card(evs[0])
    assert item in c['fields']['item'] and c['level'] == 'RED'
    assert pass_auth(client, confirm(client, c).json()['card'])['status'] == 'succeeded'
    with new_session() as db:
        order = db.query(MerchantOrder).one()
        assert order.sku == sku and order.pot_id is None
    assert ledger_ok(client)


@pytest.mark.parametrize('mode', ['client', 'llm_client'])
@pytest.mark.parametrize('sentence,item', [
    ('大后天给我姐买一台电脑', '电脑'),
    ('下周五给我妈买一个空气炸锅', '空气炸锅'),
    ('11月5号给我爸买一个望远镜', '望远镜'),
])
def test_unavailable_goods_stop_before_model(request, fake, mode, sentence, item):
    client = request.getfixturevalue(mode)
    f = fake()
    r = say(client, sentence)
    assert [c['type'] for c in r['cards']] == ['notice']
    assert f'合作商户暂时没有{item}' in r['reply']['text']
    assert '目前能买：鲜花、蛋糕' in r['reply']['text'] and '换一种商品' in r['reply']['text']
    assert '说具体一些' not in r['reply']['text'] and f.calls == []
    with new_session() as db:
        assert db.query(TaskPlan).count() == db.query(PendingAction).count() == db.query(MerchantOrder).count() == 0


@pytest.mark.parametrize('mode', ['client', 'llm_client'])
@pytest.mark.parametrize('sentence,minimum', [
    ('明天给我爸买一个1000元的按摩椅', '1,099.00'),
    ('明天给我妈买一个1000元的手机', '1,999.00'),
    ('后天给弟弟买个200元的按摩仪', '299.00'),
    ('大后天买个500元以内的耳机', '899.00'),
])
def test_budget_rejection_uses_matching_goods_minimum(request, fake, mode, sentence, minimum):
    client = request.getfixturevalue(mode)
    fake(text(json.dumps(planner.template_plan(sentence, clock.now()))))
    r = say(client, sentence)
    assert [c['type'] for c in r['cards']] == ['notice']
    assert '买不到' in r['reply']['text'] and '¥' + minimum in r['reply']['text']
    assert '提高预算' in r['reply']['text']
    with new_session() as db:
        assert db.query(TaskPlan).count() == db.query(PendingAction).count() == 0


@pytest.mark.parametrize('sentence', [
    '明天要还的信用卡', '上个月买了多少', '上个月买手机花了多少', '明天订阅腾讯视频',
    '明天买理财', '明天买基金', '明天买基金定投', '明天购买理财产品', '明天买活钱宝',
    '明天给我妈转500', '明天买手机的记录', '明天买',
])
def test_non_shopping_is_not_captured(client, sentence):
    assert not planner.is_multi_step(sentence)
    assert 'order' not in planner.action_kinds(sentence)
    r = say(client, sentence)
    assert all(c['type'] not in ('plan', 'shop_options') for c in r['cards'])
    with new_session() as db:
        assert db.query(TaskPlan).count() == db.query(MerchantOrder).count() == 0
        assert db.query(PendingAction).filter_by(tool='order.pay').count() == 0
    if '转500' in sentence:
        assert card(r)['title'] == '定时转账确认'
    if '活钱宝' in sentence or '理财产品' in sentence:
        assert rule_intent(sentence).intent == 'wealth'


@pytest.mark.parametrize('prefix', ['明天', '后天', '大后天', '下周五', '11月5号', '2026-11-05'])
@pytest.mark.parametrize('verb', ['买', '订', '购买', '预订', '下单', '送'])
def test_open_object_date_and_verb(prefix, verb):
    sentence = f'{prefix}给我爸{verb}一台望远镜'
    assert planner.is_multi_step(sentence)
    assert planner.shopping_items(sentence) == ['望远镜']


@pytest.mark.parametrize('category,item,available', [
    ('digital', '电脑', False), ('digital', '按摩椅', False), ('digital', '耳机', True),
    ('digital', '手机', True), ('appliance', '空气炸锅', False), ('appliance', '按摩椅', True),
    ('digital', '数码产品', True), ('daily', '日用品', True), ('flower', '花', True),
])
def test_quote_never_substitutes_named_item(client, category, item, available):
    with new_session() as db:
        ctx = ToolContext(db=db, user_id=1, session=_session(db, 1, None, 'app'), trace_id='quote-match', user_texts=[])
        result = auto.merchant_quote(ctx, category, item_hint=item)
    assert result.data['status'] == ('ok' if available else 'out_of_stock')
    if not available:
        assert 'quote_id' not in result.data and f'暂时没有{item}' in result.data['message']
    elif item in ('耳机', '手机', '按摩椅'):
        assert item in result.data['item']


@pytest.mark.parametrize('sentence,category', [
    ('明天买点日用品', 'daily'), ('明天买数码产品', 'digital'), ('明天买束花', 'flower'),
])
def test_generic_category_still_plans(client, sentence, category):
    p = plan_of(say(client, sentence))
    assert p['steps'][-1]['purchase']['category'] == category


@pytest.mark.parametrize('name', ['merchant_quote', 'merchant_options'])
def test_model_future_tool_call_blocks_whole_batch(llm_client, fake, name):
    future = str(clock.now().date() + timedelta(days=1))
    reply = tool(name, category='digital', date=future)
    reply.tool_calls += tool('order_propose', quote_id='Q-forged').tool_calls
    f = fake(reply)
    r = say(llm_client, '帮我挑个手机')
    assert [c['type'] for c in r['cards']] == ['notice']
    assert '先确认计划' in r['reply']['text'] and len(f.calls) == 1
    with new_session() as db:
        assert db.query(PendingAction).count() == db.query(MerchantOrder).count() == 0


def test_appliance_partner_pot_payment_and_ledger(client):
    _, pid = deposit(client, 500)
    r = say(client, '明天用心愿罐里的钱给我爸买一个400元的按摩仪')
    p = plan_of(op(client, r['session_id'], plan_of(r), 'confirm'))
    evs = fast_forward(client, p['due_at'])
    assert card(evs[0], 'receipt')['status'] == 'succeeded'
    with new_session() as db:
        order = db.query(MerchantOrder).one()
        assert order.shop_id == 'AP01' and order.sku == 'A803' and order.pot_id == pid
    assert shops.SHOPS['AP01'].whitelisted and ledger_ok(client)


def test_appliance_partner_nopwd_payment(client):
    from .test_mandate import enable

    enable(client)
    r = say(client, '明天给爸爸买1200元的按摩椅')
    p = plan_of(op(client, r['session_id'], plan_of(r), 'confirm'))
    evs = fast_forward(client, p['due_at'])
    assert card(evs[0], 'receipt')['status'] == 'succeeded'
    with new_session() as db:
        assert db.query(MerchantOrder).one().sku == 'A801'
    assert ledger_ok(client)


@pytest.mark.parametrize('sentence,item', [
    ('明天送给我妈一部手机', '手机'), ('明天买本书送孩子', '书'),
    ('明天订一束花送我妈', '花'), ('明天买一张购物卡', '购物卡'),
])
def test_recipient_after_verb_or_item(client, sentence, item):
    assert planner.shopping_items(sentence) == [item]
    assert plan_of(say(client, sentence))['status'] == 'draft'


def test_removed_named_goods_cannot_fallback(client, monkeypatch):
    monkeypatch.setattr(shops, 'CATALOG', [i for i in shops.CATALOG if '按摩椅' not in i.name])
    r = say(client, '明天给爸爸买1200元的按摩椅')
    assert '合作商户暂时没有按摩椅' in r['reply']['text']
    assert [c['type'] for c in r['cards']] == ['notice']
    assert auto.plan_quote('appliance', for_whom='爸爸', delivery_date=clock.now().date(),
                           at=clock.now(), item_hint='按摩椅') is None


@pytest.mark.parametrize('sentence,expected', [
    ('后天送我妈去医院', False),
    ('下周准备一下材料', False),
    ('后天买机票便宜吗', False),
    ('下个月买车需要多少首付', False),
    ('下周五我妈生日，送什么礼物好', False),
    ('下周五给我妈买点东西，推荐一下', False),
    ('明天提醒我买菜', False),
    ('明天给我爸买一个1200元的按摩椅', True),
    ('明天给我妈买一个2000元的手机', True),
    ('明天给我妈买个蛋糕', True),
    ('后天给孩子买本绘本', True),
    ('下周六给我妈买300左右的花', True),
    ('下周五我妈生日，帮我订束花和蛋糕，再存 500 进心愿罐', True),
    ('明天订个酒店', True),
    ('后天给我弟弟买个按摩仪', True),
    ('大后天给我姐买一台电脑', True),
    ('后天要用心愿罐里的钱给我妈妈买一双鞋', True),
    ('下个月准备买房要存多少钱', False),
    ('明年想买车，贷款怎么办', False),
    ('明天买什么理财好', False),
    ('明天能不能买基金', False),
    ('明天给我妈送500', False),
    ('明天要还的信用卡多少', False),
    ('每月15号给我妈转2000', False),
    ('明天买的东西到了吗', False),
    ('给我妈买双鞋', False),
    ('明天准备鲜花', True),
    ('明天准备一个望远镜', True),
    ('明天准备订酒店', True),
    ('后天送我妈到花店', False),
])
def test_shopping_command_gate(sentence, expected):
    assert planner.is_multi_step(sentence) is expected


@pytest.mark.parametrize('sentence', [
    '后天送我妈去医院', '下周准备一下材料', '后天买机票便宜吗',
    '下个月买车需要多少首付', '下周五我妈生日，送什么礼物好',
    '下周五给我妈买点东西，推荐一下', '明天提醒我买菜',
    '下个月准备买房要存多少钱', '明年想买车，贷款怎么办',
])
def test_advice_and_non_purchase_requests_skip_plan(client, sentence):
    assert planner.shopping_items(sentence) == []
    r = say(client, sentence)
    assert all(c['type'] != 'plan' for c in r['cards'])
    assert '合作商户暂时没有' not in r['reply']['text']
    with new_session() as db:
        assert db.query(TaskPlan).count() == db.query(MerchantOrder).count() == 0


@pytest.mark.parametrize('mode', ['client', 'llm_client'])
@pytest.mark.parametrize('item', ['东西', '礼物', '礼品', '点啥'])
def test_vague_goods_ask_what_to_buy(request, fake, mode, item):
    client = request.getfixturevalue(mode)
    f = fake()
    sentence = f'下周五给我妈买{item}'
    assert planner.is_multi_step(sentence)
    r = say(client, sentence)
    assert [c['type'] for c in r['cards']] == ['notice']
    assert '想给妈妈买什么' in r['reply']['text'] and '目前能买：' in r['reply']['text']
    assert '合作商户暂时没有' not in r['reply']['text'] and f.calls == []
    with new_session() as db:
        assert db.query(TaskPlan).count() == db.query(PendingAction).count() == 0


def test_unknown_concrete_booking_still_rejected(client):
    r = say(client, '明天订个酒店')
    assert [c['type'] for c in r['cards']] == ['notice']
    assert '合作商户暂时没有酒店' in r['reply']['text']


@pytest.mark.parametrize("item,skus", [
    ("箱水果", ["G502"]), ("束鲜花", ["F101", "F102", "F103"]), ("盒蛋糕", ["C201", "C202", "C203"]),
    ("双鞋", ["S301", "S302", "S303"]), ("礼品卡给同事", ["K701", "K702", "K703"]), ("康乃馨", ["F101"]),
    ("玫瑰", ["F102"]), ("平板电脑", ["D403"]), ("运动鞋", ["S301"]), ("手机壳", []), ("电脑", []),
])
def test_item_matching_strips_quantifier_and_keeps_head_noun(item, skus):
    """量词、「给谁」不影响匹配；按中心词找商品，「手机壳」不会被当成手机。"""
    assert [i.sku for i in auto.matching_items(auto.category_of(item), item)] == skus
