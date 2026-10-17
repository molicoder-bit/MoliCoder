"""Human review bound to a saved plan, task state and exact worktree contents."""
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from .plan_approval import approval_scope, plan_digest
from .planner.schema import parse_plan
from .worktree import TaskWorkspace, WorktreeManager


def continuation_assignments(runner, plan, payload, completed_keys):
    remaining = replace(plan, tasks=tuple(t for t in plan.tasks if t.key not in completed_keys))
    overrides = {k:v for k,v in payload.get('task_model_overrides', {}).items() if k not in completed_keys}
    assignments = runner._task_assignments(remaining, payload['runner_policy']['strategy'], overrides)
    assignments.update({k:v for k,v in payload['task_assignments'].items() if k in completed_keys})
    return assignments


def review_snapshot(runner, run_id):
    store = runner.store
    run, saved = store.get_run(run_id), store.get_latest_plan_record(run_id)
    if not run or run['project_id'] != runner.project.id or run['status'] != 'needs_attention' or not saved:
        raise ValueError('This run is not waiting for task review in the selected project.')
    tasks = store.list_run_tasks(run_id)
    task = next((t for t in tasks if t.status == 'review_required'), None)
    if task is None:
        raise ValueError('No task is waiting for review.')
    manager = WorktreeManager(runner.root, run_id=run_id, base_dir=runner.worktree_base)
    path = Path(run.get('worktree') or '').resolve()
    if path != (manager.base_dir/'workspace').resolve() or not path.is_dir():
        raise ValueError('The saved review worktree is missing or outside the managed run.')
    if manager.base_commit != run['base_commit']:
        raise ValueError('The project base changed. Review the preserved branch before continuing.')
    if manager._git('branch','--show-current',cwd=path).stdout.strip() != f'molicoder/run-{run_id}':
        raise ValueError('The review worktree branch changed.')
    head = manager._git('rev-parse','HEAD',cwd=path).stdout.strip()
    completion = store.get_task_completion(task.id)
    changes = runner._changed_paths(manager, path)
    diff = manager._git('diff','--no-ext-diff','--no-textconv','--binary',run['base_commit'],cwd=path).stdout
    fingerprint = hashlib.sha256(diff.encode())
    previews = []
    for name in changes:
        target = path/name
        if not target.resolve().is_relative_to(path):
            raise ValueError('Changed output escapes the review worktree.')
        if target.is_file():
            with target.open('rb') as stream:
                for chunk in iter(lambda: stream.read(65536), b''):
                    fingerprint.update(chunk)
            if name not in diff and target.stat().st_size <= 16000:
                previews.append(name + '\n' + target.read_text(encoding='utf-8', errors='replace'))
    completed_keys = {t.key for t in tasks if t.status == 'completed'}
    accepted_heads = {run['base_commit']}
    for item in tasks:
        if item.status == 'completed':
            record = store.get_task_completion(item.id)
            if record is None or manager._git('merge-base','--is-ancestor',record.revision,head,cwd=path,check=False).returncode:
                raise ValueError('A previously accepted task checkpoint is missing.')
            accepted_heads.add(record.revision)
    verified = bool(completion and completion.human_review_status == 'pending' and completion.revision == head and not changes)
    retry_safe = head in accepted_heads or bool(completion and completion.revision == head)
    plan = parse_plan(saved['payload'])
    target_plan = next(t for t in plan.tasks if t.key == task.key)
    plans, errors = {}, {}
    for decision in ('approve', 'changes'):
        try:
            payload = dict(saved['payload'], runner_policy=runner._runner_policy(run['mode']), task_review_continuation=True)
            payload['task_assignments'] = continuation_assignments(runner, plan, payload,
                completed_keys | ({task.key} if decision == 'approve' else set()))
            plans[decision] = payload
        except (ValueError, PermissionError) as exc:
            errors[decision] = str(exc)
    if not verified:
        errors['approve'] = 'This attempt has no unchanged, verified completion. Request changes or resolve the blocker; it cannot be accepted as complete.'
    if not retry_safe:
        errors['changes'] = 'An unverified commit was added to this worktree. Inspect it before retrying; it will not be silently accepted.'
    state = dict(run_id=run_id, task_id=task.id, task_state=task.status, plan_id=saved['id'],
        plan_digest=plan_digest(saved['payload']), head=head, changes=changes,
        content_digest=fingerprint.hexdigest(), completion=asdict(completion) if completion else None,
        task_states=[(t.id,t.status,t.attempts) for t in tasks], plans=plans)
    return dict(state, token=plan_digest(state), title=task.title, key=task.key,
        reason=run.get('recovery_reason') or 'This task needs review.',
        criteria=list(completion.unmet_criteria) if verified else [],
        validation=completion.validation_summary if verified else 'No verified completion is available.',
        files=list(completion.output_artifacts if verified else changes),
        diff=(diff + '\n\n' + '\n\n'.join(previews))[:100000],
        diff_truncated=len(diff + '\n\n' + '\n\n'.join(previews)) > 100000,
        errors=errors, objective=target_plan.objective, completed_keys=sorted(completed_keys))


