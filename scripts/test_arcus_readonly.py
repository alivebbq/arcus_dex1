"""Explicit official read-only smoke utility. Importing performs no network I/O."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.arcus import ArcusClient
try:
    from .local_config import api_values, load_local_config, wallet_address
except ImportError:
    from local_config import api_values, load_local_config, wallet_address


def main():
    config = load_local_config(('apiCredentialsFile', 'walletAddress'))
    values = api_values(config)
    credentials = {'apiKey': values['apikey'], 'signingKey': values['API Signing Key'],
                   'address': wallet_address(config), 'accountIndex': 0}
    with ArcusClient(credentials) as client:
        valid = client.validate_key()
        snap = client.account()
        markets = client.markets()
        bbo = client.bbo('BTC-USD')
        candles = client.candles('BTC-USD')
        print(json.dumps({'keyValid': valid['valid'], 'walletVerified': True,
                          'equity': snap['equity'], 'availableBalance': snap['availableBalance'],
                          'positionsCount': len(snap['positions']), 'ordersCount': len(snap['orders']),
                          'marketCount': len(markets), 'bbo': bbo,
                          'candlesCount': len(candles), 'fees': client.fee_tiers()[:1]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
