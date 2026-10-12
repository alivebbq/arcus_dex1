"""Compare the supplied test public key with the previous dashboard, read only.

Only a verified matching wallet address is returned; key material stays in memory.
"""
import json
import re
from pathlib import Path
from textwrap import dedent
try:
    from .remote import connect
    from .local_config import load_local_config, config_path, public_url, api_values, wallet_address, admin_password
except ImportError:
    from remote import connect
    from local_config import load_local_config, config_path, public_url, api_values, wallet_address, admin_password

def main():
    config = load_local_config(('apiCredentialsFile', 'sshCredentialsFile'))
    values = api_values(config)
    api_key = values['apikey'].removeprefix('0x').lower()
    script = '''
    import json,sqlite3,sys
    from pathlib import Path
    from cryptography.fernet import Fernet
    env=dict(line.split('=',1) for line in Path('/etc/arcus-control.env').read_text().splitlines() if '=' in line)
    db=sqlite3.connect('file:'+env['DATA_DIR']+'/control.sqlite3?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    target=sys.stdin.readline().strip()
    matches=[]
    for row in db.execute('SELECT address,account_index,credentials FROM accounts'):
        if not row['credentials']: continue
        obj=json.loads(Fernet(env['VAULT_KEY'].encode()).decrypt(row['credentials'].encode()))
        if any(str(v).removeprefix('0x').lower()==target for k,v in obj.items() if 'key' in k.lower() and 'sign' not in k.lower()):
            matches.append({'address':row['address'],'accountIndex':row['account_index']})
    print(json.dumps({'verifiedMatches':matches}))
    '''
    import base64
    client = connect()
    try:
        encoded = base64.b64encode(dedent(script).encode()).decode()
        stdin, stdout, stderr = client.exec_command("sudo /opt/arcus-control/venv/bin/python -c \"import base64;exec(base64.b64decode('" + encoded + "'))\"")
        stdin.write(api_key + '\n')
        stdin.flush()
        stdin.channel.shutdown_write()
        print(stdout.read().decode())
        print(stderr.read().decode())
    finally:
        client.close()


if __name__ == '__main__':
    main()
