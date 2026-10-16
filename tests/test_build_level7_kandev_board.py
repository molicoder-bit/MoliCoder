"""Approval-gated multi-task board projection against the pinned Kandev contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from local_model_chat.kandev.adapter import KandevTaskAdapter
from local_model_chat.kandev.board import KandevBoardOrchestrator
from local_model_chat.kandev.contracts import KandevConnection, KandevMode
from local_model_chat.kandev.rest import KandevTransportError
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderUnavailable
from local_model_chat.storage import ChatStore
from test_build_plan_approval import approve, fixture, git


def task(key, *parents):
    return {
        "key": key, "title": key, "objective": f"Write {key}.txt", "risk": "low",
        "depends_on": list(parents), "expected_paths": [f"{key}.txt"],
        "acceptance_criteria": ["Configured validation must pass"],
        "verification_checks": ["project-default"],
    }


class FakeBoardApi:
    def __init__(self):
        self.config = SimpleNamespace(
            agent_profile_id="profile-a", additional_agent_profile_ids=("profile-b",),
            repository_id="repo-1",
        )
        self.tasks = {}
        self.creates = []
        self.models = {"profile-a": "model-a", "profile-b": "model-b"}

    def capabilities(self):
        return ("create", "read", "lookup_external_id", "events", "cancel", "launch")

    def create_and_launch(self, external_id, objective, *, blocked_by=(), agent_profile_id=""):
        assert external_id not in self.tasks
        task_id = f"remote-{len(self.tasks) + 1}"
        raw = {
            "id": task_id, "external_id": external_id,
            "state": "CREATED" if blocked_by else "IN_PROGRESS",
            "creation_complete": True, "repository_id": "repo-1",
            "depends_on": [{"id": value} for value in blocked_by],
            "start_when_unblocked": bool(blocked_by),
            "agent_profile_id": agent_profile_id,
            "model": "" if blocked_by else self.models[agent_profile_id],
            "description": objective,
        }
        self.tasks[external_id] = raw
        self.creates.append((external_id, tuple(blocked_by), agent_profile_id))
        return dict(raw)

    def find_by_external_id(self, external_id):
        raw = self.tasks.get(external_id)
        return dict(raw) if raw else None

    def read_with_session(self, remote_task_id):
        raw = next((value for value in self.tasks.values() if value["id"] == remote_task_id), None)
        return dict(raw) if raw else None

    def complete(self, remote_task_id):
        parent = next(value for value in self.tasks.values() if value["id"] == remote_task_id)
        parent["state"] = "COMPLETED"
        for raw in self.tasks.values():
            if raw["state"] != "CREATED":
                continue
            if all(next(value for value in self.tasks.values() if value["id"] == edge["id"])["state"] == "COMPLETED"
                   for edge in raw["depends_on"]):
                raw["state"] = "IN_PROGRESS"
                raw["model"] = self.models[raw["agent_profile_id"]]


def setup(tmp_path, tasks, profiles=None):
    root, store, project, _executor, _orchestrator = fixture(tmp_path)
    plan = parse_plan({"objective": "Build board tasks", "tasks": tasks}, max_tasks=30)

    class Planner:
        def plan(self, _packet):
            return plan

    api = FakeBoardApi()
    adapter = KandevTaskAdapter(
        KandevConnection(KandevMode.ATTACHED, "http://127.0.0.1:38429/mcp", "0.97.0"),
        api, store=store,
    )
    launches = []
    board = KandevBoardOrchestrator(
        store=store, project=project, adapter=adapter, planner=Planner(),
        task_profiles=profiles or {item["key"]: "profile-a" for item in tasks},
        profile_models=api.models, authorize_launch=lambda ref: launches.append(ref.external_id),
    )
    return root, store, api, board, launches


def approved_board(store, board, count):
    prepared = board.run("Build board tasks")
    assert prepared.status == "awaiting_approval" and prepared.total_tasks == count
    with pytest.raises(PermissionError):
        board.execute_approved(prepared.run_id)
    approve(store, prepared.run_id)
    result = board.execute_approved(prepared.run_id)
    assert result.status == "running", result.error
    return result


def test_s076_every_approved_task_has_stable_remote_identity_and_project(tmp_path):
    root, store, api, board, launches = setup(tmp_path, [task("a"), task("b"), task("c")])
    result = approved_board(store, board, 3)
    local = {item.key: item for item in store.list_run_tasks(result.run_id)}
    assert len(api.tasks) == len(launches) == len(local) == 3
    assert set(api.tasks) == {
        f"molicoder:project:{board.project.id}:run:{result.run_id}:task:{item.id}"
        for item in local.values()
    }
    assert all(value["repository_id"] == "repo-1" for value in api.tasks.values())
    assert not any((root / f"{key}.txt").exists() for key in local)


def test_s077_remote_dependency_graph_matches_approved_diamond(tmp_path):
    tasks = [task("a"), task("b", "a"), task("c", "a"), task("d", "b", "c")]
    _root, store, api, board, _launches = setup(tmp_path, tasks)
    result = approved_board(store, board, 4)
    by_key = {item.key: item for item in store.list_run_tasks(result.run_id)}
    remote_by_key = {
        key: api.tasks[
            f"molicoder:project:{board.project.id}:run:{result.run_id}:task:{item.id}"
        ]["id"] for key, item in by_key.items()
    }
    assert len(remote_by_key) == 4
    for planned in tasks:
        external_id = f"molicoder:project:{board.project.id}:run:{result.run_id}:task:{by_key[planned['key']].id}"
        actual = {edge["id"] for edge in api.tasks[external_id]["depends_on"]}
        expected = {remote_by_key[parent] for parent in planned["depends_on"]}
        assert actual == expected


def test_s078_each_remote_task_uses_approved_profile_and_observed_model(tmp_path):
    profiles = {"a": "profile-a", "b": "profile-b"}
    _root, store, api, board, _launches = setup(tmp_path, [task("a"), task("b")], profiles)
    result = approved_board(store, board, 2)
    assignments = store.get_latest_plan_record(result.run_id)["payload"]["task_assignments"]
    assert assignments == {
        key: {"runner": "kandev", "agent_profile_id": profile, "model": api.models[profile]}
        for key, profile in profiles.items()
    }
    assert {raw["agent_profile_id"] for raw in api.tasks.values()} == set(profiles.values())
    assert {raw["model"] for raw in api.tasks.values()} == set(api.models.values())
    assert board.reconcile_all(result.run_id).status == "running"
    assert {item.status for item in store.list_run_tasks(result.run_id)} == {"running"}


def test_s079_remote_dependents_activate_after_parent_without_manual_move(tmp_path):
    _root, store, api, board, launches = setup(tmp_path, [task("a"), task("b", "a")])
    result = approved_board(store, board, 2)
    first, second = list(api.tasks.values())
    assert first["state"] == "IN_PROGRESS" and second["state"] == "CREATED"
    assert second["start_when_unblocked"] is True
    assert len(launches) == 2
    api.complete(first["id"])
    assert second["state"] == "IN_PROGRESS"
    reconciled = board.reconcile_all(result.run_id)
    assert reconciled.status == "needs_attention"  # Parent still needs local Git verification.
    statuses = {item.key: item.status for item in store.list_run_tasks(result.run_id)}
    assert statuses == {"a": "verifying", "b": "running"}
    assert len(launches) == 2


def test_large_board_failure_is_observed_without_claiming_integration(tmp_path):
    tasks = [
        task(f"t{index:02d}", *((f"t{index - 4:02d}",) if index >= 4 else ()))
        for index in range(24)
    ]
    profiles = {item["key"]: "profile-a" if index % 2 else "profile-b" for index, item in enumerate(tasks)}
    root, store, api, board, _launches = setup(tmp_path, tasks, profiles)
    result = approved_board(store, board, 24)
    assert len(api.tasks) == 24
    assert {raw["agent_profile_id"] for raw in api.tasks.values()} == {"profile-a", "profile-b"}
    roots = [raw for raw in api.tasks.values() if not raw["depends_on"]]
    assert len(roots) == 4 and all(raw["state"] == "IN_PROGRESS" for raw in roots)
    roots[0]["state"] = "FAILED"
    observed = board.reconcile_all(result.run_id)
    assert observed.status == "needs_attention"
    assert any(item.status == "failed" for item in store.list_run_tasks(result.run_id))
    assert all(not (root / f"t{index:02d}.txt").exists() for index in range(24))


def test_ambiguous_remote_create_recovers_identity_after_restart_without_relaunch(tmp_path):
    _root, store, api, board, launches = setup(tmp_path, [task("a"), task("b", "a")])
    create = api.create_and_launch
    lookup = api.find_by_external_id
    lost = {"pending": True}

    def create_then_disconnect(*args, **kwargs):
        result = create(*args, **kwargs)
        if len(api.creates) == 2 and lost["pending"]:
            raise KandevTransportError("connection lost after remote create")
        return result

    def unavailable_lookup(external_id):
        if lost["pending"]:
            raise KandevTransportError("read is temporarily unavailable")
        return lookup(external_id)

    api.create_and_launch = create_then_disconnect
    api.find_by_external_id = unavailable_lookup
    prepared = board.run("Build board tasks")
    approve(store, prepared.run_id)
    interrupted = board.execute_approved(prepared.run_id)
    assert interrupted.status == "needs_attention"
    assert len(api.creates) == len(launches) == 2
    assert len(api.tasks) == 2
    lost["pending"] = False
    restarted = KandevBoardOrchestrator(
        store=store, project=board.project, adapter=board.adapter,
        planner=board.planner, task_profiles=board.task_profiles,
        profile_models=board.profile_models,
        authorize_launch=lambda _ref: pytest.fail("reconciliation must not relaunch a task"),
    )
    recovered = restarted.reconcile_all(prepared.run_id)
    assert recovered.status == "running"
    assert len(api.creates) == len(launches) == 2
    assert {item.status for item in store.list_run_tasks(prepared.run_id)} == {"ready", "running"}


def test_s119_large_kandev_board_repairs_defect_and_verifies_integrated_project(tmp_path):
    tasks = [
        task(f"t{index:02d}", *((f"t{index - 4:02d}",) if index >= 4 else ()))
        for index in range(24)
    ]
    profiles = {item["key"]: "profile-a" if index % 2 else "profile-b" for index, item in enumerate(tasks)}
    root, store, api, board, launches = setup(tmp_path, tasks, profiles)
    repair_calls = []

    def validate_task(path, planned):
        target = Path(path) / f"{planned.key}.txt"
        return "validation passed" if target.is_file() and target.read_text(encoding="utf-8") == f"{planned.key}\n" else "validation failed: wrong task output"

    def repair(path, planned, failure):
        assert failure.startswith("validation failed")
        repair_calls.append(planned.key)
        (Path(path) / f"{planned.key}.txt").write_text(
            "still broken\n" if len(repair_calls) == 1 else f"{planned.key}\n",
            encoding="utf-8",
        )

    def validate_integration(path):
        return "validation passed" if all(
            (Path(path) / f"t{index:02d}.txt").read_text(encoding="utf-8") == f"t{index:02d}\n"
            for index in range(24)
        ) else "validation failed: integrated project differs"

    board.validate_task = validate_task
    board.validate_integration = validate_integration
    board.repair_task = repair
    board.worktree_base = tmp_path / "managed-repairs"
    prepared = board.run("Build a 24-module feature with dependent stages")
    assert prepared.status == "awaiting_approval" and api.creates == []
    approve(store, prepared.run_id)
    launched = board.execute_approved(prepared.run_id)
    assert launched.status == "running" and len(api.creates) == len(launches) == 24
    assert len([raw for raw in api.tasks.values() if raw["state"] == "IN_PROGRESS"]) == 4
    assert {raw["agent_profile_id"] for raw in api.tasks.values()} == {"profile-a", "profile-b"}

    by_key = {item.key: item for item in store.list_run_tasks(prepared.run_id)}
    base = store.get_run(prepared.run_id)["base_commit"]
    for planned in tasks:
        key = planned["key"]
        external_id = f"molicoder:project:{board.project.id}:run:{prepared.run_id}:task:{by_key[key].id}"
        raw = api.tasks[external_id]
        path = tmp_path / "remote-trees" / key
        path.parent.mkdir(exist_ok=True)
        branch = f"kandev-{prepared.run_id}-{key}"
        git(root, "worktree", "add", "-b", branch, str(path), base)
        (path / f"{key}.txt").write_text("defect\n" if key == "t07" else f"{key}\n", encoding="utf-8")
        git(path, "add", f"{key}.txt")
        git(path, "commit", "-m", f"remote {key}")
        raw.update({
            "worktree_path": str(path), "worktree_branch": branch,
            "base_commit_sha": base, "repository_id": "repo-1",
        })
        api.complete(raw["id"])
    assert all(raw["state"] == "COMPLETED" for raw in api.tasks.values())
    verified = board.verify_and_integrate(prepared.run_id)
    assert verified.status == "ready_to_apply", verified.error
    assert verified.completed_tasks == verified.total_tasks == 24
    assert repair_calls == ["t07", "t07"]
    assert all((verified.integration_path / f"t{index:02d}.txt").is_file() for index in range(24))
    assert all(store.get_task_completion(item.id) is not None for item in store.list_run_tasks(prepared.run_id))
    assert board.reconcile_all(prepared.run_id).status == "ready_to_apply"
    assert not any((root / f"t{index:02d}.txt").exists() for index in range(24))


def test_s120_adversarial_board_recovers_and_verifies_feature_without_duplicate_launch(tmp_path):
    tasks = [
        task("config"),
        {**task("api", "config"), "expected_paths": ["api.txt", "shared.txt"]},
        {**task("ui", "config"), "expected_paths": ["ui.txt", "shared.txt"]},
        task("integration", "api", "ui"),
    ]
    profiles = {"config": "profile-a", "api": "profile-a", "ui": "profile-b", "integration": "profile-b"}
    root, store, api, board, launches = setup(tmp_path, tasks, profiles)
    create = api.create_and_launch
    transient_create = {"raised": False}

    def create_with_lost_reply(external_id, objective, **kwargs):
        result = create(external_id, objective, **kwargs)
        if len(api.creates) == 3 and not transient_create["raised"]:
            transient_create["raised"] = True
            raise KandevTransportError("reply lost after remote task creation")
        return result

    api.create_and_launch = create_with_lost_reply
    prepared = board.run("Add a configuration-backed report API and UI with integrated output")
    assert prepared.status == "awaiting_approval" and not api.creates
    approve(store, prepared.run_id)
    launched = board.execute_approved(prepared.run_id)
    assert launched.status == "running" and transient_create["raised"]
    assert len(api.tasks) == len(api.creates) == len(launches) == 4
    assert {raw["agent_profile_id"] for raw in api.tasks.values()} == {"profile-a", "profile-b"}

    by_key = {item.key: item for item in store.list_run_tasks(prepared.run_id)}
    remote = {
        key: api.tasks[f"molicoder:project:{board.project.id}:run:{prepared.run_id}:task:{row.id}"]
        for key, row in by_key.items()
    }
    base = store.get_run(prepared.run_id)["base_commit"]
    content = {
        "config": {"config.txt": "config\n"},
        "api": {"api.txt": "broken API\n", "shared.txt": "API\n"},
        "ui": {"ui.txt": "ui\n", "shared.txt": "UI\n"},
        "integration": {"integration.txt": "integration\n"},
    }
    for key, files in content.items():
        path = tmp_path / "remote-trees" / key
        path.parent.mkdir(exist_ok=True)
        branch = f"kandev-{prepared.run_id}-{key}"
        git(root, "worktree", "add", "-b", branch, str(path), base)
        for name, value in files.items():
            (path / name).write_text(value, encoding="utf-8")
        git(path, "add", ".")
        git(path, "commit", "-m", f"remote {key}")
        remote[key].update({
            "worktree_path": str(path), "worktree_branch": branch,
            "base_commit_sha": base, "repository_id": "repo-1",
        })

    api.complete(remote["config"]["id"])
    assert remote["api"]["state"] == remote["ui"]["state"] == "IN_PROGRESS"
    api.complete(remote["api"]["id"])
    api.complete(remote["ui"]["id"])
    assert remote["integration"]["state"] == "IN_PROGRESS"
    api.complete(remote["integration"]["id"])

    read = api.read_with_session
    disconnected = {"once": True}

    def read_with_disconnect(remote_task_id):
        if disconnected["once"]:
            disconnected["once"] = False
            raise KandevTransportError("backend disconnected")
        return read(remote_task_id)

    api.read_with_session = read_with_disconnect
    interrupted = board.reconcile_all(prepared.run_id)
    assert interrupted.status == "needs_attention" and not disconnected["once"]

    reopened = ChatStore(store.path)
    restarted_adapter = KandevTaskAdapter(board.adapter.connection, api, store=reopened)
    repairs = []
    conflicts = []

    def validate_task(path, planned):
        expected = {
            "config": {"config.txt": "config\n"},
            "api": {"api.txt": "api\n", "shared.txt": "API\n"},
            "ui": {"ui.txt": "ui\n", "shared.txt": "UI\n"},
            "integration": {"integration.txt": "integration\n"},
        }[planned.key]
        return "validation passed" if all(
            (Path(path) / name).read_text(encoding="utf-8") == value
            for name, value in expected.items()
        ) else "validation failed: report feature output differs"

    def repair(path, planned, failure):
        assert planned.key == "api" and failure.startswith("validation failed")
        repairs.append(planned.key)
        if len(repairs) == 1:
            raise ProviderUnavailable("transient repair provider outage")
        (Path(path) / "api.txt").write_text("api\n", encoding="utf-8")

    def resolve(path, paths):
        conflicts.append(paths)
        assert paths == ("shared.txt",)
        (Path(path) / "shared.txt").write_text("API\nUI\n", encoding="utf-8")

    def validate_integration(path):
        expected = {
            "config.txt": "config\n", "api.txt": "api\n",
            "ui.txt": "ui\n", "integration.txt": "integration\n",
            "shared.txt": "API\nUI\n",
        }
        return "validation passed" if all(
            (Path(path) / name).read_text(encoding="utf-8") == value
            for name, value in expected.items()
        ) else "validation failed: integrated report feature differs"

    restarted = KandevBoardOrchestrator(
        store=reopened, project=board.project, adapter=restarted_adapter,
        planner=board.planner, task_profiles=profiles, profile_models=api.models,
        authorize_launch=lambda _ref: pytest.fail("restart must not relaunch remote work"),
        validate_task=validate_task, validate_integration=validate_integration,
        repair_task=repair, resolve_conflict=resolve,
        worktree_base=tmp_path / "managed-repairs",
    )
    assert restarted.reconcile_all(prepared.run_id).status == "needs_attention"
    verified = restarted.verify_and_integrate(prepared.run_id)
    assert verified.status == "ready_to_apply", verified.error
    assert verified.completed_tasks == verified.total_tasks == 4
    assert repairs == ["api", "api"] and conflicts == [("shared.txt",)]
    events = [event.kind for event in reopened.list_events(prepared.run_id)]
    assert "kandev_board_repair_route_unavailable" in events
    assert "kandev_board_integration_conflict" in events
    assert "kandev_board_integration_verified" in events
    assert len(api.creates) == len(launches) == 4
    assert all(reopened.get_task_completion(row.id) is not None for row in reopened.list_run_tasks(prepared.run_id))
    assert not any((root / name).exists() for name in ("config.txt", "api.txt", "ui.txt", "integration.txt"))
