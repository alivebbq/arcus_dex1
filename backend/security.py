import base64
import hashlib
import hmac
import os
import secrets
import time


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.b64encode(salt + digest).decode()


def check_password(password, encoded):
    try:
        raw = base64.b64decode(encoded)
        return hmac.compare_digest(password_hash(password, raw[:16]), encoded)
    except (ValueError, TypeError):
        return False


def create_session(store):
    token = secrets.token_urlsafe(32)
    with store.lock, store.db:
        store.db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
        store.db.execute('INSERT INTO sessions VALUES (?,?)',
                         (hashlib.sha256(token.encode()).hexdigest(), time.time() + 43200))
    return token


def authenticated(store, token):
    if not token:
        return False
    with store.lock:
        row = store.db.execute('SELECT expires FROM sessions WHERE token_hash=?',
                              (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return bool(row and row['expires'] > time.time())


def revoke_session(store, token):
    with store.lock, store.db:
        store.db.execute('DELETE FROM sessions WHERE token_hash=?',
                         (hashlib.sha256((token or '').encode()).hexdigest(),))
