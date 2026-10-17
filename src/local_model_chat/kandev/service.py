"""Version-scoped Kandev service lifecycle, independent of the board renderer."""
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler, Request
from urllib.parse import urlparse
from ..config import data_dir
from .ui_config import kandev_board_url


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Kandev redirected outside its configured address')


def get_json(origin, path):
    origin = kandev_board_url({'KANDEV_ENDPOINT': origin})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(Request(origin.rstrip('/') + path, headers={'Accept': 'application/json'}), timeout=3) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError('Kandev response is too large')
    return json.loads(data)


def identity(pid):
    if os.name == 'nt':
        command = f'Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}" | Select-Object ExecutablePath,CreationDate | ConvertTo-Json -Compress'
        result = subprocess.run(['powershell', '-NoProfile', '-Command', command], capture_output=True,
            text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        return result.stdout.strip()
    result = subprocess.run(['ps', '-p', str(int(pid)), '-o', 'lstart=,args='], capture_output=True, text=True, timeout=5)
    return result.stdout.strip()


class KandevServiceController:
    def __init__(self, root=None):
        self.root = Path(root or data_dir())
        self.home = self.root / 'kandev-managed'
        self.receipt = self.home / 'service.json'

    def start(self, origin):
        origin = kandev_board_url({'KANDEV_ENDPOINT': origin})
        try:
            health = get_json(origin, '/health')
        except Exception:
            health = None
        if health:
            raise ValueError('A service already answers here. Connect to it or choose another port.')
        import socket
        parsed = urlparse(origin)
        with socket.socket(socket.AF_INET6 if parsed.hostname == '::1' else socket.AF_INET) as probe:
            probe.bind((parsed.hostname, parsed.port))
        if self.receipt.is_file():
            prior = json.loads(self.receipt.read_text())
            if prior.get('identity') and identity(prior['pid']) == prior['identity']:
                raise ValueError('A managed process is already running. Reconnect or stop it before starting another.')
        receipts = sorted((self.root / 'tools').glob('kandev-*/install-receipt.json'))
        installed = next((json.loads(p.read_text()) for p in receipts if json.loads(p.read_text()).get('version') == '0.97.0'), None)
        if installed is None:
            raise ValueError('Install the pinned Kandev package in Environment readiness first.')
        binary = Path(installed['executable']).resolve()
        if not binary.is_relative_to((self.root / 'tools').resolve()) or not binary.is_file():
            raise ValueError('Managed executable receipt is invalid')
        result = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if '0.97.0' not in result.stdout + result.stderr:
            raise ValueError('Managed Kandev version differs from the pinned launcher')
        self.home.mkdir(parents=True, exist_ok=True)
        config = self.home / 'config.yaml'
        if not config.exists():
            config.write_text('server:\n  host: 127.0.0.1\n', encoding='utf-8')
        env = dict(os.environ, KANDEV_HOME_DIR=str(self.home), KANDEV_BUNDLE_DIR=str(binary.parent.parent),
            KANDEV_SERVER_HOST=parsed.hostname, KANDEV_BACKEND_PORT=str(parsed.port), KANDEV_PORT=str(parsed.port))
        with (self.home / 'stdout.log').open('ab') as out, (self.home / 'stderr.log').open('ab') as err:
            process = subprocess.Popen([str(binary), '__backend'], env=env, cwd=self.home,
                stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt')
        process_identity = identity(process.pid)
        self.receipt.write_text(json.dumps({'pid': process.pid, 'identity': process_identity, 'origin': origin,
            'executable': str(binary)}), encoding='utf-8')
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Kandev exited during startup. See kandev-managed/stderr.log.')
            try:
                health = get_json(origin, '/ready')
                if (str(health.get('version', '')).lstrip('v') == '0.97.0'
                        and health.get('startup', {}).get('phase') == 'ready'):
                    return health
            except Exception:
                pass
            time.sleep(0.5)
        process.terminate()
        raise TimeoutError('Kandev did not become ready within the startup window')

    def stop(self, origin):
        if not self.receipt.is_file():
            raise ValueError('This service is attached, not managed by MoliCoder.')
        receipt = json.loads(self.receipt.read_text())
        if receipt['origin'] != origin or not receipt['identity'] or identity(receipt['pid']) != receipt['identity']:
            raise ValueError('Process ownership changed; refusing to stop it.')
        # Query every agent execution, including sessions started in the native board.
        from .socket_rpc import call_kandev_socket
        response = call_kandev_socket(origin, '', 'agent.list', {})
        agents = response.get('agents')
        if not isinstance(agents, list):
            raise ValueError('Cannot prove the service has no active agents; stop it through Kandev.')
        if any(str(a.get('status', '')).lower() not in {'stopped', 'completed', 'failed', 'cancelled', 'exited'} for a in agents):
            raise ValueError('Kandev has active sessions. Finish or stop them in the board first.')
        if identity(receipt['pid']) != receipt['identity']:
            raise ValueError('Process ownership changed')
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/PID', str(receipt['pid'])], check=True, capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            os.kill(receipt['pid'], signal.SIGTERM)
        self.receipt.unlink(missing_ok=True)
        return 'Stopped managed Kandev'
