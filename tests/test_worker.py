"""Worker orchestration regressions. All clients/stores are local fakes."""
from copy import deepcopy
import importlib
import time

from cryptography.fernet import Fernet
import pytest


class WorkerStore:
    def __init__(self):
        self.strategy = {'id': 'grid-1', 'name': 'grid', 'accountId': 'account-1',
                         'market': 'TEST-USD', 'marketId': 1, 'mode': 'live',
                         'status': 'running', 'error': None}
        self.account = {'id': 'account-1', 'snapshot': {'connected': True,
                        'updatedAt': int(time.time() * 1000)}}
        self.events = []

    def accounts(self):
        return [deepcopy(self.account)]

    def strategies(self):
        return [deepcopy(self.strategy)]

    def get_strategy(self, identity):
        return deepcopy(self.strategy)

    def get_account(self, identity):
        return deepcopy(self.account)

    def save_strategy(self, strategy):
        self.strategy = deepcopy(strategy)

    def event(self, *args):
        self.events.append(args)


class WorkerPublic:
    def __init__(self):
        self.market = {'symbol': 'TEST-USD', 'marketId': 1, 'status': 'ONLINE', 'markPrice': '105'}
        self.market_error = None
        self.bbo_error = None
        self.book = {'bestBid': {'price': '104'}, 'bestAsk': {'price': '106'}}
        self.bbo_calls = 0

    def markets(self):
        if self.market_error:
            raise self.market_error
        return [deepcopy(self.market)]

    def bbo(self, market):
        self.bbo_calls += 1
        if self.bbo_error:
            raise self.bbo_error
        return deepcopy(self.book)


class RecordingEngine:
    def __init__(self, store):
        self.calls = []
        self.store = store
        self.fail = False

    def tick(self, strategy, account, market):
        self.calls.append(deepcopy(market))
        if self.fail:
            raise RuntimeError('temporary tick failure')
        if market.get('markPrice') == '0':
            strategy.update(status='recovering', suspended=True)
            self.store.save_strategy(strategy)


@pytest.fixture
def worker_module(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('VAULT_KEY', Fernet.generate_key().decode())
    module = importlib.import_module('backend.app')
    store = WorkerStore()
    public = WorkerPublic()
    engine = RecordingEngine(store)
    monkeypatch.setattr(module, 'store', store)
    monkeypatch.setattr(module, 'public_client', public)
    monkeypatch.setattr(module, 'engine', engine)
    monkeypatch.setattr(module, 'fee_time', time.time())
    monkeypatch.setattr(module, 'fee_cache', {})
    monkeypatch.setattr(module, 'markets_cache', [deepcopy(public.market)])
    monkeypatch.setattr(module, 'markets_time', time.time())
    return module, store, public, engine


def test_bbo_read_failure_blocks_resting_orders_keeps_fresh_mark_and_ticks(worker_module):
    module, store, public, engine = worker_module
    public.bbo_error = RuntimeError('book temporarily unavailable')
    module.one_cycle()
    assert len(engine.calls) == 1
    assert engine.calls[0]['markPrice'] == '105'
    assert engine.calls[0]['bestBid'] is None
    assert engine.calls[0]['bestAsk'] is None
    assert store.strategy['status'] == 'running'


def test_stale_market_cache_still_ticks_invalid_pricing_without_bbo(worker_module, monkeypatch):
    module, store, public, engine = worker_module
    public.market_error = RuntimeError('markets unavailable')
    monkeypatch.setattr(module, 'markets_time', time.time() - 30)
    module.one_cycle()
    assert len(engine.calls) == 1
    assert engine.calls[0]['markPrice'] == '0'
    assert public.bbo_calls == 0
    assert store.strategy['status'] == 'recovering'


def test_null_bbo_still_ticks_and_blocks_new_entries(worker_module):
    module, store, public, engine = worker_module
    public.book = {'bestBid': None, 'bestAsk': None}
    module.one_cycle()
    assert len(engine.calls) == 1
    assert engine.calls[0]['bestBid'] is None
    assert engine.calls[0]['bestAsk'] is None
    # Null levels are legitimate upstream data; the worker must preserve them
    # and continue reconciliation instead of crashing on None.get().
    assert store.strategy['status'] in ('running', 'recovering')


def test_tick_failure_is_recoverable_and_next_cycle_runs_again(worker_module):
    module, store, public, engine = worker_module
    engine.fail = True
    module.one_cycle()
    assert store.strategy['status'] == 'recovering'
    assert store.strategy['suspended'] is True
    module.one_cycle()
    assert len(engine.calls) == 2
    assert store.strategy['status'] == 'recovering'
    assert len(store.events) == 1
