"""Verify deployed source and authenticated responses without placing orders."""
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
import requests
try:
    from .remote import connect
    from .local_config import load_local_config, config_path, public_url, api_values, wallet_address, admin_password
except ImportError:
    from remote import connect
    from local_config import load_local_config, config_path, public_url, api_values, wallet_address, admin_password

ROOT = Path(__file__).resolve().parents[1]
def main():
    config = load_local_config(('publicUrl', 'sshCredentialsFile'))
    client = connect()
    try:
        sftp = client.open_sftp()
        files = list((ROOT / 'backend').glob('*.py')) + list((ROOT / 'frontend').glob('*'))
        mismatches = []
        for file in files:
            if not file.is_file():
                continue
            relative = file.relative_to(ROOT).as_posix()
            with sftp.open('/opt/arcus-grid/' + relative, 'rb') as remote:
                digest = hashlib.sha256(remote.read()).digest()
            if digest != hashlib.sha256(file.read_bytes()).digest():
                mismatches.append(relative)
        sftp.close()
        print(json.dumps({'checkedFiles': len(files), 'sourceMismatches': mismatches}))
        if mismatches:
            raise SystemExit(1)
    finally:
        client.close()

    url = public_url(config)
    password = re.search('登录密码：(.*)', (ROOT / 'deployment-access.txt').read_text(encoding='utf-8'))[1].strip()
    assert requests.get(url + '/api/state', timeout=15).status_code == 401
    session = requests.Session()
    session.headers['Origin'] = url
    login = session.post(url + '/api/login', json={'password': password}, timeout=15)
    assert login.status_code == 200
    assert all(cookie.secure for cookie in session.cookies)
    response = session.get(url + '/api/state', timeout=15)
    assert response.status_code == 200
    state = response.json()
    assert len(state['accounts']) >= 1 and state['accounts'][0]['connected']
    assert len(state['markets']) >= 1
    assert all('encrypted' not in a and 'signingKey' not in a and 'apiKey' not in a for a in state['accounts'])
    assert isinstance(state['server']['version'], str) and state['server']['version']
    assert state['server']['version'] == '1.3.0'
    for account in state['accounts']:
        assert isinstance(account['accountIndex'], int)
        assert 'statistics' in account and 'complete' in account['statistics']
        assert 'turnover' in account['statistics'] and 'realizedPnl' in account['statistics'] and 'fees' in account['statistics']
    signals = session.get(url + '/api/trend/signals', timeout=20)
    assert signals.status_code == 200
    signal_data = signals.json()
    assert signal_data['warmup'] and not signal_data['errors']
    assert signal_data['barCount'] >= 200 and len(signal_data['candles']) == 200
    assert signal_data['rules']['timeframe'] == '1h' and signal_data['rules']['adxMin'] == 20
    assert signal_data['rules']['reverseImmediately'] is False
    # Preview is calculation only: it neither changes a strategy nor sends an order.
    preview = session.post(url + '/api/trend/preview', json={
        'accountId': state['accounts'][0]['id'], 'market': 'ETH-USD', 'budget': '100',
        'budgetType': 'margin', 'leverage': 5, 'marginMode': 'isolated', 'mode': 'paper'
    }, timeout=20)
    assert preview.status_code == 200
    plan = preview.json()
    assert not plan['errors'] and plan['plan']
    assert Decimal(plan['plannedRisk']) <= Decimal('3')
    assert Decimal(plan['requiredCollateral']) <= Decimal('100')
    # Exercise deletion only on a disposable, never-started simulation.
    existing_ids = {strategy['id'] for strategy in state['strategies']}
    draft = session.post(url + '/api/trend/strategies', json={
        'accountId': state['accounts'][0]['id'], 'name': '部署删除接口验证（模拟草稿）',
        'market': 'ETH-USD', 'budget': '100', 'budgetType': 'margin',
        'leverage': 5, 'marginMode': 'isolated', 'mode': 'paper'
    }, timeout=20)
    assert draft.status_code == 200 and draft.json()['status'] == 'draft'
    draft_id = draft.json()['id']
    deleted = session.delete(url + '/api/strategies/' + draft_id, timeout=20)
    assert deleted.status_code == 200 and deleted.json()['deleted'] is True
    after = session.get(url + '/api/state', timeout=20).json()
    assert draft_id not in {strategy['id'] for strategy in after['strategies']}
    assert existing_ids <= {strategy['id'] for strategy in after['strategies']}
    print(json.dumps({'https': True, 'authenticatedState': response.status_code, 'unauthenticatedState': 401,
                      'equity': state['accounts'][0]['equity'], 'marketCount': len(state['markets']),
                      'paperStatuses': [s['status'] for s in state['strategies'] if s['mode'] == 'paper'],
                      'version': state['server']['version'], 'trendFinalBars': signal_data['barCount'],
                      'trendPreviewRisk': plan['plannedRisk'], 'trendPreviewOnly': True,
                      'paperDraftDeletionVerified': True, 'existingStrategyRecordsRetained': True,
                      'liveStrategyCount': sum(s['status'] == 'running' and s['mode'] == 'live' for s in state['strategies']),
                      'testSubmittedLiveOrders': False, 'secretsInPublicAccounts': False}))


if __name__ == '__main__':
    main()
