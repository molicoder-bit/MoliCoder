import json
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QDialogButtonBox, QMessageBox

from local_model_chat.storage import ChatStore
from local_model_chat.molicoder.projectstate import bootstrap_project
from local_model_chat.molicoder.providers import ProviderStatus
from local_model_chat.molicoder.build_selection import select_planner, ProviderSetupRequired
from local_model_chat.orchestration.catalog import CapabilityCatalogService
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.plan_review import PlanReviewDialog


def project_fixture(tmp_path):
    state = bootstrap_project(tmp_path, 'any-language', 'unknown')
    store = ChatStore(tmp_path/'profile.db')
    project = store.upsert_project('Any language', str(state.root), state.kind)
    return store, project


def test_empty_project_has_no_language_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr('local_model_chat.molicoder.projectstate._setup_python_environment', lambda *_: pytest.fail('Unexpected Python setup'))
    store, project = project_fixture(tmp_path)
    from pathlib import Path
    root = Path(project.path)
    assert (root/'.git').is_dir()
    assert (root/'README.md').exists()
    assert not (root/'pyproject.toml').exists()
    assert not (root/'package.json').exists()
    assert json.loads((root/'.molicoder/project.json').read_text())['validation'] == []


def test_typing_never_spawns_git_or_reloads_route_history(qtbot, tmp_path, monkeypatch):
    store, project = project_fixture(tmp_path)
    window = MainWindow(store, start_ollama_check=False); qtbot.addWidget(window)
    window._load_project_list(select_id=project.id); window._set_view('build')
    monkeypatch.setattr('local_model_chat.ui.main_window.read_git_state', lambda *_: pytest.fail('Git on a keystroke'))
    monkeypatch.setattr(window, '_refresh_route_identity', lambda: pytest.fail('History/routing scan on a keystroke'))
    QTest.keyClicks(window.build_request, 'Create a useful calculator with tests and a clear user interface')
    assert window.build_run_button.isEnabled()


def configure_catalog(store, project, monkeypatch):
    store.set_project_preferences(project.id, provider='auto', strategy='HYBRID', trusted_providers=('codex','claude'))
    models = [dict(model='cheap-small', description='Affordable for easier tasks', effort_levels=['low','medium']),
        dict(model='balanced-frontier', description='Frontier workhorse for demanding tasks', effort_levels=['low','medium','high'])]
    monkeypatch.setattr(CapabilityCatalogService, 'codex_models', staticmethod(lambda: models))
    store.set_setting(CapabilityCatalogService.KEY, json.dumps({'cli_models': {'codex': models}, 'runtimes': [
        {'tool_id':'codex','installed':True}, {'tool_id':'claude','installed':True}]}))


def test_auto_evaluates_reasoning_and_skips_signed_out_provider(tmp_path, monkeypatch):
    store, project = project_fixture(tmp_path); configure_catalog(store, project, monkeypatch)
    seen = []
    def provider(name, **kwargs):
        seen.append((name, kwargs['model']))
        return SimpleNamespace(name=name, model=kwargs['model'], probe=lambda: ProviderStatus(name, True, name=='claude', auth_verified=True))
    ready, failures = select_planner(store, project, None, provider)
    assert ready[0][0].provider == 'claude'
    assert ready[0][0].model == 'sonnet'
    assert ('codex','cheap-small') not in seen
    assert failures[0][0] == 'codex'
    assert 'Set up / sign in' in failures[0][1]


def test_auto_uses_advertised_effort_and_preserves_project_allowlist(tmp_path, monkeypatch):
    store, project = project_fixture(tmp_path); configure_catalog(store, project, monkeypatch)
    ready, _ = select_planner(store, project, None, lambda name, **kw: SimpleNamespace(
        name=name, model=kw['model'], probe=lambda: ProviderStatus(name, True, True, auth_verified=True)))
    route, provider = next((r, p) for r, p in ready if r.provider == 'codex')
    assert route.model == 'balanced-frontier'
    assert provider.effort == 'high'
    store.set_project_preferences(project.id, provider='auto', strategy='HYBRID', trusted_providers=('claude',))
    ready, _ = select_planner(store, project, None, lambda name, **kw: SimpleNamespace(
        name=name, model=kw['model'], probe=lambda: ProviderStatus(name, True, True, auth_verified=True)))
    assert {r.provider for r, _ in ready} == {'claude'}


def test_auto_no_ready_provider_returns_actionable_setup(tmp_path, monkeypatch):
    store, project = project_fixture(tmp_path); configure_catalog(store, project, monkeypatch)
    with pytest.raises(ProviderSetupRequired) as caught:
        select_planner(store, project, None, lambda name, **kw: SimpleNamespace(
            probe=lambda: ProviderStatus(name, True, False, auth_verified=True, detail='{"loggedIn":false}')))
    assert set(caught.value.providers) == {'codex', 'claude'}
    assert 'loggedIn' not in str(caught.value)
    assert 'not signed in' in str(caught.value)


