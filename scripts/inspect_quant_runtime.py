"""Read the currently running quant strategy, excluding credentials and account data."""
import base64
import hashlib
import json
from pathlib import Path
from textwrap import dedent
try:
    from .remote import connect
    from .local_config import load_local_config, config_path, public_url, api_values, wallet_address, admin_password
except ImportError:
    from remote import connect
    from local_config import load_local_config, config_path, public_url, api_values, wallet_address, admin_password

ROOT = Path(__file__).resolve().parents[1]
def main():
    config = load_local_config(('sshCredentialsFile', 'quantSourceDir'))
    script = r'''
    import json,inspect,hashlib,subprocess
    from pathlib import Path
    from nakedquant.desktop_service import DesktopService
    root=Path('/opt/nakedquant/runtime-eth')
    status=json.loads((root/'logs/desktop-live/status.json').read_text())
    pkg=Path(inspect.getfile(DesktopService)).parent
    names=['desktop_service.py','strategy_supertrend.py','maker_execution.py','config.py']
    print(json.dumps({'service':subprocess.run(['systemctl','is-active','nakedquant'],capture_output=True,text=True).stdout.strip(),
     'statusKeys':sorted(status),
     'strategyFields':{k:status.get(k) for k in ['symbol','band_atr','stop_atr','trade_risk','breakeven_r','offset',
                                             'exit_offset','stop_limit_offset','stop_limit_timeout_seconds',
                                             'risk_capital_source','account_scope','retry_seconds']},
     'state':{k:status.get(k) for k in ['updated_ms','enabled','halt','phase','worker_alive']},
     'sourceHashes':{name:hashlib.sha256((pkg/name).read_bytes()).hexdigest() for name in names},
     'sourceFiles':{name:(pkg/name).read_text() for name in names}},ensure_ascii=True))
    '''
    client = connect()
    try:
        encoded = base64.b64encode(dedent(script).encode()).decode()
        _, out, err = client.exec_command("sudo -n -u nakedquant /opt/nakedquant/venv/bin/python -c \"import base64;exec(base64.b64decode('" + encoded + "'))\"", timeout=30)
        raw = out.read().decode()
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read().decode()[:500])
        result = json.loads(raw)
        directory = ROOT / 'data' / 'quant-reference'
        directory.mkdir(parents=True, exist_ok=True)
        sources = result.pop('sourceFiles')
        for name, code in sources.items():
            (directory / name).write_text(code, encoding='utf-8')
        original = config_path('quantSourceDir', config)
        result['sourceMatchesLocal'] = {name: hashlib.sha256((original/name).read_bytes()).hexdigest() == digest
                                       for name,digest in result['sourceHashes'].items()}
        (directory / 'runtime-evidence.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=True))
    finally:
        client.close()


if __name__ == '__main__':
    main()
