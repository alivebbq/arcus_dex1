"""Ignored machine-local settings; credentials remain in separate private files."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def load_local_config(required=()):
    config_file = Path(os.environ.get('ARCUS_DEPLOYMENT_CONFIG', ROOT / 'deployment.local.json'))
    if not config_file.is_absolute():
        config_file = ROOT / config_file
    if not config_file.is_file():
        raise ValueError('Create ignored deployment.local.json from deployment.example.json before running this utility')
    config = json.loads(config_file.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('Local deployment configuration must be a JSON object')
    missing = [key for key in required if not isinstance(config.get(key), str) or not config[key].strip()]
    if missing:
        raise ValueError('Local deployment configuration is missing: ' + ', '.join(missing))
    return config


def config_path(key, config=None):
    config = load_local_config((key,)) if config is None else config
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Local deployment configuration is missing: ' + key)
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else ROOT / path


def public_url(config=None):
    config = load_local_config(('publicUrl',)) if config is None else config
    value = str(config.get('publicUrl', '')).rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path
            or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname)):
        raise ValueError('publicUrl must be an HTTPS origin with a valid hostname')
    if parsed.hostname == 'example.com' or parsed.hostname.endswith('.example.com'):
        raise ValueError('Replace the example publicUrl in ignored deployment.local.json before deployment')
    return value


def api_values(config=None):
    config = load_local_config(('apiCredentialsFile',)) if config is None else config
    lines = config_path('apiCredentialsFile', config).read_text(encoding='utf-8-sig').splitlines()
    values = dict((parts[0].strip(), parts[1].strip()) for line in lines
                  if len(parts := re.split(r'[:=：]', line, maxsplit=1)) == 2)
    if not values.get('apikey') or not values.get('API Signing Key'):
        raise ValueError('The private API credential file is missing required key fields')
    return values


def wallet_address(config=None):
    config = load_local_config(('walletAddress',)) if config is None else config
    address = str(config.get('walletAddress', '')).lower()
    if not re.fullmatch(r'0x[0-9a-f]{40}', address):
        raise ValueError('walletAddress must be a valid Ethereum address in ignored deployment.local.json')
    return address


def admin_password(local=False):
    path = ROOT / 'data/preview/access.txt' if local else ROOT / 'deployment-access.txt'
    text = path.read_text(encoding='utf-8')
    if local:
        return text.strip()
    match = re.search(r'登录密码：(.*)', text)
    if not match:
        raise ValueError('The ignored deployment access file does not contain the login password')
    return match[1].strip()
