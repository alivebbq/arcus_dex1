"""Deletion never cancels orders, closes exposure, or loses account-scoped logs."""
from copy import deepcopy
import json
import sqlite3
import time

from cryptography.fernet import Fernet
import pytest

from backend.store import Store
from backend.strategy_management import StrategyDeletionBlocked, delete_strategy


class ReadOnlyExchange:
    def __init__(self):
        self.snapshot = {'connected': True, 'updatedAtMs': int(time.time() * 1000), 'orders': [], 'positions': []}
        self.statuses = {}
        self.snapshots = 0
        self.lookups = []

    def account_snapshot(self):
        self.snapshots += 1
        return deepcopy(self.snapshot)

    def order_status(self, market, client_id, order_id=None):
        self.lookups.append((client_id, order_id))
        return deepcopy(self.statuses.get(client_id, {'status': 'UNKNOWN', 'filledSize': None}))

    def cancel_order(self, *args, **kwargs):
        raise AssertionError('Deletion must not cancel any order')

    def place_order(self, *args, **kwargs):
        raise AssertionError('Deletion must not place or close any order')


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path, Fernet.generate_key())
    account = store.add_account('Account A', {'address': '0x' + 'a' * 40, 'apiKey': 'b' * 64,
                                             'signingKey': 'c' * 64}, {'equity': '100', 'availableBalance': '80'})
    other = store.add_account('Account B', {'address': '0x' + 'd' * 40, 'apiKey': 'e' * 64,
                                           'signingKey': 'f' * 64}, {'equity': '250'})
    strategy = {'id': 'test-strategy-a', 'accountId': account['id'], 'name': 'Grid A', 'market': 'ETH-USD',
                'marketId': 2, 'mode': 'paper', 'status': 'draft', 'hasStarted': False,
                'inventory': '0', 'residualQuantity': '0', 'orders': [], 'slots': [], 'stats': {'cycles': 0}}
    store.save_strategy(strategy)
    exchange = ReadOnlyExchange()
    called = []

    def factory(selected):
        assert selected['id'] == account['id']
        called.append(selected['id'])
        return exchange

    return store, account, other, strategy, exchange, factory, called


def save(state, **changes):
    store, _, _, strategy, *_ = state
    strategy.update(changes)
    store.save_strategy(strategy)
    return strategy


def order(**changes):
    return {'clientId': 'ag-test-strateg-a-order', 'orderId': 'order-a', 'status': 'CANCELED',
            'state': 'terminal', 'filledSize': '0', 'accountedFill': '0', **changes}


def test_draft_deletion_preserves_accounts_snapshots_and_scoped_history(state):
    store, account, other, strategy, exchange, factory, called = state
    before = (store.get_account(account['id']), store.get_account(other['id']))
    store.event('info', 'Existing A history', strategy['id'])
    store.event('info', 'Existing B history', accountId=other['id'])
    result = delete_strategy(store, strategy, account, factory)
    assert result == {'deleted': True, 'id': strategy['id']}
    assert store.get_strategy(strategy['id']) is None
    assert (store.get_account(account['id']), store.get_account(other['id'])) == before
    assert not called and not exchange.snapshots
    logs = store.events()
    assert next(row for row in logs if row['message'] == 'Existing A history')['accountId'] == account['id']
    assert next(row for row in logs if row['message'] == 'Existing B history')['accountId'] == other['id']
    assert logs[0]['accountId'] == account['id']


@pytest.mark.parametrize('changes', [
    {'status': 'running'}, {'status': 'recovering'}, {'status': 'configuring'}, {'status': 'pausing'},
    {'status': 'stopped', 'inventory': '0.1'}, {'status': 'paused', 'residualQuantity': '0.1'},
    {'status': 'paused', 'trade': {'direction': 1}}, {'status': 'stopped', 'position': {'size': '0.1'}},
    {'status': 'stopped', 'marginSetup': {'state': 'pending'}},
    {'status': 'stopped', 'marginSetup': {'state': 'error', 'uncertain': True}},
    {'status': 'stopped', 'slots': [{'inventory': '0', 'entryCost': '1'}]},
])
def test_unsafe_local_states_are_blocked_without_network_or_mutation(state, changes):
    store, account, _, strategy, exchange, factory, _ = state
    save(state, mode='live', **changes)
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, account, factory)
    assert store.get_strategy(strategy['id']) == strategy
    assert not exchange.snapshots


def test_paper_stopped_discards_virtual_pending_order_without_any_exchange_calls(state):
    store, account, _, strategy, _, factory, _ = state
    save(state, status='stopped', orders=[order(status='UNKNOWN', state='pending_submission')])
    assert delete_strategy(store, strategy, account, factory)['deleted']


@pytest.mark.parametrize('status', ['stopped', 'paused'])
def test_paper_held_inventory_trade_and_virtual_protection_are_discarded_safely(state, status):
    store, account, other, strategy, exchange, factory, called = state
    original_snapshots = (store.get_account(account['id'])['snapshot'], store.get_account(other['id'])['snapshot'])
    save(state, status=status, inventory='4', residualQuantity='4', position={'size': '4'}, trade={'direction': 1},
         protection=order(status='UNTRIGGERED', state='protected'),
         orders=[order(status='UNKNOWN', state='pending_submission')],
         slots=[{'inventory': '4', 'entryCost': '400', 'entry': order(status='OPEN', state='entry_open')}])
    assert delete_strategy(store, strategy, account, factory)['deleted']
    assert not called and not exchange.snapshots and not exchange.lookups
    assert (store.get_account(account['id'])['snapshot'], store.get_account(other['id'])['snapshot']) == original_snapshots
    assert '虚拟订单/持仓' in store.events()[0]['message']


