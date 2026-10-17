"""User journeys for provider setup; all Qt interaction runs headlessly."""
import json

import pytest
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QPushButton

from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.provider_setup import PROVIDERS, choose_project_access
from local_model_chat.ui.settings_window import show_settings_window


def window(qtbot, tmp_path):
    store = ChatStore(tmp_path / 'profile.db')
    root = tmp_path / 'project'; root.mkdir()
    project = store.upsert_project('Sample', str(root), 'generic')
    w = MainWindow(store, start_ollama_check=False); qtbot.addWidget(w)
    w._load_project_list(select_id=project.id)
    return w, project


def test_one_editor_autosaves_models_and_preserves_project_preferences(qtbot, tmp_path):
    w, project = window(qtbot, tmp_path)
    w.store.set_project_preferences(project.id, provider='claude', strategy='FRONTIER_ONLY',
        trusted_providers=('claude',), model_overrides={'claude': 'existing-model'})
    show_settings_window(w, 'Models & providers')
    panel = w._settings_providers
    assert not any('Save model' in b.text() for b in w._settings_dialog.findChildren(QPushButton))
    assert not any('Trust ' in b.text() for b in w._settings_dialog.findChildren(QCheckBox))
    panel.advanced.click(); panel.model.setText('model-one')
    panel.providers.setCurrentRow(PROVIDERS.index('opencode'))
    panel.advanced.click(); panel.model.setText('model-two')
    prefs = w.store.get_project_preferences(project.id)
    assert prefs.model_overrides == {'claude': 'existing-model', 'codex': 'model-one', 'opencode': 'model-two'}
    assert prefs.provider == 'claude' and prefs.strategy == 'FRONTIER_ONLY'
    assert prefs.trusted_providers == ('claude',)
    panel.model.clear()
    assert 'opencode' not in w.store.get_project_preferences(project.id).model_overrides
    w._settings_dialog.close(); show_settings_window(w, 'Models & providers')
    panel.providers.setCurrentRow(PROVIDERS.index('codex'))
    assert panel.model.text() == 'model-one'


def test_editor_switches_project_scope_without_overwriting_previous_project(qtbot, tmp_path):
    w, first = window(qtbot, tmp_path)
    second = w.store.upsert_project('Second', str(tmp_path), 'generic')
    show_settings_window(w, 'Models & providers'); panel = w._settings_providers
    panel.model.setText('first-model')
    w._load_project_list(select_id=second.id)
    assert panel.project_id == second.id and panel.model.text() == ''
    panel.model.setText('second-model')
    assert w.store.get_project_preferences(first.id).model_overrides['codex'] == 'first-model'
    assert w.store.get_project_preferences(second.id).model_overrides['codex'] == 'second-model'
    w._load_project_list()
    assert not panel.model.isEnabled() and not panel.access.isEnabled()


def test_first_open_offers_one_choice_and_remembers_defer(qtbot, tmp_path):
    w, project = window(qtbot, tmp_path)
    w.show()
    qtbot.waitUntil(lambda: getattr(w, '_project_setup_dialog', None) is not None)
    dialog = w._project_setup_dialog
    assert len([c for c in dialog.findChildren(QCheckBox) if c.objectName().startswith('allow_')]) == 5
    dialog.reject()
    w._load_project_list(); w._load_project_list(select_id=project.id)
    qtbot.wait(10)
    assert w._project_setup_dialog is None
    assert w.store.get_project_preferences(project.id).trusted_providers == ()
    reopened = ChatStore(w.store.path)
    assert reopened.get_setting(f'ui.provider_setup.{project.id}') == 'seen'


def test_setup_choice_cannot_save_to_a_project_that_was_switched(qtbot, tmp_path):
    w, project = window(qtbot, tmp_path)
    dialog = choose_project_access(w, project, first_open=True, valid=lambda: w.current_project_id == project.id)
    dialog.findChild(QCheckBox, 'allow_opencode').setChecked(True)
    w._load_project_list()
    dialog.accept()
    assert w.store.get_project_preferences(project.id).trusted_providers == ()


def test_permission_dialog_requires_selection_and_can_revoke_access(qtbot, tmp_path, monkeypatch):
    w, project = window(qtbot, tmp_path)
    def select(dialog):
        ok = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
        assert not ok.isEnabled()
        dialog.findChild(QCheckBox, 'allow_opencode').setChecked(True)
        assert ok.isEnabled()
        ok.click(); return dialog.result()
    monkeypatch.setattr(QDialog, 'exec', select)
    assert choose_project_access(w, project, needed=['opencode'])
    assert w.store.get_project_preferences(project.id).trusted_providers == ('opencode',)
    def revoke(dialog):
        dialog.findChild(QCheckBox, 'allow_opencode').setChecked(False)
        dialog.accept(); return dialog.result()
    monkeypatch.setattr(QDialog, 'exec', revoke)
    assert choose_project_access(w, project)
    assert w.store.get_project_preferences(project.id).trusted_providers == ()


