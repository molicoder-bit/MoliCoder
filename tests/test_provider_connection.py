import base64
from dataclasses import replace
import io
import json
import subprocess
import threading
import time
import sys

import pytest

from local_model_chat.environment.provider_connection import (
    ProviderConnection, cached_connection, login_arguments, powershell_command, result, save_connection,
)
from local_model_chat.molicoder.providers import ProviderResult, _safe_environment
from local_model_chat.storage import ChatStore


class Runtime:
    def __init__(self, binary, model='fixture/model', signed_in=True):
        self.binary, self.model, self.signed_in = binary, model, signed_in
        self.calls = []; self.cancelled = False; self.reply = None

    def discover_binary(self): return self.binary
    def invocation_environment(self): return _safe_environment()
    def cancel(self): self.cancelled = True

    def analyze(self, root, prompt, **kwargs):
        assert list(root.iterdir()) == []
        self.calls.append((root, prompt))
        if self.reply: return self.reply
        if not self.signed_in: return ProviderResult('failed', '', error='401 Unauthorized: sign-in required')
        payload = json.loads(prompt.split('Return ONLY this JSON object: ', 1)[1])
        return ProviderResult('completed', json.dumps(payload))


def service(tmp_path, provider='codex', *, signed_in=True, popen=None):
    binary = tmp_path / (provider + '.exe'); binary.touch()
    runtime = Runtime(binary, signed_in=signed_in)
    def runner(command, **kwargs):
        payload = json.dumps({'loggedIn': runtime.signed_in}) if provider == 'claude' else (
            'Logged in using ChatGPT' if runtime.signed_in else 'Not logged in')
        return subprocess.CompletedProcess(command, 0 if runtime.signed_in else 1, payload, '')
    return ProviderConnection(provider, runtime.model, factory=lambda *a, **k: runtime, runner=runner,
        **({'popen': popen} if popen else {})), runtime


@pytest.mark.parametrize('provider', ['codex', 'claude', 'copilot', 'opencode', 'kilo'])
def test_existing_login_must_pass_actual_isolated_model_test(tmp_path, provider):
    connection, runtime = service(tmp_path, provider)
    checked = connection.check()
    assert checked.state == 'ready' and checked.model == runtime.model
    assert len(runtime.calls) == 1 and not runtime.calls[0][0].exists()


@pytest.mark.parametrize('provider', ['codex', 'claude', 'copilot', 'opencode', 'kilo'])
def test_official_login_runs_then_model_access_is_verified(tmp_path, provider, monkeypatch):
    monkeypatch.setattr('local_model_chat.environment.provider_connection.powershell_command', lambda args: ['powershell', *args])
    launches = []
    def popen(arguments, **options):
        launches.append((arguments, options)); runtime.signed_in = True
        return type('Process', (), {'returncode': 0, 'poll': lambda _: 0, 'stdout': io.StringIO('Continue in your browser\n')})()
    connection, runtime = service(tmp_path, provider, signed_in=False, popen=popen)
    checked = connection.connect()
    assert checked.state == 'ready' and len(launches) == 1
    assert 'CI' not in launches[0][1]['env']
    assert len(runtime.calls) == (1 if provider in {'codex', 'claude'} else 2)
    command = launches[0][0]
    if provider in {'claude', 'opencode', 'kilo'}: assert command[0] == 'powershell'
    assert 'login' in command


@pytest.mark.parametrize('reply', [ProviderResult('completed', 'Ready'), ProviderResult('completed', '{"connection_test":"stale"}'),
    ProviderResult('completed', 'Please sign in')])
def test_exit_zero_or_model_claim_cannot_mark_connection_ready(tmp_path, reply):
    connection, runtime = service(tmp_path)
    runtime.reply = reply
    assert connection.check().state == 'test_failed'


def test_login_exit_zero_is_not_success_when_model_access_is_still_denied(tmp_path):
    def popen(*args, **kwargs):
        return type('Process', (), {'returncode': 0, 'poll': lambda _: 0, 'stdout': io.StringIO('Signed in\n')})()
    connection, runtime = service(tmp_path, signed_in=False, popen=popen)
    assert connection.connect().state == 'sign_in_required'
    assert not runtime.calls


@pytest.mark.parametrize('detail,state', [('401 Unauthorized', 'sign_in_required'), ('Quota exceeded', 'access_blocked'),
    ('429 rate limited', 'access_blocked'), ('Model not found', 'test_failed'), ('Network timeout', 'test_failed')])
def test_access_and_network_failures_are_not_all_treated_as_logout(tmp_path, detail, state):
    connection, runtime = service(tmp_path)
    runtime.reply = ProviderResult('failed', '', error=detail)
    assert connection.check().state == state


def test_claude_json_false_is_not_a_verified_login_even_with_exit_zero(tmp_path):
    connection, runtime = service(tmp_path, 'claude')
    connection.runner = lambda *a, **k: subprocess.CompletedProcess(a, 0, '{"loggedIn":false}', '')
    assert connection.check().state == 'sign_in_required' and not runtime.calls


