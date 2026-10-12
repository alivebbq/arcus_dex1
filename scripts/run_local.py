"""Private local preview with generated development-only credentials."""
import os
import json
import secrets
import sys
import argparse
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.security import password_hash
from cryptography.fernet import Fernet
import uvicorn

def main():
    data = ROOT / 'data' / 'preview'
    data.mkdir(parents=True, exist_ok=True)
    runtime_path = data / 'runtime.json'
    if not runtime_path.exists():
        password = secrets.token_urlsafe(18)
        runtime_path.write_text(json.dumps({'VAULT_KEY': Fernet.generate_key().decode(),
                                          'ADMIN_PASSWORD_HASH': password_hash(password)}), encoding='utf-8')
        (data / 'access.txt').write_text(password, encoding='utf-8')
    runtime = json.loads(runtime_path.read_text(encoding='utf-8'))
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    port = args.port
    if port == 0:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
    os.environ.update(DATA_DIR=str(data), **runtime, COOKIE_SECURE='0',
                      PUBLIC_ORIGIN=f'http://127.0.0.1:{port}')
    print(f'Local preview: http://127.0.0.1:{port}', flush=True)
    uvicorn.run('backend.app:app', host='127.0.0.1', port=port, access_log=False)


if __name__ == '__main__':
    main()
