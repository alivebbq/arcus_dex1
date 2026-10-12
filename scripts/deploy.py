"""Deploy isolated service without altering existing site configurations.

Runtime secrets flow in encrypted SSH stdin. Access information stays in ignored
local deployment-access.txt. Re-running preserves the vault and login password.
"""
import base64
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from textwrap import dedent
try:
    from .remote import connect
    from .local_config import load_local_config, config_path, public_url, api_values, wallet_address, admin_password
except ImportError:
    from remote import connect
    from local_config import load_local_config, config_path, public_url, api_values, wallet_address, admin_password

ROOT = Path(__file__).resolve().parents[1]
def main():
    config = load_local_config(('publicUrl', 'sshCredentialsFile'))
    URL = public_url(config)
    client = connect()


    def run(command, stdin_data=None):
        stdin, out, err = client.exec_command(command, timeout=180)
        if stdin_data is not None:
            stdin.write(stdin_data)
            stdin.flush()
            stdin.channel.shutdown_write()
        output = out.read().decode('utf-8', errors='replace')
        error = err.read().decode('utf-8', errors='replace')
        status = out.channel.recv_exit_status()
        print(output)
        print(error)
        if status:
            raise RuntimeError(f'Remote step failed ({status})')


    try:
        run('sudo install -d -m 755 /opt/arcus-grid; sudo chown -R ubuntu:ubuntu /opt/arcus-grid; sudo install -d -m 700 /var/lib/arcus-grid; id arcusgrid >/dev/null 2>&1 || sudo useradd --system --home /var/lib/arcus-grid --shell /usr/sbin/nologin arcusgrid')
        sftp = client.open_sftp()
        files = list((ROOT / 'backend').glob('*.py')) + list((ROOT / 'frontend').glob('*')) + [ROOT / 'requirements.txt']
        for folder in ('backend', 'frontend', 'ops'):
            try:
                sftp.mkdir('/opt/arcus-grid/' + folder)
            except OSError:
                pass
        files += list((ROOT / 'ops').glob('arcus-grid.*'))
        for file in files:
            if file.is_file():
                relative = file.relative_to(ROOT).as_posix()
                if relative == 'ops/arcus-grid.caddy':
                    template = file.read_text(encoding='utf-8')
                    if template.count('trade.example.com') != 1:
                        raise ValueError('Caddy template must contain exactly one generic hostname placeholder')
                    hostname = urlsplit(URL).hostname
                    rendered = template.replace('trade.example.com', hostname, 1)
                    with sftp.open('/opt/arcus-grid/' + relative, 'w') as output_file:
                        output_file.write(rendered)
                else:
                    sftp.put(str(file), '/opt/arcus-grid/' + relative)
        sftp.close()
        run('python3 -m venv /opt/arcus-grid/venv; /opt/arcus-grid/venv/bin/pip install --disable-pip-version-check -q -r /opt/arcus-grid/requirements.txt')
        run('sudo chown -R root:root /opt/arcus-grid; sudo chown -R arcusgrid:arcusgrid /var/lib/arcus-grid; sudo install -m 644 /opt/arcus-grid/ops/arcus-grid.service /etc/systemd/system/arcus-grid.service; sudo install -m 644 /opt/arcus-grid/ops/arcus-grid.caddy /etc/caddy/arcus-grid.conf')
        password = secrets.token_urlsafe(18)
        # Secret is passed only through stdin. The setup emits existence, never values.
        setup = '''
    import sys,json,os
    from pathlib import Path
    from urllib.parse import urlsplit
    sys.path.insert(0,'/opt/arcus-grid')
    from backend.security import password_hash
    from cryptography.fernet import Fernet
    v=json.loads(sys.stdin.read())
    p=Path('/etc/arcus-grid.env')
    if not p.exists():
        p.write_text('PUBLIC_ORIGIN='+v['url']+'\\nCOOKIE_SECURE=1\\nDATA_DIR=/var/lib/arcus-grid\\nADMIN_PASSWORD_HASH='+password_hash(v['password'])+'\\nVAULT_KEY='+Fernet.generate_key().decode()+'\\n')
        os.chmod(p,0o600)
        print('Runtime configuration created')
    else:
        print('Existing runtime configuration preserved')
    caddy=Path('/etc/caddy/Caddyfile')
    text=caddy.read_text()
    line='import /etc/caddy/arcus-grid.conf'
    if line not in text:
        Path('/etc/caddy/Caddyfile.before-arcus-grid').write_text(text)
        caddy.write_text(text.rstrip()+'\\n\\n'+line+'\\n')
    '''
        encoded = base64.b64encode(dedent(setup).encode()).decode()
        # Only save the access file on first provisioning. Check before setup.
        _, out, _ = client.exec_command('sudo test -f /etc/arcus-grid.env')
        existed = out.channel.recv_exit_status() == 0
        run("sudo /opt/arcus-grid/venv/bin/python -c \"import base64;exec(base64.b64decode('" + encoded + "'))\"",
            json.dumps({'url': URL, 'password': password}))
        if not existed:
            (ROOT / 'deployment-access.txt').write_text('Arcus 自动交易面板\n网址：'+URL+'\n登录密码：'+password+'\n\n请妥善保管。API Key 与签名私钥不会显示在本文件中。\n', encoding='utf-8')
        run('sudo caddy validate --config /etc/caddy/Caddyfile')
        run('sudo systemctl daemon-reload && sudo systemctl enable --now arcus-grid && sudo systemctl restart arcus-grid')
        run('sudo systemctl reload caddy && systemctl is-active arcus-grid caddy')
        print('Deployment started: ' + URL)
    finally:
        client.close()


if __name__ == '__main__':
    main()
