"""The trend workbench routes use finalized data and never start historical trades."""
import importlib
import time
from decimal import Decimal

from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from backend.store import Store
from backend.security import password_hash


def test_trend_routes_monitoring_and_same_market_exclusion(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('VAULT_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('COOKIE_SECURE', '0')
    monkeypatch.setenv('PUBLIC_ORIGIN', 'http://testserver')
    monkeypatch.setenv('ADMIN_PASSWORD_HASH', password_hash('test-pass'))
    module = importlib.import_module('backend.app')
    private_store = Store(tmp_path, Fernet.generate_key())
    monkeypatch.setattr(module, 'store', private_store)
    monkeypatch.setattr(module.engine, 'store', private_store)
    monkeypatch.setattr(module.trend_engine, 'store', private_store)
    monkeypatch.setattr(module, 'one_cycle', lambda: None)
    hour = int(time.time()) // 3600 * 3600
    bars = [{'openTime': (hour - (300-i)*3600) * 1_000_000, 'open': '2500', 'high': '2505',
             'low': '2495', 'close': '2500', 'isFinal': True} for i in range(300)]
    market = {'symbol': 'ETH-USD', 'marketDisplayName': 'ETH-USD', 'marketId': 2, 'status': 'ONLINE',
              'tickSize': '0.01', 'stepSize': '0.0000001', 'minOrderSize': '0.001', 'minOrderNotional': '5',
              'initialMarginFraction': '0.04', 'offHoursInitialMarginFraction': '0.04',
              'markPrice': '2500', 'makerFee': '0', 'takerFee': '0.000225',
              'regularTradingHours': None, 'isOutsideRth': False}
    monkeypatch.setattr(module, 'markets_cache', [market])
    monkeypatch.setattr(module, 'markets_time', time.time())
    monkeypatch.setattr(module, 'trend_candle_cache', {'ETH-USD': {'time': time.time(), 'candles': bars, 'error': None}})
    account = private_store.add_account('test', {'address': '0x' + 'a'*40},
        {'connected': True, 'equity': '4.04', 'availableBalance': '4.04', 'positions': [], 'orders': [],
         'updatedAt': int(time.time()*1000)})
    body = {'accountId': account['id'], 'market': 'ETH-USD', 'budget': '100', 'budgetType': 'margin',
            'leverage': 5, 'marginMode': 'isolated', 'mode': 'paper'}
    with TestClient(module.app) as client:
        assert client.get('/api/trend/signals').status_code == 401
        assert client.post('/api/login', json={'password': 'test-pass'}).status_code == 200
        signal = client.get('/api/trend/signals').json()
        assert signal['warmup'] and signal['rules']['timeframe'] == '1h'
        assert len(signal['candles']) == 200
        preview = client.post('/api/trend/preview', json=body)
        assert preview.status_code == 200 and not preview.json()['errors']
        # No flip still permits creation of a draft that waits for future valid signals.
        assert not preview.json()['eligible']
        created = client.post('/api/trend/strategies', json=body)
        assert created.status_code == 200
        strategy = created.json()
        assert strategy['type'] == 'trend' and strategy['status'] == 'draft'
        started = client.post(f"/api/strategies/{strategy['id']}/start")
        assert started.status_code == 200 and started.json()['status'] == 'running'
        assert started.json()['orders'] == []
        assert Decimal(started.json()['residualQuantity']) == 0
        grid = client.post('/api/strategies', json={'accountId': account['id'], 'market': 'ETH-USD',
            'direction': 'long', 'lower': '2450', 'upper': '2550', 'gridCount': 2,
            'budget': '100', 'leverage': 1, 'marginMode': 'cross', 'mode': 'paper'}).json()
        assert client.post(f"/api/strategies/{grid['id']}/start").status_code == 409
        paused = client.post(f"/api/strategies/{strategy['id']}/pause")
        assert paused.status_code == 200 and paused.json()['status'] == 'paused'
        assert client.post('/api/trend/preview', json={**body, 'leverage': 6}).status_code == 422
        assert client.get('/api/trend/signals?market=BTC-USD').status_code == 400
    private_store.db.close()


def test_worker_keeps_trend_risk_management_when_other_margin_or_bbo_read_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('VAULT_KEY', Fernet.generate_key().decode())
    module = importlib.import_module('backend.app')
    private_store = Store(tmp_path, Fernet.generate_key())
    monkeypatch.setattr(module, 'store', private_store)
    monkeypatch.setattr(module.engine, 'store', private_store)
    monkeypatch.setattr(module.trend_engine, 'store', private_store)
    stamp = int(time.time()*1000)
    market = {'symbol': 'ETH-USD', 'marketId': 2, 'status': 'ONLINE', 'tickSize': '0.01',
        'stepSize': '0.0000001', 'minOrderSize': '0.001', 'minOrderNotional': '5',
        'initialMarginFraction': '0.04', 'offHoursInitialMarginFraction': '0.04',
        'markPrice': '2500', 'makerFee': '0', 'takerFee': '0.000225', 'isOutsideRth': False}
    account = private_store.add_account('test', {'address': '0x' + 'b'*40},
        {'connected': True, 'equity': '1000', 'availableBalance': '1000', 'positions': [],
         'orders': [], 'updatedAt': stamp})
    trend = module.trend_engine.create({'accountId': account['id'], 'budget': '100',
        'market': 'ETH-USD', 'mode': 'live', 'leverage': 5, 'marginMode': 'isolated'}, market)
    trend.update(status='stopped', desiredStatus='stopped', hasStarted=True)
    trend['slots'][0].update(inventory='0.4', entryCost='1000')
    module.trend_engine._save(trend)
    other = module.engine.create({'accountId': account['id'], 'market': 'BTC-USD',
        'direction': 'long', 'lower': '2400', 'upper': '2600', 'gridCount': 2,
        'budget': '100', 'leverage': 1, 'marginMode': 'cross', 'mode': 'live'}, market)
    other.update(status='paused', desiredStatus='paused', hasStarted=True)
    private_store.save_strategy(other)
    calls = []
    monkeypatch.setattr(module.trend_engine, 'tick', lambda s, a, m: calls.append((s, m)))
    monkeypatch.setattr(module, 'fee_time', time.time())
    monkeypatch.setattr(module.public_client, 'markets', lambda: [market])
    def failed_bbo(*args):
        raise RuntimeError('BBO unavailable')
    monkeypatch.setattr(module.public_client, 'bbo', failed_bbo)
    monkeypatch.setattr(module, 'trend_market', lambda m: m)
    module.one_cycle()
    assert len(calls) == 1  # Holding, stopped trend still reaches exit reconciliation.
    assert calls[0][0]['suspended']
    assert calls[0][1]['availableForStrategy'] == '0'
    assert calls[0][1]['markPrice'] == '2500'  # A BBO failure doesn't erase a fresh mark.
    assert calls[0][1]['bestBid'] is None and calls[0][1]['bestAsk'] is None
    assert '预留保证金尚未确认' in calls[0][0]['error']
    private_store.db.close()
