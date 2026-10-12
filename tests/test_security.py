from cryptography.fernet import Fernet

from backend.security import password_hash, check_password, create_session, authenticated, revoke_session
from backend.store import Store


def test_login_session_revocation_and_encrypted_storage(tmp_path):
    store = Store(tmp_path, Fernet.generate_key())
    digest = password_hash('test-pass-only')
    assert check_password('test-pass-only', digest)
    assert not check_password('incorrect', digest)
    token = create_session(store)
    assert authenticated(store, token)
    assert not authenticated(store, 'invalid-token')
    revoke_session(store, token)
    assert not authenticated(store, token)
    credentials = {'address': '0x' + 'a' * 40, 'apiKey': 'b' * 64, 'signingKey': 'c' * 64}
    account = store.add_account('测试', credentials, {'equity': None, 'connected': False})
    assert credentials['signingKey'] not in account['encrypted']
    assert credentials['signingKey'].encode() not in (tmp_path / 'arcus.sqlite3').read_bytes()
    assert store.credentials(account) == credentials
    assert store.get_account(account['id'])['snapshot']['equity'] is None
