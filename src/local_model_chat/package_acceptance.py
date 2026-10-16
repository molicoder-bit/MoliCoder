"""Explicitly invoked packaged acceptance check; uses real Codex subscription calls."""
import subprocess
from datetime import datetime, timedelta, timezone


from local_model_chat.storage import ChatStore
from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.molicoder.providers import CodexProvider
from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from local_model_chat.molicoder.plan_approval import approval_scope


def verify_file_workflow(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'README.md').write_text('Temporary file creation acceptance project.\n')
    (root / '.gitignore').write_text('.molicoder/\n')
    for args in [('init', '-b', 'main'), ('config', 'user.email', 'test@example.com'),
                 ('config', 'user.name', 'Acceptance'), ('add', '.'), ('commit', '-m', 'base')]:
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    store = ChatStore(tmp_path / 'acceptance.db')
    project = ProjectRegistry(store).add(root)
    provider = CodexProvider(project_root=root)

    def validate(path):
        target = path / 'thisisthenewworld.txt'
        return 'validation passed' if target.is_file() and target.read_bytes() == b'' else 'validation failed: missing or nonempty file'

    orchestrator = SequentialRunOrchestrator(
        store=store, project=project, planner=provider, provider=provider,
        validate=validate, worktree_base=tmp_path / 'worktrees',
        auto_apply=True, require_plan_approval=True, max_task_attempts=1,
    )
    prepared = orchestrator.run('Can you generate a file called thisisthenewworld.txt', mode='FRONTIER_ONLY')
    if prepared.status != 'awaiting_approval':
        raise RuntimeError(prepared.error)
    if (root / 'thisisthenewworld.txt').exists():
        raise RuntimeError('File was created before approval')
    run = store.get_run(prepared.run_id)
    plan = store.get_latest_plan_record(prepared.run_id)
    store.record_action_authorization(
        project_id=project.id, action='build_plan_execute',
        scope=approval_scope(run, plan['id'], plan['payload']), risk='mutation',
        decision='approved', actor='temporary_acceptance_test',
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )
    result = orchestrator.execute_approved(prepared.run_id)
    if result.status != 'completed':
        raise RuntimeError(result.error)
    if (root / 'thisisthenewworld.txt').read_bytes() != b'':
        raise RuntimeError('Created file is not empty')
    return {'status': 'passed', 'planned': True, 'approved': True, 'applied': True, 'file_verified': True, 'provider': str(provider.discover_binary())}