def test_live_deletion_reads_selected_account_and_ignores_foreign_positions(state):
    store, account, other, strategy, exchange, factory, called = state
    save(state, mode='live', status='stopped', hasStarted=True)
    exchange.snapshot['positions'] = [{'marketId': 2, 'size': '20', 'side': 'LONG'}, {'marketId': 1, 'size': '3'}]
    exchange.snapshot['orders'] = [{'clientId': 'another-strategy-order', 'orderId': 'foreign', 'marketId': 2}]
    assert delete_strategy(store, strategy, account, factory)['deleted']
    assert called == [account['id']]
    assert exchange.snapshots == 1
    assert store.get_account(other['id'])['snapshot']['equity'] == '250'


def test_live_owned_order_snapshot_blocks_even_when_local_slots_are_empty(state):
    store, account, _, strategy, exchange, factory, _ = state
    save(state, mode='live', status='paused', hasStarted=True)
    exchange.snapshot['orders'] = [{'clientId': f"ag-{strategy['id'][:12]}-pending", 'orderId': 'new-unknown'}]
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, account, factory)
    assert store.get_strategy(strategy['id']) is not None


def test_live_current_unknown_requires_terminal_and_exact_fill_evidence(state):
    store, account, _, strategy, exchange, factory, _ = state
    pending = order(status='UNKNOWN', state='recovery_error', uncertain=True)
    save(state, mode='live', status='stopped', hasStarted=True, orders=[pending])
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, account, factory)
    exchange.statuses[pending['clientId']] = {'status': 'CANCELED', 'filledSize': None}
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, account, factory)
    exchange.statuses[pending['clientId']] = {'status': 'CANCELED', 'filledSize': '0.1'}
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, account, factory)
    exchange.statuses[pending['clientId']] = {'status': 'CANCELED', 'filledSize': '0'}
    assert delete_strategy(store, strategy, account, factory)['deleted']


def test_observed_fill_difference_cannot_be_deleted_even_if_summary_inventory_is_zero(state):
    store, account, _, strategy, exchange, factory, _ = state
    save(state, mode='live', status='stopped', hasStarted=True,
         orders=[order(observedFilledSize='0.4', accountedFill='0')])
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, account, factory)
    assert not exchange.snapshots


def test_retained_terminal_history_does_not_need_expired_order_lookup(state):
    store, account, _, strategy, exchange, factory, _ = state
    terminal = order(status='FILLED', filledSize='0.4', accountedFill='0.4', uncertain=True)
    save(state, mode='live', status='stopped', hasStarted=True, orders=[terminal])
    assert delete_strategy(store, strategy, account, factory)['deleted']
    assert not exchange.lookups


def test_compact_completed_registry_can_survive_retention_but_visible_new_fill_blocks(state):
    store, account, _, strategy, exchange, factory, _ = state
    historical = {'orderId': 'old-order', 'filledSize': '0.4', 'submittedAt': 1}
    save(state, mode='live', status='paused', hasStarted=True, orderRegistry={'old-client': historical})
    exchange.statuses['old-client'] = {'status': 'FILLED', 'filledSize': '0.5'}
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, account, factory)
    exchange.statuses.clear()
    assert delete_strategy(store, strategy, account, factory)['deleted']


def test_unknown_trend_protection_and_missing_account_cannot_be_forgotten(state):
    store, account, _, strategy, exchange, factory, _ = state
    save(state, mode='live', status='stopped', hasStarted=True,
         protection=order(status='UNKNOWN', state='pending_submission'))
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, account, factory)
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, None, factory)
    assert store.get_strategy(strategy['id']) is not None


def test_incomplete_or_stale_account_snapshot_and_wrong_account_block_delete(state):
    store, account, other, strategy, exchange, factory, _ = state
    save(state, mode='live', status='stopped', hasStarted=True)
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, other, factory)
    exchange.snapshot['updatedAtMs'] = 0
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, account, factory)
    exchange.snapshot.update(updatedAtMs=int(time.time() * 1000), orders=[{}] * 1000)
    with pytest.raises(StrategyDeletionBlocked):
        delete_strategy(store, strategy, account, factory)


def test_old_event_schema_migrates_owner_before_strategy_is_deleted(tmp_path):
    db = sqlite3.connect(tmp_path / 'arcus.sqlite3')
    db.executescript('''CREATE TABLE strategies(id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,time REAL NOT NULL,
                            level TEXT NOT NULL,message TEXT NOT NULL,strategy_id TEXT);''')
    db.execute('INSERT INTO strategies VALUES(?,?)', ('old', json.dumps({'accountId': 'owner-a'})))
    db.execute('INSERT INTO events(time,level,message,strategy_id) VALUES(?,?,?,?)', (1, 'info', 'old owned log', 'old'))
    db.execute('INSERT INTO events(time,level,message,strategy_id) VALUES(?,?,?,?)', (1, 'info', 'orphan global log', 'missing'))
    db.commit()
    db.close()
    store = Store(tmp_path, Fernet.generate_key())
    store.delete_strategy('old')
    events = {row['message']: row for row in store.events()}
    assert events['old owned log']['accountId'] == 'owner-a'
    assert events['orphan global log']['accountId'] is None
