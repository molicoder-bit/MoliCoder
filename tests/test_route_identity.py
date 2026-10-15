from types import SimpleNamespace

import pytest

from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.route_identity import observe_build_route


@pytest.fixture
def window(qtbot, tmp_path):
    store = ChatStore(tmp_path / 'routes.db')
    root = tmp_path / 'project'
    root.mkdir()
    project = store.upsert_project('Project', str(root), 'python')
    store.set_project_preferences(project.id, provider='codex', strategy='FRONTIER_ONLY',
                                  model_overrides={'codex': 'gpt-4'})
    w = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(w)
    w._load_project_list(select_id=project.id)
    yield w
    # These tests simulate worker state without launching an agent.
    w.orchestration_worker = None
    w.custom_api_worker = None
    w.chat_worker = None


def test_route_follows_mode_and_selection_without_using_runtime_health(window):
    window._ollama_ready(True, [{'name': 'molicoder:latest'}, {'name': 'qwen:7b'}])
    window.model_combo.setCurrentIndex(window.model_combo.findData('qwen:7b'))
    assert window.route_identity.text() == 'Chat selected · Ollama (local) · qwen:7b'
    window._set_view('build')
    assert window.route_identity.text() == 'Build selected · Codex · gpt-4 (configured)'
    assert 'connected' in window.connection_label.text()
    assert window.connection_label.parent() == window.inspector
    window.build_strategy.setCurrentIndex(window.build_strategy.findData('LOCAL_OFFLINE'))
    assert window.route_identity.text() == 'Build selected · Ollama (local) · molicoder:latest'
    window.build_strategy.setCurrentIndex(window.build_strategy.findData('HYBRID'))
    assert 'gpt-4 (configured)' in window.route_identity.text()
    assert 'Hybrid: Ollama / molicoder:latest' in window.route_identity.text()
    window.build_provider.setCurrentIndex(window.build_provider.findData('claude'))
    assert 'Claude · CLI default · model not reported' in window.route_identity.text()
    assert 'gpt-4' not in window.route_identity.text()


def test_live_handoffs_clear_old_model_and_ignore_stale_run_on_completion(window):
    window._set_view('build')
    window.orchestration_worker = SimpleNamespace(isRunning=lambda: False)
    observe_build_route(window, 'run_started', {'strategy': 'HYBRID'})
    observe_build_route(window, 'provider_started', {'provider': 'codex'})
    assert 'Calling: Codex · gpt-4 (configured)' in window.route_identity.text()
    observe_build_route(window, 'provider_finished', {'provider': 'codex', 'model': 'gpt-5', 'model_source': 'reported'})
    assert 'Last call: Codex · gpt-5 (reported)' in window.route_identity.text()
    observe_build_route(window, 'local_attempt_started', {})
    assert 'Ollama (local) · molicoder:latest' in window.route_identity.text()
    observe_build_route(window, 'provider_attempt_started', {'provider': 'claude'})
    assert 'Calling: Claude · CLI default · model not reported' in window.route_identity.text()
    assert 'gpt-5' not in window.route_identity.text()
    window.orchestration_worker = None
    window._refresh_route_identity()
    assert window.route_identity.text() == 'Build selected · Codex · gpt-4 (configured)'


def test_local_offline_run_does_not_display_unused_external_provider(window):
    window._set_view('build')
    window.orchestration_worker = SimpleNamespace(isRunning=lambda: False)
    observe_build_route(window, 'run_started', {'strategy': 'LOCAL_OFFLINE', 'provider': 'codex', 'model': 'gpt-4'})
    assert 'gpt-4' not in window.route_identity.text()
    # The orchestrator keeps a provider object even when its local planner is selected.
    observe_build_route(window, 'provider_started', {'provider': 'codex'})
    assert 'Ollama (local) · molicoder:latest' in window.route_identity.text()
    assert 'gpt-4' not in window.route_identity.text()


def test_kandev_owner_and_board_do_not_claim_ollama_model(window):
    window._set_view('build')
    window.build_owner.setCurrentIndex(window.build_owner.findData('kandev'))
    assert 'Kandev · agent/model managed in Kandev · model not reported' in window.route_identity.text()
    window.build_owner.setCurrentIndex(window.build_owner.findData('internal'))
    window.build_tabs.setCurrentIndex(1)
    assert window.route_identity.text().startswith('Kandev board ·')
    assert 'gpt-4' not in window.route_identity.text()


def test_api_test_identity_overrides_local_chat_header(window, monkeypatch):
    window.custom_api_worker = SimpleNamespace(offering_id='test-route')
    monkeypatch.setattr(window.inference_registry, 'get_offering',
                        lambda _: SimpleNamespace(provider_id='example-api', model_id='gpt-4'))
    window._refresh_route_identity()
    assert window.route_identity.text() == 'API test running · example-api · gpt-4 (configured)'
    window.custom_api_worker = None
    window._refresh_route_identity()
    assert window.route_identity.text().startswith('Chat selected · Ollama (local)')
