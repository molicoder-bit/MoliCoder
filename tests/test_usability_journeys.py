from types import SimpleNamespace
import pytest

from PySide6.QtWidgets import QMessageBox

from local_model_chat.domain import Message
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.workspace import refresh_workspace, show_history


def test_failure_is_visible_after_reopen_cleanup_and_history(qtbot,tmp_path):
    store=ChatStore(tmp_path/'history.db')
    project=store.upsert_project('Fixture',str(tmp_path),'python')
    run=store.create_run(project.id,'Create a folder','HYBRID',provider='codex')
    task=store.create_run_task(run,'create_messi_folder','Create folder','Create folder','low')
    store.update_run_task(task,'running',0)
    store.fail_active_run(run,'handoff expected path escapes the worktree')
    store.add_event(run,'run_needs_attention',{'error':'handoff expected path escapes the worktree'})
    w=MainWindow(store,start_ollama_check=False);qtbot.addWidget(w);w.show()
    w._load_project_list(select_id=project.id);w._set_view('build')
    assert 'Target needs correction' in w.work_feedback.text()
    assert not w.work_feedback.isHidden()
    assert store.list_run_tasks(run)[0].status == 'failed'
    worker=SimpleNamespace(deleteLater=lambda:None)
    w.orchestration_worker=worker
    w._orchestration_worker_finished(worker)
    refresh_workspace(w);show_history(w)
    assert 'Target needs correction' in w.work_feedback.text()
    assert 'running' not in w.task_list.item(0).text()
    assert not w.busy_indicator.timer.isActive()
    w.history_dialog.close()


def test_general_history_clears_project_without_losing_its_draft(qtbot,tmp_path):
    store=ChatStore(tmp_path/'scope.db')
    a=tmp_path/'a';a.mkdir();b=tmp_path/'b';b.mkdir()
    first=store.upsert_project('First',str(a),'python')
    second=store.upsert_project('Second',str(b),'python')
    w=MainWindow(store,start_ollama_check=False);qtbot.addWidget(w)
    general=w.current_conversation_id;w.composer.setPlainText('general draft')
    w._load_project_list(select_id=first.id);w.new_chat_button.click()
    project_chat=w.current_conversation_id;w.composer.setPlainText('project draft')
    w._load_project_list(select_id=second.id)
    w._load_conversation_list(select_id=general)
    assert w.current_project_id is None and w.registry.selected() is None
    assert w.composer.toPlainText() == 'general draft'
    w._load_conversation_list(select_id=project_chat)
    assert w.current_project_id == first.id
    assert w.composer.toPlainText() == 'project draft'


def test_new_chat_does_not_interrupt_background_build(qtbot,tmp_path):
    w=MainWindow(ChatStore(tmp_path/'background.db'),start_ollama_check=False);qtbot.addWidget(w);w.show()
    worker=SimpleNamespace(paused=False,pause_state='running',isRunning=lambda:False)
    w.orchestration_worker=worker
    w._set_view('build');old=w.current_conversation_id
    w.new_chat_button.click()
    assert w.current_conversation_id != old and w.orchestration_worker is worker
    assert w.return_to_build.isVisible()
    w.return_to_build.click()
    assert w.view_stack.currentWidget() == w.build_view
    w.orchestration_worker=None


def test_folder_outcomes_are_verified_recorded_and_visible(qtbot,tmp_path,monkeypatch):
    w=MainWindow(ChatStore(tmp_path/'actions.db'),start_ollama_check=False);qtbot.addWidget(w)
    monkeypatch.setattr(QMessageBox,'question',lambda *_:QMessageBox.StandardButton.Yes)
    target=tmp_path/'new folder'
    w.build_request.setPlainText(f'Let\'s create a new folder called "new folder" in {tmp_path}')
    w._run_build();assert target.is_dir()
    assert w.store.list_local_actions()[0]['outcome'] == 'created'
    w._run_build();assert w.store.list_local_actions()[0]['outcome'] == 'already_exists'
    file=tmp_path/'conflict';file.write_text('keep')
    result=w._confirm_directory_creation(file)
    assert result.action_state == 'failed' and file.read_text() == 'keep'
    monkeypatch.setattr(QMessageBox,'question',lambda *_:QMessageBox.StandardButton.No)
    result=w._confirm_directory_creation(tmp_path/'cancelled')
    assert result.action_state == 'cancelled' and not (tmp_path/'cancelled').exists()
    w._set_view('build');refresh_workspace(w)
    assert 'No AI model used' in w.route_identity.text()


def test_folder_permission_failure_preserves_truth(qtbot,tmp_path,monkeypatch):
    from pathlib import Path
    w=MainWindow(ChatStore(tmp_path/'denied.db'),start_ollama_check=False);qtbot.addWidget(w)
    target=tmp_path/'denied'
    monkeypatch.setattr(QMessageBox,'question',lambda *_:QMessageBox.StandardButton.Yes)
    original=Path.mkdir
    def mkdir(path,*args,**kwargs):
        if path == target: raise PermissionError('Fixture access denied')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'mkdir',mkdir)
    result=w._confirm_directory_creation(target)
    assert result.action_state == 'failed' and not target.exists()
    assert w.store.list_local_actions()[0]['outcome'] == 'failed'


@pytest.mark.parametrize('status,reason,title', [('waiting_auth','Authentication expired','Account connection needed'),('waiting_quota','Provider quota exhausted','Usage limit reached')])
def test_waiting_provider_state_has_visible_recovery(qtbot,tmp_path,status,reason,title):
    store=ChatStore(tmp_path/'waiting.db')
    project=store.upsert_project('Fixture',str(tmp_path),'python')
    run=store.create_run(project.id,'Fixture','HYBRID',provider='codex')
    store.update_run_state(run,status,recovery_reason=reason)
    store.finish_run(run,status)
    w=MainWindow(store,start_ollama_check=False);qtbot.addWidget(w)
    w._load_project_list(select_id=project.id);w._set_view('build')
    assert title in w.work_feedback.text() and not w.work_feedback.isHidden()
    assert w.failure_recovery.text() in {'Models & providers','Usage & limits'}
