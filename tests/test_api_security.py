import importlib

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.security import password_hash


def test_api_requires_session_rejects_origin_and_redacts_invalid_key(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('VAULT_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('ADMIN_PASSWORD_HASH', password_hash('test-password'))
    monkeypatch.setenv('COOKIE_SECURE', '0')
    monkeypatch.setenv('PUBLIC_ORIGIN', 'http://testserver')
    module = importlib.import_module('backend.app')
    monkeypatch.setattr(module, 'one_cycle', lambda: None)
    with TestClient(module.app) as client:
        assert client.get('/api/state').status_code == 401
        assert client.post('/api/login', json={'password': 'wrong'}).status_code == 401
        assert client.post('/api/login', json={'password': 'test-password'}).status_code == 200
        state = client.get('/api/state')
        assert state.status_code == 200
        assert state.json()['accounts'] == []
        result = client.post('/api/accounts', headers={'origin': 'https://foreign.invalid'}, json={})
        assert result.status_code == 403
        secret = 'private-key-material-for-invalid-test'
        result = client.post('/api/accounts', json={'name': 'test', 'address': 'bad', 'apiKey': secret,
                                                   'signingKey': secret})
        assert result.status_code == 422
        assert secret not in result.text
        assert 'signingKey' in result.text
        assert client.post('/api/logout').status_code == 200
        assert client.get('/api/state').status_code == 401
