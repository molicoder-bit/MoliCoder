"""Provider login and an isolated, nonce-checked model connectivity test.

Official login commands own credentials. Login exit codes and credential
inventories are not proof of model access; only a successful test is Ready.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from ..molicoder.providers import create_provider, redact_text


CONNECTION_KEY = 'providers.connection_checks.v1'
AUTH_REQUIRED = re.compile(r'not logged in|not authenticated|authentication (?:required|failed)|unauthorized|\b401\b|'
    r'please (?:log|sign) in|(?:log|sign)[- ]?in (?:required|needed)|no (?:authentication|credentials)|'
    r'(?:invalid|missing|expired).{0,20}(?:api.?key|token)|(?:api.?key|token).{0,20}(?:invalid|missing|expired)', re.I)


@dataclass(frozen=True)
class ConnectionResult:
    provider: str
    model: str
    state: str
    detail: str
    checked_at: str
    binary: str = ''
    binary_mtime: int = 0


def result(provider, model, state, detail, binary=None):
    return ConnectionResult(provider, model, state, redact_text(detail)[:900], datetime.now(timezone.utc).isoformat(),
        str(binary or ''), binary.stat().st_mtime_ns if binary and binary.is_file() else 0)


def save_connection(store, checked):
    try:
        rows = json.loads(store.get_setting(CONNECTION_KEY, '{}'))
    except ValueError:
        rows = {}
    rows[checked.provider + ':' + checked.model] = asdict(checked)
    store.set_setting(CONNECTION_KEY, json.dumps(rows))


def forget_connections(store, provider):
    try: rows = json.loads(store.get_setting(CONNECTION_KEY, '{}'))
    except ValueError: return
    store.set_setting(CONNECTION_KEY, json.dumps({key: value for key, value in rows.items()
        if value.get('provider') != provider}))


def cached_connection(store, provider, model):
    try:
        row = json.loads(store.get_setting(CONNECTION_KEY, '{}')).get(provider + ':' + model)
        if not row: return None
        if row.get('state') not in {'ready', 'not_installed', 'sign_in_required', 'access_blocked', 'test_failed', 'cancelled'}:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row['checked_at'])
        binary = Path(row['binary'])
        if not 0 <= age.total_seconds() <= 900 or not binary.is_file() or binary.stat().st_mtime_ns != row['binary_mtime']:
            return None
        return ConnectionResult(**row)
    except (ValueError, TypeError, KeyError, OSError):
        return None


def connection_model(w, provider):
    project = w.registry.selected()
    model = w.store.get_project_preferences(project.id).model_overrides.get(provider, '') if project else ''
    if not model and provider == 'codex':
        from ..orchestration.catalog import CapabilityCatalogService
        advertised = CapabilityCatalogService.planner_default()
        model = advertised['model'] if advertised else ''
    return model or create_provider(provider).model


def login_arguments(provider, binary, model='', *, terminal=False, device=False):
    if provider == 'codex': return [str(binary), 'login', *(['--device-auth'] if device else [])]
    if provider == 'claude': return [str(binary), 'auth', 'login']
    if provider == 'copilot': return [str(binary), 'login', '--device-code' if device else '--web-flow']
    if provider == 'opencode':
        args = [str(binary), '--pure', 'auth', 'login']
        if '/' in model: args += ['--provider', model.split('/', 1)[0]]
        return args
    if provider == 'kilo': return [str(binary), '--pure', 'auth', 'login']
    raise ValueError('Unsupported provider')


def powershell_command(arguments):
    """Only shell-quote fixed CLI arguments; never put credentials in the script."""
    call = '& ' + ' '.join("'" + part.replace("'", "''") + "'" for part in arguments)
    script = "$ErrorActionPreference='Stop'\ntry {\n" + call + "\nexit $LASTEXITCODE\n} catch { Write-Host $_; exit 1 }"
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    executable = shutil.which('powershell.exe') or shutil.which('pwsh')
    if not executable: raise OSError('PowerShell was not found. Run the displayed login command in your terminal.')
    return [executable, '-NoLogo', '-NoProfile', '-EncodedCommand', encoded]


class ProviderConnection:
    def __init__(self, provider, model='', *, progress=lambda text: None, factory=create_provider, runner=subprocess.run,
                 popen=subprocess.Popen, login_timeout=900):
        self.provider, self.model, self.progress = provider, model, progress
        self.runtime = factory(provider, model=model)
        self.model = self.runtime.model
        if provider == 'codex': self.runtime.effort = 'medium'
        self.runner, self.popen, self.login_timeout = runner, popen, login_timeout
        self.stopped = threading.Event()
        self.process = None
        self.on_login = lambda: None

    def cancel(self):
        self.stopped.set()
        self.runtime.cancel()
        process = self.process
        if process is not None and process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=10, check=False)
            else:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass

    def guard(self):
        if self.stopped.is_set(): raise InterruptedError('Connection setup cancelled.')

    def login_progress(self, line):
        line = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', line)
        line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
        self.progress(line)

    def auth_state(self, binary):
        if self.provider not in {'codex', 'claude'}: return 'unknown'
        command = [str(binary), 'login', 'status'] if self.provider == 'codex' else [str(binary), 'auth', 'status']
        try:
            response = self.runner(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=15, check=False, env=self.runtime.invocation_environment(),
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if self.provider == 'claude':
                logged_in = json.loads(response.stdout).get('loggedIn')
                return 'signed_in' if logged_in is True else 'sign_in_required' if logged_in is False else 'unknown'
            text = (response.stdout + response.stderr).lower()
            if response.returncode == 0 and 'logged in' in text: return 'signed_in'
            if 'not logged in' in text: return 'sign_in_required'
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError, TypeError):
            pass
        # Config errors, unsupported status commands and outages do not imply
        # signed out. The isolated invocation can still succeed.
        return 'unknown'

    def check(self):
        binary = self.runtime.discover_binary()
        if not binary:
            return result(self.provider, self.model, 'not_installed', 'Install this provider before signing in.')
        self.guard()
        self.progress('Checking sign-in…')
        if self.auth_state(binary) == 'sign_in_required':
            return result(self.provider, self.model, 'sign_in_required', 'The provider reports that sign-in is required.', binary)
        self.guard()
        self.progress('Testing the selected model with a small request…')
        marker = 'MOLICODER_CONNECTION_' + uuid4().hex
        # No project is opened or shared by a setup test.
        with tempfile.TemporaryDirectory(prefix='molicoder-connection-') as directory:
            try:
                response = self.runtime.analyze(Path(directory),
                    'Connection test only. Do not use any tools or read files. Return ONLY this JSON object: ' +
                    json.dumps({'connection_test': marker}), timeout_seconds=90)
            except InterruptedError:
                raise
            except Exception as exc:
                self.guard()
                return self.failure(str(exc), binary)
        self.guard()
        from ..orchestration.contracts import decode
        schema = {'type': 'object', 'properties': {'connection_test': {'type': 'string'}},
                  'required': ['connection_test'], 'additionalProperties': False}
        try: matches = decode(response.message, schema) == {'connection_test': marker}
        except (ValueError, TypeError): matches = False
        if response.status == 'completed' and matches:
            return result(self.provider, self.model, 'ready', 'Connection verified: the selected model answered the test.', binary)
        if response.status == 'completed':
            return result(self.provider, self.model, 'test_failed', 'The model replied, but did not pass the structured connection test. Retry or choose another model.', binary)
        detail = response.error or response.message or 'The provider did not complete the connection test.'
        return self.failure(detail, binary)

    def failure(self, detail, binary):
        if AUTH_REQUIRED.search(detail): state = 'sign_in_required'
        elif re.search(r'quota|credit|balance|rate.?limit|\b429\b|\b403\b|subscription|policy|entitlement', detail, re.I): state = 'access_blocked'
        else: state = 'test_failed'
        return result(self.provider, self.model, state, detail, binary)

    def login(self, *, terminal=False, device=False):
        binary = self.runtime.discover_binary()
        if not binary: raise OSError('Provider is not installed.')
        self.guard()
        terminal = terminal or self.provider in {'claude', 'opencode', 'kilo'}
        arguments = login_arguments(self.provider, binary, self.model, terminal=terminal, device=device)
        environment = self.runtime.invocation_environment()
        environment.pop('CI', None)
        if terminal:
            environment.pop('NO_COLOR', None)
            self.progress('Official sign-in command:\n& ' + ' '.join("'" + part.replace("'", "''") + "'" for part in arguments))
            arguments = powershell_command(arguments)
        self.on_login()
        self.progress('Complete sign-in in the PowerShell window. Connection will be checked afterward.' if terminal else
            'Complete sign-in in your browser. Login details will appear here.')
        with tempfile.TemporaryDirectory(prefix='molicoder-login-') as directory:
            self.guard()
            options = dict(cwd=directory, env=environment)
            if os.name == 'nt':
                options['creationflags'] = (subprocess.CREATE_NEW_CONSOLE if terminal else subprocess.CREATE_NO_WINDOW) | subprocess.CREATE_NEW_PROCESS_GROUP
            else: options['start_new_session'] = True
            if not terminal:
                options.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    text=True, encoding='utf-8', errors='replace', bufsize=1)
            process = self.popen(arguments, **options)
            self.process = process
            lines = queue.Queue()
            def read_output():
                for line in process.stdout:
                    # OAuth URLs and device codes are ephemeral UI output, never
                    # persisted as connection evidence or application logs.
                    lines.put(line[:3000])
            if not terminal:
                reader = threading.Thread(target=read_output, daemon=True); reader.start()
            started = time.monotonic()
            try:
                while process.poll() is None:
                    self.guard()
                    if time.monotonic() - started > self.login_timeout:
                        self.cancel(); raise TimeoutError('Sign-in timed out. Retry setup when ready.')
                    if terminal: self.stopped.wait(.1)
                    else:
                        try: self.login_progress(lines.get(timeout=.1))
                        except queue.Empty: pass
                self.guard()
                if not terminal:
                    reader.join(timeout=1)
                    while not lines.empty(): self.login_progress(lines.get_nowait())
                if process.returncode != 0:
                    raise OSError('The official login command did not finish successfully. Retry or use PowerShell sign-in.')
            finally:
                if process.poll() is None: self.cancel()
                self.process = None
        self.guard()
        return self.check()

    def connect(self, *, terminal=False):
        checked = self.check()
        if checked.state == 'sign_in_required':
            return self.login(terminal=terminal)
        return checked


def main(argv=None):
    import argparse
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'): stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Verify a provider or run its official sign-in and verify afterward')
    parser.add_argument('provider', choices=('codex', 'claude', 'copilot', 'opencode', 'kilo'))
    parser.add_argument('--model', default='')
    parser.add_argument('--sign-in', action='store_true', help='Start official login if needed, then verify model access')
    parser.add_argument('--powershell', action='store_true', help='Use an interactive PowerShell login window')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    model = args.model
    if not model and args.provider == 'codex':
        from ..orchestration.catalog import CapabilityCatalogService
        advertised = CapabilityCatalogService.planner_default()
        model = advertised['model'] if advertised else ''
    def output(text):
        if sys.stdout is not None: print(text, flush=True)
    service = ProviderConnection(args.provider, model, progress=output)
    try:
        checked = service.connect(terminal=args.powershell) if args.sign_in else service.check()
    except KeyboardInterrupt:
        service.cancel(); return 130
    except Exception as exc:
        checked = result(service.provider, service.model, 'test_failed', str(exc), service.runtime.discover_binary())
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(asdict(checked), indent=2), encoding='utf-8')
    output(json.dumps(asdict(checked)))
    return 0 if checked.state == 'ready' else 2


if __name__ == '__main__':
    raise SystemExit(main())