def test_codex_config_error_does_not_start_unnecessary_login(tmp_path):
    connection, _ = service(tmp_path)
    connection.runner = lambda *a, **k: subprocess.CompletedProcess(a, 1, '', 'Invalid unrelated config option')
    assert connection.connect().state == 'ready'


def test_cache_is_model_and_executable_specific_and_never_stores_oauth_output(tmp_path):
    connection, runtime = service(tmp_path)
    store = ChatStore(tmp_path / 'profile.db')
    checked = connection.check(); save_connection(store, checked)
    assert cached_connection(store, 'codex', runtime.model).state == 'ready'
    assert cached_connection(store, 'codex', 'another-model') is None
    runtime.binary.write_text('upgraded binary')
    assert cached_connection(store, 'codex', runtime.model) is None


def test_powershell_uses_documented_arguments_and_literal_quoting(monkeypatch):
    monkeypatch.setattr('local_model_chat.environment.provider_connection.shutil.which', lambda _: 'powershell.exe')
    command = powershell_command(["C:/A user's folder/claude.exe", 'auth', 'login'])
    script = base64.b64decode(command[-1]).decode('utf-16-le')
    assert "& 'C:/A user''s folder/claude.exe' 'auth' 'login'" in script
    assert '-NoProfile' in command and 'Bypass' not in command
    assert login_arguments('codex', 'codex.exe')[1:] == ['login']
    assert login_arguments('copilot', 'copilot.exe')[1:] == ['login', '--web-flow']
    assert login_arguments('opencode', 'opencode.exe', 'openai/model')[1:] == ['--pure', 'auth', 'login', '--provider', 'openai']
    assert login_arguments('kilo', 'kilo.exe')[1:] == ['--pure', 'auth', 'login']
    assert login_arguments('codex', 'codex.exe', device=True)[1:] == ['login', '--device-auth']
    assert login_arguments('copilot', 'copilot.exe', device=True)[1:] == ['login', '--device-code']


def test_setup_button_confirms_access_without_granting_project_trust(qtbot, tmp_path, monkeypatch):
    from local_model_chat.ui.main_window import MainWindow
    from local_model_chat.ui.settings_window import show_settings_window
    connection, runtime = service(tmp_path)
    def factory(provider, model, progress):
        connection.progress = progress; return connection
    monkeypatch.setattr('local_model_chat.ui.provider_connection.ProviderConnection', factory)
    w = MainWindow(ChatStore(tmp_path / 'profile.db'), start_ollama_check=False); qtbot.addWidget(w)
    project = w.store.upsert_project('Sample', str(tmp_path), 'generic'); w._load_project_list(select_id=project.id)
    show_settings_window(w, 'Models & providers')
    # Match the provider/model the dialog actually captures.
    w.store.set_project_preferences(project.id, provider='codex', strategy='HYBRID', model_overrides={'codex': runtime.model})
    w._settings_providers.refresh(); w._settings_providers.guide.click()
    dialog = w._provider_connection_dialog
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.last_result.state == 'ready'
    from local_model_chat.ui.provider_setup import provider_status
    assert provider_status(w, 'codex')[0] == 'Ready'
    assert w.store.get_project_preferences(project.id).trusted_providers == ()
    dialog.reject()


def test_cancelled_login_cannot_save_late_success(qtbot, tmp_path, monkeypatch):
    from local_model_chat.ui.main_window import MainWindow
    from local_model_chat.ui.provider_connection import show_provider_connection
    connection, runtime = service(tmp_path)
    started = threading.Event()
    def connect():
        started.set()
        connection.stopped.wait(5)
        return result('codex', runtime.model, 'ready', 'Late response', runtime.binary)
    connection.connect = connect
    monkeypatch.setattr('local_model_chat.ui.provider_connection.ProviderConnection', lambda *a, **k: connection)
    w = MainWindow(ChatStore(tmp_path / 'profile.db'), start_ollama_check=False); qtbot.addWidget(w)
    dialog = show_provider_connection(w, 'codex')
    qtbot.waitUntil(started.is_set)
    dialog.reject()
    qtbot.waitUntil(lambda: not w._provider_connection_workers)
    assert cached_connection(w.store, 'codex', runtime.model) is None


