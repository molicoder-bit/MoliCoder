import json
from pathlib import Path

import pytest

from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult
from test_build_plan_approval import approve, fixture, git


def pending_review(tmp_path, *, two_tasks=True):
    root, store, project, _, runner = fixture(tmp_path)
    tasks = [dict(key='first',title='Write a greeting',objective='Write hello.txt',risk='low',depends_on=[],
        expected_paths=['hello.txt'],acceptance_criteria=['Greeting reads naturally'],verification_checks=['project-default'])]
    if two_tasks:
        tasks.append(dict(key='second',title='Add a farewell',objective='Write second.txt',risk='low',depends_on=['first'],
            expected_paths=['second.txt'],acceptance_criteria=['Configured validation must pass'],verification_checks=['project-default']))
    class Planner:
        provider='scripted'
        def plan(self, _): return parse_plan({'objective':'Greetings', 'tasks':tasks})
    class Executor:
        name='scripted'; model='fixture-model'
        def __init__(self): self.calls=[]
        def execute(self, path, prompt, **kwargs):
            first = '(first)' in prompt
            self.calls.append(('first' if first else 'second',prompt))
            (Path(path)/('hello.txt' if first else 'second.txt')).write_text('Hello again' if 'Use a warmer greeting' in prompt else 'Hello')
            return ProviderResult('completed','Written')
        def cancel(self): pass
    provider=Executor(); runner.provider=provider; runner.planner=Planner()
    runner.validate_task=lambda path, task: 'validation passed' if all((Path(path)/p).exists() for p in task.expected_paths) else 'validation failed'
    runner.validate=lambda path: 'validation passed' if (Path(path)/('second.txt' if two_tasks else 'hello.txt')).exists() else 'validation failed'
    prepared=runner.run('Greetings', mode='FRONTIER_ONLY'); approve(store,prepared.run_id)
    result=runner.execute_approved(prepared.run_id)
    assert result.status=='needs_attention' and store.list_run_tasks(result.run_id)[0].status=='review_required'
    return root,store,project,runner,provider,result


def test_accept_review_preserves_checkpoint_and_executes_only_remaining_task(tmp_path):
    root,store,_,runner,provider,result=pending_review(tmp_path)
    snapshot=runner.review_snapshot(result.run_id)
    assert snapshot['criteria']==['Greeting reads naturally'] and not snapshot['errors']
    checkpoint=snapshot['head']
    done=runner.continue_review(result.run_id,snapshot,'approve',checked=snapshot['criteria'])
    assert done.status=='ready_to_apply',done.error
    assert [key for key,_ in provider.calls]==['first','second']
    completion=store.get_task_completion(store.list_run_tasks(result.run_id)[0].id)
    assert completion.human_review_status=='approved' and not completion.unmet_criteria
    assert completion.revision==checkpoint
    assert (done.integration_path/'second.txt').exists() and not (root/'hello.txt').exists()
    decisions=[json.loads(e.payload_json) for e in store.list_events(result.run_id) if e.kind=='task_review_decision']
    assert decisions[0]['reviewed_criteria']==snapshot['criteria']
    with pytest.raises(ValueError): runner.continue_review(result.run_id,snapshot,'approve',checked=snapshot['criteria'])
    assert len(provider.calls)==2


def test_last_task_acceptance_runs_checks_without_another_model_call(tmp_path):
    _,store,_,runner,provider,result=pending_review(tmp_path,two_tasks=False)
    snapshot=runner.review_snapshot(result.run_id)
    done=runner.continue_review(result.run_id,snapshot,'approve',checked=snapshot['criteria'])
    assert done.status=='ready_to_apply',done.error
    assert len(provider.calls)==1


def test_request_changes_retries_with_feedback_and_requires_fresh_result_review(tmp_path):
    _,store,_,runner,provider,result=pending_review(tmp_path,two_tasks=False)
    snapshot=runner.review_snapshot(result.run_id)
    done=runner.continue_review(result.run_id,snapshot,'changes','Use a warmer greeting')
    assert done.status=='needs_attention' and len(provider.calls)==2
    assert 'Human review requested these changes' in provider.calls[-1][1]
    assert store.list_run_tasks(result.run_id)[0].attempts==2
    assert [json.loads(e.payload_json)['attempt'] for e in store.list_events(result.run_id) if e.kind=='provider_attempt_started']==[1,2]
    assert (done.integration_path/'hello.txt').read_text()=='Hello again'
    refreshed=runner.review_snapshot(result.run_id)
    assert refreshed['head'] != snapshot['head']
    done=runner.continue_review(result.run_id,refreshed,'approve',checked=refreshed['criteria'])
    assert done.status=='ready_to_apply' and len(provider.calls)==2


