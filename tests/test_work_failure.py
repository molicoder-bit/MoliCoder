import json

from local_model_chat.storage import ChatStore
from local_model_chat.work_state import WorkSnapshot


def test_failure_is_atomic_idempotent_and_preserves_verified_work(tmp_path):
    store = ChatStore(tmp_path/'state.db')
    project = store.upsert_project('Fixture', str(tmp_path), 'python')
    run = store.create_run(project.id, 'fixture request', 'HYBRID')
    complete = store.create_run_task(run, 'done', 'Done', 'Done', 'low')
    active = store.create_run_task(run, 'active', 'Active', 'Active', 'low')
    pending = store.create_run_task(run, 'pending', 'Pending', 'Pending', 'low')
    store.update_run_task(complete, 'completed', 1)
    store.update_run_task(active, 'running', 0)
    assert store.fail_active_run(run, 'handoff expected path escapes the worktree')
    assert not store.fail_active_run(run, 'late callback')
    assert [t.status for t in store.list_run_tasks(run)] == ['completed', 'failed', 'blocked']
    snapshot = WorkSnapshot.from_run(store, run)
    assert snapshot.outcome == 'needs_attention'
    assert not snapshot.running
    assert not snapshot.accepts('run:other')
    assert not snapshot.accepts(f'run:{run}')
    assert store.get_run(run)['ended_at']
    assert 'handoff' in snapshot.reason


def test_restart_preserves_external_work_for_reconciliation(tmp_path):
    store = ChatStore(tmp_path/'restart.db')
    project = store.upsert_project('Fixture', str(tmp_path), 'python')
    local = store.create_run(project.id, 'local', 'HYBRID', provider='codex')
    external = store.create_run(project.id, 'external', 'HYBRID', provider='kandev')
    assert store.recover_interrupted_runs() == (local,)
    assert store.get_run(local)['status'] == 'needs_attention'
    assert store.get_run(external)['status'] == 'running'


def test_request_failure_survives_refresh_and_stale_worker_cleanup(qtbot, tmp_path):
    from types import SimpleNamespace
    from local_model_chat.ui.main_window import MainWindow
    from local_model_chat.ui.workspace import refresh_workspace
    window = MainWindow(ChatStore(tmp_path/'ui.db'), start_ollama_check=False)
    qtbot.addWidget(window)
    window._build_failed('Provider unavailable')
    refresh_workspace(window)
    assert not window.work_feedback.isHidden()
    assert 'Provider unavailable' in window.work_feedback.text()
    old = SimpleNamespace(deleteLater=lambda: None)
    window._orchestration_worker_finished(old)
    assert 'Provider unavailable' in window.work_feedback.text()


def test_selected_run_snapshot_does_not_reuse_another_run_identity(tmp_path):
    store = ChatStore(tmp_path/'snapshots.db')
    project = store.upsert_project('Fixture', str(tmp_path), 'python')
    first = store.create_run(project.id, 'first', 'HYBRID', provider='codex', model='configured')
    second = store.create_run(project.id, 'second', 'HYBRID', provider='claude')
    store.add_event(first, 'provider_finished', {'provider': 'codex', 'model': 'reported', 'model_source': 'reported'})
    one, two = WorkSnapshot.from_run(store, first), WorkSnapshot.from_run(store, second)
    assert one.last_completed_call['model'] == 'reported'
    assert two.last_completed_call is None
    assert two.provider == 'claude'
    assert not two.accepts(one.operation_id)


def test_recovery_checks_local_process_birth_and_preserves_uncertainty(tmp_path, monkeypatch):
    store=ChatStore(tmp_path/'owners.db')
    project=store.upsert_project('Fixture',str(tmp_path),'python')
    run=store.create_run(project.id,'owned','HYBRID',provider='codex')
    monkeypatch.setattr('local_model_chat.process_owner.process_birth',lambda pid:'original')
    store.record_run_owner(run)
    assert store.recover_interrupted_runs() == ()
    monkeypatch.setattr('local_model_chat.process_owner.process_birth',lambda pid:None)
    assert store.recover_interrupted_runs() == ()
    monkeypatch.setattr('local_model_chat.process_owner.process_birth',lambda pid:'reused-pid-new-process')
    assert store.recover_interrupted_runs() == (run,)