def test_cancel_and_stale_worker_do_not_grant_access(qtbot, tmp_path, monkeypatch):
    w, project = window(qtbot, tmp_path)
    monkeypatch.setattr(QDialog, 'exec', lambda _: QDialog.DialogCode.Rejected)
    assert not choose_project_access(w, project, needed=['opencode'])
    def approve(dialog):
        dialog.findChild(QCheckBox, 'allow_opencode').setChecked(True)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, 'exec', approve)
    assert not choose_project_access(w, project, needed=['opencode'], valid=lambda: False)
    assert w.store.get_project_preferences(project.id).trusted_providers == ()


@pytest.mark.parametrize('approved', [True, False])
def test_request_worker_waits_for_permission_and_continues_or_cancels(qtbot, tmp_path, monkeypatch, approved):
    from local_model_chat.workers import RequestWorker
    from local_model_chat.orchestration.coordinator import RequestCoordinator
    from test_request_orchestration import Client, evaluation, turn
    w, project = window(qtbot, tmp_path)
    w.store.set_setting('orchestration.catalog.v1', json.dumps({'runtimes': [{'tool_id': 'opencode', 'installed': True}]}))
    client = Client([RuntimeError('Local unavailable')])
    original = RequestCoordinator.invoke
    invoked = []
    def invoke(self, route, messages, schema, purpose):
        if route.provider == 'ollama': return original(self, route, messages, schema, purpose)
        invoked.append((route.provider, messages))
        return evaluation('conversation', []) if 'intent' in schema.get('properties', {}) else turn('answer_ready', message='Hello. What are we working on?')
    monkeypatch.setattr(RequestCoordinator, 'invoke', invoke)
    def select(dialog):
        if approved:
            dialog.findChild(QCheckBox, 'allow_opencode').setChecked(True)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        else: dialog.reject()
        return dialog.result()
    monkeypatch.setattr(QDialog, 'exec', select)
    worker = RequestWorker(client, 'molicoder:latest', [
        {'role': 'user', 'content': 'My name is Pat'}, {'role': 'assistant', 'content': 'Hello Pat'},
        {'role': 'user', 'content': 'Hi again'}], {}, w.inference_policy, project)
    w.chat_worker = worker
    worker.review_required.connect(lambda proposal: w._review_request_operation(worker, proposal))
    results = []; worker.route_changed.connect(lambda e: results.append(e) if e['phase'] == 'completed' else None)
    worker.start()
    qtbot.waitUntil(lambda: len(results) == 1, timeout=10000)
    qtbot.waitUntil(lambda: not worker.isRunning())
    w.chat_worker = None
    assert results[0]['outcome'] == ('completed' if approved else 'cancelled')
    assert len(invoked) == (2 if approved else 0)
    if approved:
        assert 'My name is Pat' in json.dumps(invoked[0][1])
        assert w.store.get_project_preferences(project.id).trusted_providers == ('opencode',)


def test_direct_install_reuses_exact_verified_plan_without_a_tool_picker(qtbot, tmp_path, monkeypatch):
    from local_model_chat.environment.discovery import EnvironmentReport, DiscoveredTool
    from local_model_chat.environment.doctor import doctor_report
    from local_model_chat.environment.manifests import load_manifests
    from local_model_chat.workers import DoctorWorker, ManagedInstallWorker
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    environment = EnvironmentReport(1, 'Windows', 'x86_64', 4, 8 * 1024**3, 10 * 1024**3,
        'unknown', 'fixture', (DiscoveredTool('opencode', None, None, 'missing'),), ())
    monkeypatch.setattr(DoctorWorker, 'run', lambda worker: worker.ready.emit(doctor_report(environment, load_manifests())))
    monkeypatch.setattr(QInputDialog, 'getItem', lambda *_: pytest.fail('Already selected OpenCode'))
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, 'information', lambda *_: None)
    captured = []
    monkeypatch.setattr(ManagedInstallWorker, 'start', lambda worker: captured.append(worker))
    w, _ = window(qtbot, tmp_path)
    w._show_environment_doctor(install_tool='opencode')
    qtbot.waitUntil(lambda: bool(captured))
    assert captured[0].manifest.tool_id == 'opencode' and captured[0].plan.authorized
    qtbot.waitUntil(lambda: w.doctor_worker is None)
    from local_model_chat.environment.install import InstallReceipt
    captured[0].completed.emit(InstallReceipt('opencode', '1.19.0', captured[0].manifest.sha256,
        str(tmp_path), str(tmp_path / 'opencode.exe'), 'fixture'))
    from local_model_chat.orchestration.catalog import CapabilityCatalogService
    assert CapabilityCatalogService(w.store, w.client).untrusted_providers(w.registry.selected()) == ['opencode']
    assert w._provider_statuses['opencode'].installed
    qtbot.waitUntil(lambda: w.doctor_worker is None)
    w.doctor_dialog.close()