@pytest.mark.parametrize('change',['file','plan','validation','unchecked'])
def test_review_rejects_stale_or_unverified_acceptance(tmp_path,change):
    _,store,_,runner,provider,result=pending_review(tmp_path,two_tasks=False)
    snapshot=runner.review_snapshot(result.run_id)
    if change=='file': (result.integration_path/'hello.txt').write_text('Changed after review opened')
    if change=='plan': store.save_plan(result.run_id,'changed','scripted',store.get_run_plan(result.run_id))
    if change=='validation': runner.validate_task=lambda *_:'validation failed: regression'
    with pytest.raises(ValueError):
        runner.continue_review(result.run_id,snapshot,'approve',checked=() if change=='unchecked' else snapshot['criteria'])
    assert store.list_run_tasks(result.run_id)[0].status=='review_required'
    assert store.get_task_completion(store.list_run_tasks(result.run_id)[0].id).human_review_status=='pending'
    assert len(provider.calls)==1


def test_unverified_attempt_cannot_be_approved_and_resume_cannot_bypass_review(tmp_path):
    _,store,_,runner,provider,result=pending_review(tmp_path,two_tasks=False)
    # A post-checkpoint edit makes the saved completion insufficient for acceptance.
    (result.integration_path/'hello.txt').write_text('Unverified edit')
    snapshot=runner.review_snapshot(result.run_id)
    assert 'approve' in snapshot['errors']
    with pytest.raises(ValueError,match='Review & continue'): runner.resume(result.run_id)
    with pytest.raises(ValueError): runner.continue_review(result.run_id,snapshot,'approve')
    with pytest.raises(ValueError,match='Describe'): runner.continue_review(result.run_id,snapshot,'changes')


def test_review_survives_reopening_database_and_restoring_runner(tmp_path):
    from local_model_chat.storage import ChatStore
    _,store,_,runner,provider,result=pending_review(tmp_path,two_tasks=False)
    runner.store=ChatStore(store.path)
    snapshot=runner.review_snapshot(result.run_id)
    done=runner.continue_review(result.run_id,snapshot,'approve',checked=snapshot['criteria'])
    assert done.status=='ready_to_apply' and len(provider.calls)==1


def test_review_dialog_requires_checks_and_explicit_feedback(qtbot,tmp_path):
    from local_model_chat.ui.task_review import TaskReviewDialog
    _,_,_,runner,_,result=pending_review(tmp_path,two_tasks=False)
    snapshot=runner.review_snapshot(result.run_id)
    dialog=TaskReviewDialog(None,snapshot);qtbot.addWidget(dialog);dialog.show()
    assert not dialog.approve.isEnabled() and not dialog.changes.isEnabled()
    dialog.checks[0].setChecked(True);assert dialog.approve.isEnabled()
    dialog.feedback.setPlainText('Use a warmer greeting');assert dialog.changes.isEnabled()
    dialog.approve.click()
    assert dialog.selection()==('approve','Use a warmer greeting',('Greeting reads naturally',))
    assert dialog.width()<=dialog.screen().availableGeometry().width()


def test_main_window_routes_review_to_review_action_not_legacy_resume(qtbot,tmp_path,monkeypatch):
    from local_model_chat.ui.main_window import MainWindow
    _,store,project,runner,_,result=pending_review(tmp_path,two_tasks=False)
    window=MainWindow(store,start_ollama_check=False);qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    seen=[]
    monkeypatch.setattr(window,'_review_pending_task',lambda p,r:seen.append(r['id']))
    window._refresh_recovery_actions(project)
    assert window.build_resume_button.text()=='Review & continue' and window.build_resume_button.isEnabled()
    window.build_resume_button.click()
    assert seen==[result.run_id]


