import json
import subprocess
from pathlib import Path

from local_model_chat.domain import GenerationMetrics
from local_model_chat.inference.adapters.ollama import ensure_local_offering
from local_model_chat.inference.policy import InferencePolicy
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.molicoder.gateway import InferenceGateway
from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult
from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.molicoder.selective_review import SelectiveModelReviewer
from local_model_chat.ollama import ChatResponse
from local_model_chat.storage import ChatStore


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    store = ChatStore(tmp_path / "review.sqlite3")
    project = ProjectRegistry(store).add(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root, store, project


class Planner:
    provider = "fixture"

    def __init__(self, criterion):
        self.criterion = criterion

    def plan(self, _packet):
        return parse_plan({"objective": "Create output", "tasks": [{
            "key": "one", "title": "First", "objective": "Create one.txt",
            "risk": "low", "depends_on": [], "expected_paths": ["one.txt"],
            "acceptance_criteria": [self.criterion], "verification_checks": ["project-default"],
        }]})


class Executor:
    name = "fixture"
    model = "fixture"

    def execute(self, worktree, _prompt, *, timeout_seconds=1200):
        (Path(worktree) / "one.txt").write_text("one\n", encoding="utf-8")
        return ProviderResult("completed", "done")

    def cancel(self):
        pass


class LocalClient:
    def __init__(self, approved_ids):
        self.approved_ids = approved_ids
        self.calls = 0

    def chat(self, _model, _messages, **_kwargs):
        self.calls += 1
        return ChatResponse({"content": json.dumps({
            "approved_ids": self.approved_ids, "reason": "checkpoint diff supports it",
        })}, GenerationMetrics(prompt_tokens=50, output_tokens=20))


def _reviewer(store, project, client, *, enabled=True):
    registry = ProviderRegistry(store)
    offering_id = ensure_local_offering(registry, "review:fixture", context_window=8192, max_output_tokens=512)
    if enabled:
        registry.set_offering_state(offering_id, status="eligible", health_state="healthy",
                                    actor="fixture", reason="test route")
    gateway = InferenceGateway(client, policy=InferencePolicy(registry), project_id=project.id)
    return SelectiveModelReviewer(gateway, "review:fixture", context_window=8192)


def _run(tmp_path, criterion, client, *, route_enabled=True, validation="validation passed"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, store, project = _project(tmp_path)
    reviewer = _reviewer(store, project, client, enabled=route_enabled)
    result = SequentialRunOrchestrator(
        store=store, project=project, planner=Planner(criterion), provider=Executor(),
        validate=lambda _root: validation, worktree_base=tmp_path / "worktrees",
        auto_apply=True, max_task_attempts=1, model_reviewer=reviewer,
    ).run("Create output", mode="FRONTIER_ONLY")
    return root, store, result


def test_selective_review_accepts_explicit_criterion_after_validation(tmp_path):
    client = LocalClient([0])
    root, store, result = _run(tmp_path, "Model review: output contains one", client)
    task = store.list_run_tasks(result.run_id)[0]
    assert result.status == "completed" and task.status == "completed"
    assert (root / "one.txt").read_text(encoding="utf-8") == "one\n"
    assert store.get_task_completion(task.id).unmet_criteria == ()
    assert client.calls == 1
    assert any(event.kind == "model_review_finished" for event in store.list_events(result.run_id))
    with store._connect() as connection:
        assert connection.execute(
            "SELECT purpose FROM inference_requests ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0] == "review"


def test_human_criterion_and_failed_validation_never_invoke_reviewer(tmp_path):
    client = LocalClient([0])
    root, store, result = _run(tmp_path, "Output reviewed by a person", client)
    assert result.status == "needs_attention" and client.calls == 0
    assert not (root / "one.txt").exists()
    client = LocalClient([0])
    _root, _store, failed = _run(tmp_path / "second", "Model review: output contains one", client,
                                validation="validation failed")
    assert failed.status == "needs_attention" and client.calls == 0


def test_review_without_eligible_route_keeps_task_for_review(tmp_path):
    client = LocalClient([0])
    root, store, result = _run(tmp_path, "Model review: output contains one", client,
                               route_enabled=False)
    task = store.list_run_tasks(result.run_id)[0]
    assert result.status == "needs_attention" and task.status == "review_required"
    assert store.get_task_completion(task.id).unmet_criteria == ("Model review: output contains one",)
    assert client.calls == 0 and not (root / "one.txt").exists()
    assert any(event.kind == "model_review_skipped" for event in store.list_events(result.run_id))


def test_malformed_review_verdict_cannot_complete_task(tmp_path):
    client = LocalClient([4])
    root, store, result = _run(tmp_path, "Model review: output contains one", client)
    task = store.list_run_tasks(result.run_id)[0]
    assert result.status == "needs_attention" and task.status == "review_required"
    assert client.calls == 1 and not (root / "one.txt").exists()
    assert store.get_task_completion(task.id).unmet_criteria == ("Model review: output contains one",)
