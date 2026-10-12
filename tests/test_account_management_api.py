"""Account/subaccount isolation and safe strategy deletion through authenticated routes."""
import asyncio
import importlib
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.security import password_hash
from backend.store import Store


def test_subaccounts_statistics_and_strategy_deletion_stay_separate(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('VAULT_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('COOKIE_SECURE', '0')
    monkeypatch.setenv('PUBLIC_ORIGIN', 'http://testserver')
    monkeypatch.setenv('ADMIN_PASSWORD_HASH', password_hash('test-pass'))
    module = importlib.import_module('backend.app')
    private_store = Store(tmp_path / 'case-store', Fernet.generate_key())
    monkeypatch.setattr(module, 'store', private_store)
    monkeypatch.setattr(module.engine, 'store', private_store)
    monkeypatch.setattr(module.trend_engine, 'store', private_store)
    monkeypatch.setattr(module, 'clients', {})
    monkeypatch.setattr(module, 'one_cycle', lambda: None)

    async def idle_statistics():
        while True:
            await asyncio.sleep(1)
    monkeypatch.setattr(module, 'statistics_worker', idle_statistics)

    class Statistics:
        def __init__(self):
            self.forgotten = []

        def request_sync(self, account_id):
            pass

        def snapshot(self, account_id):
            account = private_store.get_account(account_id)
            index = private_store.credentials(account)['accountIndex']
            return {'status': 'ready', 'complete': True, 'stale': False, 'pnlKnown': True,
                    'turnover': str(1000 + index), 'realizedPnl': str(10 + index), 'fees': str(1 + index)}

        def forget(self, account_id):
            self.forgotten.append(account_id)

    metrics = Statistics()
    monkeypatch.setattr(module, 'account_statistics', metrics)

    class Client:
        def __init__(self, credentials):
            self.credentials = credentials

        def validate_key(self):
            return {'valid': True}

        def account(self):
            return {'equity': str(100 + self.credentials['accountIndex']), 'availableBalance': '100',
                    'positions': [], 'orders': []}

    monkeypatch.setattr(module, 'ArcusClient', Client)
    market = {'symbol': 'ETH-USD', 'marketId': 2, 'status': 'ONLINE', 'tickSize': '0.01',
              'stepSize': '0.0000001', 'minOrderSize': '0.001', 'minOrderNotional': '5',
              'initialMarginFraction': '0.04', 'offHoursInitialMarginFraction': '0.04',
              'markPrice': '2500', 'makerFee': '0', 'takerFee': '0.000225', 'isOutsideRth': False}
    monkeypatch.setattr(module, 'markets_cache', [market])
    monkeypatch.setattr(module, 'markets_time', time.time())
    body = {'name': 'test', 'address': '0x' + 'c'*40, 'apiKey': 'a'*64,
            'signingKey': 'b'*64, 'accountIndex': 0}
    config = {'market': 'ETH-USD', 'direction': 'long', 'lower': '2450', 'upper': '2550',
              'gridCount': 2, 'budget': '100', 'leverage': 1, 'marginMode': 'cross', 'mode': 'paper'}
    with TestClient(module.app) as client:
        assert client.delete('/api/strategies/missing').status_code == 401
        assert client.post('/api/login', json={'password': 'test-pass'}).status_code == 200
        first = client.post('/api/accounts', json=body).json()
        second = client.post('/api/accounts', json={**body, 'name': 'second', 'accountIndex': 1}).json()
        assert first['id'] != second['id'] and first['accountIndex'] == 0 and second['accountIndex'] == 1
        assert first['statistics']['turnover'] == '1000' and second['statistics']['turnover'] == '1001'
        assert client.post('/api/accounts', json=body).status_code == 409
        a = client.post('/api/strategies', json={**config, 'accountId': first['id']}).json()
        b = client.post('/api/strategies', json={**config, 'accountId': second['id']}).json()
        private_store.event('info', 'account-owned strategy event', a['id'])
        response = client.delete(f"/api/strategies/{a['id']}")
        assert response.status_code == 200 and response.json()['deleted']
        state = client.get('/api/state').json()
        assert [s['id'] for s in state['strategies']] == [b['id']]
        assert next(e for e in state['events'] if e['message'] == 'account-owned strategy event')['accountId'] == first['id']
        assert next(x for x in state['accounts'] if x['id'] == first['id'])['statistics']['turnover'] == '1000'
        assert client.delete(f"/api/strategies/{a['id']}").status_code == 404
        changed = private_store.get_strategy(b['id'])
        changed.update(status='running', desiredStatus='running')
        private_store.save_strategy(changed)
        assert client.delete(f"/api/strategies/{b['id']}").status_code == 409
        assert private_store.get_strategy(b['id']) is not None
        assert client.delete(f"/api/accounts/{first['id']}").status_code == 200
        assert metrics.forgotten == [first['id']]
        assert client.post(f"/api/accounts/{second['id']}/refresh").json()['equity'] == '101'
        assert module.clients[second['id']].credentials['accountIndex'] == 1
        assert all('apiKey' not in x and 'encrypted' not in x for x in client.get('/api/state').json()['accounts'])
    private_store.db.close()
