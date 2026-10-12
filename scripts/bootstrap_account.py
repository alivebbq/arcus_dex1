"""Import the user-authorized test account and exercise paper mode only."""
import argparse
import json
import re
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
import requests

try:
    from .local_config import load_local_config, public_url, api_values, wallet_address
except ImportError:
    from local_config import load_local_config, public_url, api_values, wallet_address

ROOT = Path(__file__).resolve().parents[1]
def main():
    config = load_local_config(('apiCredentialsFile', 'walletAddress'))
    parser = argparse.ArgumentParser()
    parser.add_argument('--local', action='store_true')
    parser.add_argument('--url')
    args = parser.parse_args()
    if args.local:
        url = args.url or 'http://127.0.0.1:8787'
        password = (ROOT / 'data/preview/access.txt').read_text(encoding='utf-8')
    else:
        url = args.url or public_url(config)
        access = (ROOT / 'deployment-access.txt').read_text(encoding='utf-8')
        password = re.search(r'登录密码：(.*)', access)[1].strip()

    values = api_values(config)
    address = wallet_address(config)
    session = requests.Session()
    session.headers['Origin'] = url


    def call(method, path, body=None):
        result = session.request(method, url + path, json=body, timeout=60)
        if result.status_code >= 400:
            # Server validation is sanitized; do not print request content.
            raise RuntimeError(f'{path}: {result.status_code} {result.text[:600]}')
        return result.json()


    call('POST', '/api/login', {'password': password})
    state = call('GET', '/api/state')
    account = next((a for a in state['accounts'] if a['address'] == address), None)
    if not account:
        account = call('POST', '/api/accounts', {'name': values.get('name', 'Test account'), 'apiKey': values['apikey'],
                        'signingKey': values['API Signing Key'],
                        'address': address, 'accountIndex': 0})
    state = call('GET', '/api/state')
    market = next(m for m in state['markets'] if m['symbol'] == 'BTC-USD')
    tick = Decimal(market['tickSize'])
    price = Decimal(market['markPrice'])
    quantize = lambda v: format((v / tick).to_integral_value(rounding=ROUND_FLOOR) * tick, 'f')
    config = {'name': 'BTC 验证网格（模拟）', 'accountId': account['id'], 'market': 'BTC-USD',
              'direction': 'long', 'lower': quantize(price * Decimal('0.97')),
              'upper': quantize(price * Decimal('1.03')), 'gridCount': 8, 'budget': '100', 'mode': 'paper'}
    preview = call('POST', '/api/strategies/preview', config)
    if preview['errors']:
        raise RuntimeError('Paper preview validation failed: ' + str(preview['errors']))
    strategy = next((s for s in state['strategies'] if s['name'] == config['name'] and s['mode'] == 'paper'), None)
    if not strategy:
        strategy = call('POST', '/api/strategies', config)
    call('POST', '/api/strategies/' + strategy['id'] + '/start')
    result = call('POST', '/api/strategies/' + strategy['id'] + '/pause')
    final = call('GET', '/api/state')
    public_accounts = json.dumps(final['accounts'])
    if values['apikey'] in public_accounts or values['API Signing Key'] in public_accounts:
        raise RuntimeError('Secret exposure in public account response')
    print(json.dumps({'accountConnected': account['connected'], 'equity': account['equity'],
                      'availableBalance': account['availableBalance'], 'marketCount': len(final['markets']),
                      'paperPreview': preview, 'paperStatus': result['status'],
                      'liveStrategiesStarted': sum(s['mode'] == 'live' and s['status'] == 'running' for s in final['strategies']),
                      'secretExposure': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