def test_desktop_accept_and_continue_worker_finishes_remaining_tasks(qtbot,tmp_path,monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    from local_model_chat.ui.main_window import MainWindow
    root,store,project,runner,provider,result=pending_review(tmp_path)
    window=MainWindow(store,start_ollama_check=False);qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    monkeypatch.setattr(window,'_task_review_runner',lambda *_:runner)
    def accept_dialog():
        dialog=QApplication.activeModalWidget()
        dialog.checks[0].setChecked(True)
        dialog.approve.click()
    QTimer.singleShot(20,accept_dialog)
    window.build_resume_button.click()
    qtbot.waitUntil(lambda: window.orchestration_worker is None,timeout=15000)
    assert store.get_run(result.run_id)['status']=='ready_to_apply'
    assert [key for key,_ in provider.calls]==['first','second']
    assert window.build_merge_button.isEnabled()
    monkeypatch.setattr(QMessageBox,'question',lambda *_:QMessageBox.StandardButton.Yes)
    def unexpected_message(*args):
        raise AssertionError(str(args[1:]))
    monkeypatch.setattr(QMessageBox,'warning',unexpected_message)
    monkeypatch.setattr(QMessageBox,'critical',unexpected_message)
    window.build_merge_button.click()
    assert store.get_run(result.run_id)['status']=='completed'
    assert (root/'hello.txt').read_text()=='Hello' and (root/'second.txt').read_text()=='Hello'


def test_generated_test_cache_does_not_invalidate_review(tmp_path):
    _,_,_,runner,provider,result=pending_review(tmp_path,two_tasks=False)
    cache=result.integration_path/'__pycache__';cache.mkdir()
    (cache/'hello.pyc').write_bytes(b'generated cache')
    snapshot=runner.review_snapshot(result.run_id)
    done=runner.continue_review(result.run_id,snapshot,'approve',checked=snapshot['criteria'])
    assert done.status=='ready_to_apply' and len(provider.calls)==1


def test_reviewed_task_does_not_require_its_previous_model_to_stay_available(tmp_path):
    from local_model_chat.molicoder.task_routing import TaskRouter
    _,_,_,runner,provider,result=pending_review(tmp_path,two_tasks=False)
    runner.task_router=TaskRouter({'low':[], 'medium':[], 'high':[]},local_ready=False)
    snapshot=runner.review_snapshot(result.run_id)
    assert 'changes' in snapshot['errors'] and 'approve' not in snapshot['errors']
    done=runner.continue_review(result.run_id,snapshot,'approve',checked=snapshot['criteria'])
    assert done.status=='ready_to_apply' and len(provider.calls)==1


def test_review_retry_preserves_dirty_output_before_reexecution(tmp_path):
    _,store,_,runner,provider,result=pending_review(tmp_path,two_tasks=False)
    (result.integration_path/'hello.txt').write_text('Unverified partial output')
    snapshot=runner.review_snapshot(result.run_id)
    assert 'approve' in snapshot['errors']
    done=runner.continue_review(result.run_id,snapshot,'changes','Use a warmer greeting')
    assert done.status=='needs_attention' and len(provider.calls)==2
    decision=next(json.loads(e.payload_json) for e in store.list_events(result.run_id) if e.kind=='task_review_decision')
    assert decision['failed_attempt_stash']
    assert 'Unverified partial output' in git(result.integration_path,'show',decision['failed_attempt_stash']+':hello.txt')


def test_cancelled_review_leaves_task_pending(tmp_path):
    _,store,_,runner,provider,result=pending_review(tmp_path,two_tasks=False)
    snapshot=runner.review_snapshot(result.run_id)
    runner.should_cancel=lambda:True
    with pytest.raises(ValueError,match='cancelled'):
        runner.continue_review(result.run_id,snapshot,'approve',checked=snapshot['criteria'])
    assert store.list_run_tasks(result.run_id)[0].status=='review_required'
    assert len(provider.calls)==1


@pytest.mark.parametrize('mode',['HYBRID','LOCAL_OFFLINE'])
def test_review_runtime_can_open_without_available_planning_models(tmp_path,monkeypatch,mode):
    from types import SimpleNamespace
    from datetime import datetime,timedelta,timezone
    from local_model_chat.molicoder.runtime import build_orchestrator
    from local_model_chat.molicoder.providers import ProviderStatus
    _,store,project,_,_=fixture(tmp_path)
    store.set_project_preferences(project.id,provider='codex',strategy=mode,trusted_providers=('codex',))
    store.record_action_authorization(project_id=project.id,action='build_run',scope='review',risk='external',
        decision='approved',actor='test',expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    monkeypatch.setattr('local_model_chat.molicoder.runtime.create_provider',lambda name,**kw:SimpleNamespace(
        name=name,model=kw.get('model',''),probe=lambda:ProviderStatus(name,False,False,auth_verified=False)))
    runner=build_orchestrator(store=store,project=project,client=SimpleNamespace(tags=lambda:[]),
        mode=mode,provider_name='codex',authorization_scope='review',planning_required=False)
    assert not runner.planning_providers and not runner.local_executor
