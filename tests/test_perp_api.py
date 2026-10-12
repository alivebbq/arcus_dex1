import importlib
import time

from fastapi.testclient import TestClient

from backend.store import Store
from backend.security import password_hash
from cryptography.fernet import Fernet


def test_contract_form_payload_creates_margin_strategy_and_keeps_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('VAULT_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('COOKIE_SECURE', '0')
    monkeypatch.setenv('PUBLIC_ORIGIN', 'http://testserver')
    monkeypatch.setenv('ADMIN_PASSWORD_HASH', password_hash('test-pass'))
    module = importlib.import_module('backend.app')
    private_store = Store(tmp_path, Fernet.generate_key())
    monkeypatch.setattr(module, 'store', private_store)
    monkeypatch.setattr(module.engine, 'store', private_store)
    monkeypatch.setattr(module, 'one_cycle', lambda: None)
    monkeypatch.setattr(module, 'markets_cache', [{'symbol': 'BTC-USD', 'marketId': 1, 'status': 'ONLINE',
        'tickSize': '0.1', 'stepSize': '0.00000001', 'minOrderSize': '0.0001', 'minOrderNotional': '5',
        'initialMarginFraction': '0.025', 'offHoursInitialMarginFraction': '0.025',
        'markPrice': '83000', 'regularTradingHours': None, 'isOutsideRth': False}])
    monkeypatch.setattr(module, 'markets_time', time.time())
    account = private_store.add_account('test', {'address': '0x' + 'a' * 40},
                                       {'connected': True, 'equity': '4.04', 'availableBalance': '4.04',
                                        'positions': [], 'orders': [], 'updatedAt': int(time.time() * 1000)})
    config = {'accountId': account['id'], 'market': 'BTC-USD', 'direction': 'long',
              'lower': '81000', 'upper': '85000', 'gridCount': 2, 'budget': '3',
              'budgetType': 'margin', 'leverage': 10, 'marginMode': 'cross', 'mode': 'paper'}
    with TestClient(module.app) as client:
        assert client.post('/api/login', json={'password': 'test-pass'}).status_code == 200
        preview = client.post('/api/strategies/preview', json=config)
        assert preview.status_code == 200
        assert preview.json()['errors'] == []
        assert preview.json()['notionalBudget'] == '30'
        assert float(preview.json()['requiredCollateral']) <= 3
        created = client.post('/api/strategies', json=config)
        assert created.status_code == 200
        strategy = created.json()
        assert strategy['budgetType'] == 'margin'
        assert strategy['leverage'] == 10
        assert strategy['status'] == 'draft'
        assert client.post('/api/strategies/preview', json={**config, 'leverage': 40.1}).status_code == 422
        assert client.post('/api/strategies/preview', json={**config, 'budgetType': 'notional'}).status_code == 422
