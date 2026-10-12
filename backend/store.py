"""Private SQLite state. Credentials never leave the encrypted account record."""
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory, vault_key):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.vault = Fernet(vault_key.encode() if isinstance(vault_key, str) else vault_key)
        self.db = sqlite3.connect(directory / 'arcus.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                address TEXT NOT NULL, encrypted TEXT NOT NULL, snapshot TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                level TEXT NOT NULL, message TEXT NOT NULL, strategy_id TEXT);
            CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
        ''')
        self.db.commit()
        try:
            os.chmod(directory / 'arcus.sqlite3', 0o600)
        except OSError:
            pass

    def add_account(self, name, credentials, snapshot):
        account_id = uuid.uuid4().hex
        encrypted = self.vault.encrypt(json.dumps(credentials).encode()).decode()
        with self.lock, self.db:
            self.db.execute('INSERT INTO accounts VALUES (?,?,?,?,?)',
                            (account_id, name, credentials['address'], encrypted, json.dumps(snapshot)))
        return self.get_account(account_id)

    def get_account(self, account_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        return self._account(row) if row else None

    @staticmethod
    def _account(row):
        result = dict(row)
        result['snapshot'] = json.loads(result['snapshot'])
        return result

    def accounts(self):
        with self.lock:
            return [self._account(row) for row in self.db.execute('SELECT * FROM accounts ORDER BY rowid')]

    def credentials(self, account):
        return json.loads(self.vault.decrypt(account['encrypted'].encode()))

    def save_snapshot(self, account_id, snapshot):
        with self.lock, self.db:
            self.db.execute('UPDATE accounts SET snapshot=? WHERE id=?', (json.dumps(snapshot), account_id))

    def delete_account(self, account_id):
        with self.lock, self.db:
            self.db.execute('DELETE FROM accounts WHERE id=?', (account_id,))

    def save_strategy(self, strategy):
        with self.lock, self.db:
            self.db.execute('INSERT INTO strategies VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                            (strategy['id'], json.dumps(strategy)))

    def strategies(self):
        with self.lock:
            return [json.loads(row['data']) for row in self.db.execute('SELECT data FROM strategies ORDER BY rowid DESC')]

    def get_strategy(self, strategy_id):
        with self.lock:
            row = self.db.execute('SELECT data FROM strategies WHERE id=?', (strategy_id,)).fetchone()
        return json.loads(row['data']) if row else None

    def event(self, level, message, strategyId=None):
        with self.lock, self.db:
            self.db.execute('INSERT INTO events(time,level,message,strategy_id) VALUES(?,?,?,?)',
                            (time.time(), level, message[:1000], strategyId))
            self.db.execute('DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-2000 FROM events)')

    def events(self):
        with self.lock:
            return [{'id': r['id'], 'time': int(r['time'] * 1000), 'level': r['level'],
                     'message': r['message'], 'strategyId': r['strategy_id']}
                    for r in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 150')]
