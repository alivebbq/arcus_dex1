"""SSH utility; reads user supplied credentials in memory, never logs them."""
import argparse
from pathlib import Path
import paramiko
try:
    from .local_config import config_path
except ImportError:
    from local_config import config_path


def connect():
    lines = [x.strip() for x in config_path('sshCredentialsFile').read_text(encoding='utf-8-sig').splitlines() if x.strip()]
    if len(lines) < 3:
        raise ValueError('The private SSH credential file must contain host, username, and password lines')
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    # First connection fingerprint is recorded for subsequent operations.
    known = Path.home() / '.ssh' / 'known_hosts'
    known.parent.mkdir(exist_ok=True)
    if known.exists():
        client.load_host_keys(str(known))
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(lines[0], username=lines[1], password=lines[2], timeout=20, look_for_keys=False, allow_agent=False)
    client.save_host_keys(str(known))
    return client


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--command', required=True)
    args = parser.parse_args()
    client = connect()
    try:
        _, out, err = client.exec_command(args.command, timeout=120)
        print(out.read().decode('utf-8', errors='replace'))
        print(err.read().decode('utf-8', errors='replace'))
        raise SystemExit(out.channel.recv_exit_status())
    finally:
        client.close()