def continue_review(runner, run_id, reviewed, decision, feedback='', checked=()):
    if decision not in {'approve','changes'}:
        raise ValueError('Choose approve or request changes.')
    current = review_snapshot(runner, run_id)
    if current['token'] != reviewed['token']:
        raise ValueError('The plan, task or files changed while reviewing. Reopen Review & continue.')
    if decision in current['errors']:
        raise ValueError(current['errors'][decision])
    if decision == 'approve' and set(checked) != set(current['criteria']):
        raise ValueError('Confirm every pending acceptance requirement before continuing.')
    if decision == 'changes' and not feedback.strip():
        raise ValueError('Describe the changes needed before retrying.')
    store = runner.store
    run = store.get_run(run_id)
    task = next(t for t in store.list_run_tasks(run_id) if t.id == current['task_id'])
    plan = parse_plan(store.get_run_plan(run_id))
    target = next(t for t in plan.tasks if t.key == task.key)
    manager = WorktreeManager(runner.root, run_id=run_id, base_dir=runner.worktree_base)
    workspace = TaskWorkspace('workspace',Path(run['worktree']),f'molicoder/run-{run_id}',run['base_commit'])
    validation = current['validation']
    if decision == 'approve':
        validation = runner._task_validation(target, manager, workspace.path)
        if not validation.lstrip().startswith('validation passed'):
            raise ValueError('The current checks did not pass. Request changes instead.\n' + validation)
        if review_snapshot(runner, run_id)['token'] != current['token']:
            raise ValueError('Files changed during validation. Reopen Review & continue.')
    if runner.should_cancel():
        raise ValueError('Review continuation cancelled; the task remains pending.')
    stash = manager.preserve_failed_attempt(workspace, f'Human review changes for task {task.key}') if decision == 'changes' else None
    payload = dict(current['plans'][decision])
    notes = dict(payload.get('task_review_feedback', {}))
    if decision == 'changes':
        notes[task.key] = feedback.strip()
        limits = dict(payload.get('task_review_attempt_limits', {}))
        limits[task.key] = task.attempts + runner.max_task_attempts
        payload['task_review_attempt_limits'] = limits
    else:
        notes.pop(task.key, None)
    payload['task_review_feedback'] = notes
    _record_decision(store, run, task, current, decision, feedback, validation, payload, stash)
    if decision == 'approve':
        runner._event(run_id, 'task_completed', dict(key=task.key,title=task.title,attempts=task.attempts,
            changed_paths=current['files'],checkpoint=current['head'],worktree=str(workspace.path),human_review=True),task.id)
    return runner.execute_approved(run_id)


def _record_decision(store, run, task, snapshot, decision, feedback, validation, payload, stash):
    """Commit approval, task completion and continuation plan together."""
    now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    actor = 'local_user_task_review'
    policy = payload['runner_policy']
    with closing(store._connect()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        latest = connection.execute('SELECT id FROM plans WHERE run_id=? ORDER BY id DESC LIMIT 1',(run['id'],)).fetchone()
        state = connection.execute('SELECT status FROM tasks WHERE id=?',(task.id,)).fetchone()
        status = connection.execute('SELECT status FROM runs WHERE id=?',(run['id'],)).fetchone()
        if latest[0] != snapshot['plan_id'] or state[0] != 'review_required' or status[0] != 'needs_attention':
            raise ValueError('Review state changed; reopen the review.')
        latest_completion = connection.execute("SELECT payload_json FROM events WHERE task_id=? AND kind='task_completion_record' ORDER BY id DESC LIMIT 1",(task.id,)).fetchone()
        if (json.loads(latest_completion[0]) if latest_completion else None) != json.loads(json.dumps(snapshot['completion'])):
            raise ValueError('Completion evidence changed; reopen the review.')
        def event(kind, data):
            connection.execute('INSERT INTO events(run_id,task_id,kind,payload_json,created_at) VALUES(?,?,?,?,?)',
                (run['id'],task.id,kind,json.dumps(data,ensure_ascii=False),now))
        revision = connection.execute('INSERT INTO plans(run_id,source,provider,json,created_at) VALUES(?,?,?,?,?)',
            (run['id'],'task_review_continuation',policy['provider'],json.dumps(payload,ensure_ascii=False),now)).lastrowid
        event('task_review_decision',dict(decision=decision,feedback=feedback,reviewed_criteria=snapshot['criteria'],
            checkpoint=snapshot['head'],review_token=snapshot['token'],actor=actor,failed_attempt_stash=stash))
        if decision == 'approve':
            completion = dict(snapshot['completion'],validation_summary=validation[-8000:],unmet_criteria=[],human_review_status='approved',created_at=now)
            event('task_completion_record',completion)
        next_state = 'completed' if decision == 'approve' else 'waiting_approval'
        connection.execute('UPDATE tasks SET status=?,updated_at=? WHERE id=?',(next_state,now,task.id))
        number = connection.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND kind='task_state_changed'",(task.id,)).fetchone()[0]+1
        event('task_state_changed',dict(**{'from':'review_required','to':next_state},attempts=task.attempts,
            actor=actor,reason='Human accepted verified result' if decision=='approve' else feedback,revision=number))
        event('plan_revised',dict(revision=revision,previous_revision=snapshot['plan_id'],digest=plan_digest(payload),reason='Human task review'))
        connection.execute("UPDATE runs SET status='awaiting_approval',provider=?,model=?,final_commit=?,recovery_reason=? WHERE id=?",
            (policy['provider'],policy['model'],snapshot['head'],'Human review: '+decision,run['id']))
        approved_run = dict(run,provider=policy['provider'],model=policy['model'])
        connection.execute('INSERT INTO action_authorizations(project_id,action,scope,risk,decision,actor,reason,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (run['project_id'],'build_plan_execute',approval_scope(approved_run,revision,payload),'mutation','approved',actor,
             'Reviewed task result and remaining execution assignments',now,(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()))
        connection.commit()