@pytest.mark.parametrize('terminal', [False, True])
def test_real_login_process_is_waited_for_then_verified(tmp_path, monkeypatch, terminal):
    """Run a harmless CLI fixture; a real PowerShell wrapper stays hidden in tests."""
    if terminal and sys.platform != 'win32': pytest.skip('PowerShell integration on Windows')
    script = tmp_path / "developer's sign in.py"
    marker = tmp_path / 'signed-in'
    script.write_text('import pathlib,sys\nprint("Finish sign-in in your browser", flush=True)\npathlib.Path(sys.argv[1]).touch()\n', encoding='utf-8')
    monkeypatch.setattr('local_model_chat.environment.provider_connection.login_arguments', lambda *a, **k: [sys.executable, str(script), str(marker)])
    def popen(command, **options):
        if terminal:
            options.update(creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return subprocess.Popen(command, **options)
    connection, runtime = service(tmp_path, signed_in=False, popen=popen)
    def runner(*a, **k):
        runtime.signed_in = marker.exists()
        return subprocess.CompletedProcess(a, 0 if runtime.signed_in else 1,
            'Logged in using ChatGPT' if runtime.signed_in else 'Not logged in', '')
    connection.runner = runner
    assert connection.connect(terminal=terminal).state == 'ready'
    assert marker.exists() and len(runtime.calls) == 1


def test_real_login_process_cancel_stops_before_credentials_are_written(tmp_path, monkeypatch):
    script = tmp_path / 'wait_login.py'; marker = tmp_path / 'signed-in'
    script.write_text('import pathlib,sys,time\nprint("Waiting for sign-in",flush=True)\ntime.sleep(20)\npathlib.Path(sys.argv[1]).touch()\n', encoding='utf-8')
    monkeypatch.setattr('local_model_chat.environment.provider_connection.login_arguments', lambda *a, **k: [sys.executable, str(script), str(marker)])
    connection, _ = service(tmp_path, signed_in=False)
    started, stopped = threading.Event(), threading.Event()
    connection.progress = lambda text: started.set() if 'Waiting for sign-in' in text else None
    def login():
        try: connection.connect()
        except InterruptedError: stopped.set()
    thread = threading.Thread(target=login); thread.start()
    assert started.wait(5)
    connection.cancel(); thread.join(timeout=5)
    assert stopped.is_set() and not thread.is_alive() and not marker.exists()


def test_login_timeout_is_reported_in_the_dialog(qtbot, tmp_path, monkeypatch):
    from local_model_chat.ui.main_window import MainWindow
    from local_model_chat.ui.provider_connection import show_provider_connection
    connection, runtime = service(tmp_path)
    def connect():
        connection.stopped.set()
        raise TimeoutError('Sign-in timed out. Retry setup when ready.')
    connection.connect = connect
    monkeypatch.setattr('local_model_chat.ui.provider_connection.ProviderConnection', lambda *a, **k: connection)
    w = MainWindow(ChatStore(tmp_path / 'profile.db'), start_ollama_check=False); qtbot.addWidget(w)
    dialog = show_provider_connection(w, 'codex')
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.last_result.state == 'test_failed' and 'timed out' in dialog.last_result.detail
    dialog.reject()


def test_refresh_keeps_live_proof_when_auth_is_unknown_but_clears_known_logout(qtbot, tmp_path, monkeypatch):
    from local_model_chat.ui.main_window import MainWindow
    from local_model_chat.ui.settings_window import show_settings_window
    from local_model_chat.molicoder.providers import ProviderStatus
    connection, runtime = service(tmp_path, 'opencode')
    w = MainWindow(ChatStore(tmp_path / 'profile.db'), start_ollama_check=False); qtbot.addWidget(w)
    project = w.store.upsert_project('Sample', str(tmp_path), 'generic'); w._load_project_list(select_id=project.id)
    w.store.set_project_preferences(project.id, provider='opencode', strategy='HYBRID', model_overrides={
        'opencode': runtime.model, 'claude': runtime.model})
    checked = connection.check()
    save_connection(w.store, checked); save_connection(w.store, replace(checked, provider='claude'))
    monkeypatch.setattr('local_model_chat.molicoder.providers.probe_all_providers', lambda: (
        ProviderStatus('opencode', True, False, model=runtime.model, auth_verified=False),
        ProviderStatus('claude', True, False, model=runtime.model, auth_verified=True)))
    show_settings_window(w, 'Models & providers'); w._settings_providers.check.click()
    qtbot.waitUntil(lambda: getattr(w, '_provider_check_worker', None) is None)
    assert cached_connection(w.store, 'opencode', runtime.model).state == 'ready'
    assert cached_connection(w.store, 'claude', runtime.model) is None


def test_browser_login_can_switch_to_device_code_without_waiting_for_timeout(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QPushButton
    from local_model_chat.ui.main_window import MainWindow
    from local_model_chat.ui.provider_connection import show_provider_connection
    instances = []
    def factory(provider, model, progress):
        connection, runtime = service(tmp_path)
        instances.append(connection)
        def connect():
            connection.on_login()
            connection.stopped.wait(5)
            raise InterruptedError('Switched method')
        def login(**kwargs):
            assert kwargs == {'device': True}
            return connection.check()
        connection.connect = connect; connection.login = login
        return connection
    monkeypatch.setattr('local_model_chat.ui.provider_connection.ProviderConnection', factory)
    w = MainWindow(ChatStore(tmp_path / 'profile.db'), start_ollama_check=False); qtbot.addWidget(w)
    dialog = show_provider_connection(w, 'codex')
    button = next(b for b in dialog.findChildren(QPushButton) if b.text() == 'Use device code')
    qtbot.waitUntil(button.isEnabled)
    button.click()
    qtbot.waitUntil(lambda: getattr(dialog, 'last_result', None) is not None)
    qtbot.waitUntil(lambda: not w._provider_connection_workers)
    assert len(instances) == 2 and instances[0].stopped.is_set()
    assert dialog.last_result.state == 'ready'
    dialog.reject()