def test_auto_unknown_auth_can_be_verified_by_bounded_invocation(tmp_path, monkeypatch):
    store, project = project_fixture(tmp_path); configure_catalog(store, project, monkeypatch)
    ready, _ = select_planner(store, project, None, lambda name, **kw: SimpleNamespace(name=name, model=kw['model'],
        probe=lambda: ProviderStatus(name, True, name=='codex', auth_verified=name=='claude')))
    assert ready[0][0].provider == 'codex'


def test_auto_runtime_keeps_model_efforts_and_separate_fallback_scopes(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from local_model_chat.molicoder.runtime import build_orchestrator
    from local_model_chat.orchestration.catalog import Route
    store, project = project_fixture(tmp_path)
    store.set_project_preferences(project.id, provider='auto', strategy='HYBRID', trusted_providers=('codex',))
    monkeypatch.setattr(CapabilityCatalogService, 'planning_routes', lambda *a, **k: ([
        Route('codex','frontier-a','codex',effort='high',reason='reasoning evidence'),
        Route('codex','frontier-b','codex',effort='high',reason='reasoning evidence')], []))
    monkeypatch.setattr('local_model_chat.molicoder.runtime.create_provider', lambda name, **kw: SimpleNamespace(
        name=name, model=kw['model'], probe=lambda: ProviderStatus(name, True, True, auth_verified=True)))
    scope='exact-auto-test'
    store.record_action_authorization(project_id=project.id, action='build_run', scope=scope, risk='external',decision='approved',
        actor='test',expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    runtime = build_orchestrator(store=store,project=project,client=SimpleNamespace(tags=lambda: []),provider_name='auto',authorization_scope=scope)
    assert runtime.provider.model == 'frontier-a' and runtime.provider.effort == 'high'
    assert len(runtime.planning_providers) == 1
    assert runtime.planning_providers[0].model == 'frontier-b'
    assert runtime.planning_providers[0].effort == 'high'
    authorization = store.latest_action_authorization(project_id=project.id, action='build_run',scope=scope+':planning-fallback:codex:frontier-b')
    assert authorization['decision'] == 'approved'


def test_auto_permission_accepts_one_frontier_and_saves_access(qtbot, tmp_path):
    from local_model_chat.ui.provider_setup import choose_project_access
    store, project = project_fixture(tmp_path)
    window = MainWindow(store, start_ollama_check=False); qtbot.addWidget(window)
    def interact():
        dialog = QApplication.activeModalWidget()
        checks = {c.objectName():c for c in dialog.findChildren(QCheckBox)}
        assert checks['allow_claude'].isEnabled()
        checks['allow_claude'].setChecked(True)
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons.button(QDialogButtonBox.Ok).isEnabled()
        buttons.button(QDialogButtonBox.Ok).click()
    QTimer.singleShot(10, interact)
    assert choose_project_access(window, project, needed=['codex','claude'], build=True, automatic_build=True)
    assert store.get_project_preferences(project.id).trusted_providers == ('claude',)


def test_long_plan_is_bounded_scrollable_and_drills_down(qtbot):
    tasks = [dict(key=f't{i}', title='Implement a meaningful feature with a long descriptive title ' * 3,
        objective='Detailed objective ' * 80, acceptance_criteria=['Expected result']*30) for i in range(30)]
    plan = {'id':9, 'payload': {'objective':'Long objective ' * 200, 'tasks':tasks,
        'task_assignments':{t['key']:{'model':'balanced-frontier','effort':'high'} for t in tasks}}}
    dialog = PlanReviewDialog(None, plan=plan, run={'provider':'codex','model':'balanced-frontier'}, detail='diagnostics', can_edit=True)
    qtbot.addWidget(dialog); dialog.show(); qtbot.wait(50)
    area = dialog.screen().availableGeometry()
    assert dialog.height() < area.height() and dialog.width() < area.width()
    assert dialog.tasks.verticalScrollBar().maximum() > 0
    dialog.show_task(dialog.tasks.item(0))
    assert dialog.pages.currentIndex() == 1
    assert 'Expected result' in dialog.details.toPlainText()
    dialog.back.click(); assert dialog.pages.currentIndex() == 0
    dialog.close(); assert dialog.decision == 'dismiss'


def test_optional_kandev_never_probes_when_off_and_blocks_when_unavailable(qtbot, tmp_path, monkeypatch):
    store, project = project_fixture(tmp_path)
    window = MainWindow(store, start_ollama_check=False); qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    assert not window.use_kandev.isChecked()
    assert not window.build_tabs.isTabVisible(1)
    monkeypatch.setattr('local_model_chat.ui.main_window.configured_kandev_adapter', lambda *a, **k: (_ for _ in ()).throw(ConnectionError('connection refused')))
    window.use_kandev.setChecked(True)
    assert window.build_tabs.isTabVisible(1)
    window.build_request.setPlainText('Build a calculator')
    monkeypatch.setattr('local_model_chat.ui.main_window.build_orchestrator', lambda **_: pytest.fail('Generated before board preflight'))
    QTimer.singleShot(20, lambda: QApplication.activeModalWidget().reject())
    window._run_build()
    assert store.list_project_runs(project.id) == []
    window.use_kandev.setChecked(False)
    assert store.get_setting(f'kandev.mirror.{project.id}') == 'false'