def test_build_permission_requires_all_selected_providers_without_enabling_chat_fallback(qtbot, tmp_path, monkeypatch):
    w, project = window(qtbot, tmp_path)
    w.store.set_setting(f'repository.external_fallback.{project.id}', 'false')
    def select(dialog):
        ok = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
        dialog.findChild(QCheckBox, 'allow_codex').setChecked(True)
        assert not ok.isEnabled()
        dialog.findChild(QCheckBox, 'allow_opencode').setChecked(True)
        assert ok.isEnabled()
        ok.click(); return dialog.result()
    monkeypatch.setattr(QDialog, 'exec', select)
    assert choose_project_access(w, project, needed=['codex', 'opencode'], build=True)
    assert set(w.store.get_project_preferences(project.id).trusted_providers) == {'codex', 'opencode'}
    assert w.store.get_setting(f'repository.external_fallback.{project.id}') == 'false'


def test_fallback_access_preserves_completed_tools_and_does_not_repeat_a_write(tmp_path, monkeypatch):
    from local_model_chat.orchestration.coordinator import RequestCoordinator
    from test_request_orchestration import Client, evaluation, turn, write_call, request_payload
    root = tmp_path / 'repository'; root.mkdir()
    store = ChatStore(tmp_path / 'fallback.db')
    project = store.upsert_project('Sample', str(root), 'generic')
    store.set_setting('orchestration.catalog.v1', json.dumps({'runtimes': [{'tool_id': 'opencode', 'installed': True}]}))
    checks = evaluation()['acceptance_checks'] + [{'id': 'readback', 'kind': 'tool_evidence', 'target': '.', 'expected': 'read_file'}]
    client = Client([evaluation(checks=checks), turn('needs_tools', [write_call()]), RuntimeError('Provider unavailable after writing')])
    reviews, access = [], []
    original = RequestCoordinator.invoke
    def invoke(self, route, messages, schema, purpose):
        if route.provider == 'ollama': return original(self, route, messages, schema, purpose)
        payload = request_payload(messages)
        assert payload['completed_tools']
        return turn('needs_tools', [{'call_id': 'readback', 'name': 'read_file', 'arguments': {'path': 'Iworked.txt', 'start': 1, 'end': 5}}])
    monkeypatch.setattr(RequestCoordinator, 'invoke', invoke)
    def allow(proposal):
        access.append(proposal)
        store.set_project_preferences(project.id, provider='opencode', strategy='HYBRID', trusted_providers=('opencode',))
        return True
    coordinator = RequestCoordinator(store, project, client, review=lambda p: reviews.append(p) or True, provider_access=allow)
    result = coordinator.run('Create Iworked.txt with yeay and read it back')
    assert result.receipt['outcome'] == 'completed', result.content
    assert len(access) == 1 and len(reviews) == 1
    assert (root / 'Iworked.txt').read_bytes() == b'yeay'
    assert sum(e['tool'] == 'write_file' for e in result.receipt['evidence'].values()) == 1


@pytest.mark.parametrize('mode', ['local_only', 'automatic_disabled', 'scope_denied'])
def test_provider_prompt_respects_local_only_and_real_scope_denials(tmp_path, mode):
    from local_model_chat.orchestration.coordinator import RequestCoordinator
    from test_request_orchestration import Client, evaluation, turn, write_call
    root = tmp_path / 'repository'; root.mkdir()
    store = ChatStore(tmp_path / 'fallback.db')
    project = store.upsert_project('Sample', str(root), 'generic')
    store.set_setting('orchestration.catalog.v1', json.dumps({'runtimes': [{'tool_id': 'opencode', 'installed': True}]}))
    if mode == 'automatic_disabled': store.set_setting(f'repository.external_fallback.{project.id}', 'false')
    call = write_call(); call['arguments']['path'] = '../escape.txt'
    client = Client([evaluation(), turn('needs_tools', [call])] if mode == 'scope_denied' else [RuntimeError('Local unavailable')])
    result = RequestCoordinator(store, project, client, allow_external=mode != 'local_only',
        provider_access=lambda _: pytest.fail('Must not ask for a provider here')).run('Please do the task')
    assert result.receipt['outcome'] == ('blocked' if mode == 'scope_denied' else 'failed')
    assert not (tmp_path / 'escape.txt').exists()


@pytest.mark.parametrize('trusted,qualified,expected', [
    (True, False, ['opencode']), (False, False, []),
    (True, True, ['ollama', 'opencode'])])
def test_source_reasoning_needs_qualification_instead_of_self_approval(tmp_path, trusted, qualified, expected):
    from local_model_chat.orchestration.catalog import CapabilityCatalogService
    from test_request_orchestration import Client, evaluation
    store = ChatStore(tmp_path / 'routing.db')
    project = store.upsert_project('Sample', str(tmp_path), 'generic')
    catalog = CapabilityCatalogService(store, Client([]))
    catalog.refresh(discover_runtimes=False)
    facts = catalog.cached(); facts['runtimes'] = [{'tool_id': 'opencode', 'installed': True}]
    store.set_setting(catalog.KEY, json.dumps(facts))
    if trusted:
        store.set_project_preferences(project.id, provider='opencode', strategy='HYBRID', trusted_providers=('opencode',))
    if qualified:
        store.set_setting('orchestration.qualification.v1', json.dumps({'molicoder:latest': {'qualified_intents': ['investigation']}}))
    spec = evaluation('investigation', [{'id': 'source', 'kind': 'tool_evidence', 'target': '.', 'expected': 'read_file'}])
    spec['required_capabilities'] = ['source_reasoning', 'read_file']
    routes, excluded = catalog.routes(project, 'molicoder:latest', spec=spec)
    assert [r.provider for r in routes] == expected
    if not qualified: assert any('Insufficient reasoning evidence' in r['reason'] for r in excluded)
    # Directory tools and ordinary conversation can still use the local model.
    routes, _ = catalog.routes(project, 'molicoder:latest', spec=evaluation('investigation', [
        {'id': 'listing', 'kind': 'tool_evidence', 'target': '.', 'expected': 'list_directory'}]))
    assert routes[0].provider == 'ollama'


def test_unqualified_local_cannot_publish_a_self_approved_code_answer(tmp_path, monkeypatch):
    from local_model_chat.orchestration.coordinator import RequestCoordinator
    from test_request_orchestration import Client, evaluation, request_payload, turn
    store = ChatStore(tmp_path / 'request.db')
    root = tmp_path / 'project'; root.mkdir()
    code = 'active = not note.get("archived", False)'
    (root / 'notes.py').write_text(code, encoding='utf-8')
    project = store.upsert_project('Sample', str(root), 'generic')
    store.set_setting('orchestration.catalog.v1', json.dumps({'runtimes': [{'tool_id': 'opencode', 'installed': True}]}))
    spec = evaluation('investigation', [{'id': 'source', 'kind': 'tool_evidence', 'target': '.', 'expected': 'search_code'}])
    spec['required_capabilities'] = ['source_reasoning', 'search_code']
    client = Client([spec, RuntimeError('Unqualified local answer must not be generated')])
    original = RequestCoordinator.invoke
    def invoke(self, route, messages, schema, purpose):
        if route.provider == 'ollama': return original(self, route, messages, schema, purpose)
        payload = request_payload(messages)
        key = next(k for k,e in payload['completed_tools'].items() if e['tool'] == 'search_code')
        return turn('answer_ready', message='The missing archived flag defaults to False; the note remains active.', refs=[key],
            citations=[{'evidence_id': key, 'path': 'notes.py', 'line': 1, 'quote': code}])
    monkeypatch.setattr(RequestCoordinator, 'invoke', invoke)
    prompts = []
    def allow(proposal):
        prompts.append(proposal)
        store.set_project_preferences(project.id, provider='opencode', strategy='HYBRID', trusted_providers=('opencode',))
        return True
    result = RequestCoordinator(store, project, client, provider_access=allow).run('What if an entry has no archived flag?')
    assert result.receipt['outcome'] == 'completed', result.content
    assert result.receipt['provider'] == 'opencode' and len(client.calls) == 1 and len(prompts) == 1
