"""Build controls and status with real persisted task transitions."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from local_model_chat.domain import Message
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.workspace import refresh_workspace
from local_model_chat.workers import OrchestrationWorker
from test_build_plan_approval import approve, fixture


class FakeClient:
    def tags(self):
        return [{"name": "molicoder:latest"}]


def test_s108_s109_pause_resume_and_chat_switch_keep_completed_work(tmp_path, qtbot):
    root, store, project, _old_provider, orchestrator = fixture(tmp_path)

    class Planner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Build two files", "tasks": [
                {"key": "first", "title": "First", "objective": "Write first.txt", "risk": "low",
                 "depends_on": [], "expected_paths": ["first.txt"]},
                {"key": "second", "title": "Second", "objective": "Write second.txt", "risk": "low",
                 "depends_on": ["first"], "expected_paths": ["second.txt"]},
            ]})

    entered_first = threading.Event()
    release_first = threading.Event()

    class Executor:
        name = "scripted"
        model = "fixture-model"

        def __init__(self):
            self.calls = []

        def execute(self, worktree, prompt, **_kwargs):
            key = "first" if "(first)" in prompt else "second"
            self.calls.append(key)
            (Path(worktree) / f"{key}.txt").write_text(f"{key}\n", encoding="utf-8")
            if key == "first":
                entered_first.set()
                assert release_first.wait(20)
            else:
                assert (Path(worktree) / "first.txt").read_text(encoding="utf-8") == "first\n"
            return ProviderResult("completed", "done", seconds=0.01)

        def cancel(self):
            pass

    executor = Executor()
    orchestrator.planner = Planner()
    orchestrator.provider = executor
    orchestrator.validate_task = lambda path, task: (
        "validation passed" if (Path(path) / f"{task.key}.txt").read_text(encoding="utf-8") == f"{task.key}\n"
        else "validation failed"
    )
    orchestrator.validate = lambda path: (
        "validation passed" if all(
            (Path(path) / f"{key}.txt").read_text(encoding="utf-8") == f"{key}\n"
            for key in ("first", "second")
        ) else "validation failed"
    )
    prepared = orchestrator.run("Build two files", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    window = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    window.show()
    window._load_project_list(select_id=project.id)
    window._set_view("build")
    original_chat = window.current_conversation_id
    store.add_message(Message(None, original_chat, "user", "Keep this conversation"))
    worker = OrchestrationWorker(orchestrator, "Build two files", "FRONTIER_ONLY", approved_run_id=prepared.run_id)
    orchestrator.on_event = worker.relay_event
    worker.event.connect(window._build_event)
    results = []
    worker.completed.connect(results.append)
    window.orchestration_worker = worker
    window.build_pause_button.setEnabled(True)
    try:
        worker.start()
        qtbot.waitUntil(entered_first.is_set, timeout=20000)
        window._toggle_build_pause()
        assert worker.paused and window.build_pause_button.text() == "Resume"
        window.new_chat_button.click()
        assert window.current_conversation_id != original_chat
        assert window.orchestration_worker is worker
        assert window.return_to_build.isVisible()
        release_first.set()
        qtbot.waitUntil(lambda: worker.pause_state == "paused", timeout=20000)
        assert executor.calls == ["first"]
        assert {task.key: task.status for task in store.list_run_tasks(prepared.run_id)}["first"] == "completed"
        window.return_to_build.click()
        assert window.view_stack.currentWidget() == window.build_view
        window._toggle_build_pause()
        qtbot.waitUntil(lambda: bool(results), timeout=20000)
        assert results[0].status == "ready_to_apply", results[0].error
        assert executor.calls == ["first", "second"]
        assert all(task.status == "completed" for task in store.list_run_tasks(prepared.run_id))
        assert any(message.content == "Keep this conversation"
                   for message in store.list_messages(original_chat))
        assert not (root / "first.txt").exists() and not (root / "second.txt").exists()
    finally:
        release_first.set()
        worker.resume()
        worker.wait(20000)
        window.orchestration_worker = None


def test_s110_out_of_order_rapid_events_follow_authoritative_task_state(tmp_path, qtbot):
    root, store, project, _provider, _orchestrator = fixture(tmp_path)
    run_id = store.create_run(project.id, "Build two files", "FRONTIER_ONLY", provider="scripted")
    first = store.create_run_task(run_id, "first", "First", "Write first", "low")
    second = store.create_run_task(run_id, "second", "Second", "Write second", "low")
    store.update_run_state(run_id, "running")
    store.record_run_owner(run_id)
    store.update_run_task(first, "completed", 1)
    store.update_run_task(second, "running", 1)
    window = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    window.show()
    window._load_project_list(select_id=project.id)
    window._set_view("build")
    window.orchestration_worker = SimpleNamespace(
        orchestrator=SimpleNamespace(_active_run_id=run_id), pause_state="running",
        isRunning=lambda: False,
    )
    for kind, key in (
        ("task_started", "second"), ("task_completed", "first"),
        ("task_started", "first"), ("task_completed", "first"),
    ):
        window._build_event(kind, {"key": key, "title": key})
    refresh_workspace(window)
    assert window.run_stage.text() == "Execute · task in progress"
    assert store.list_run_tasks(run_id)[0].status == "completed"
    assert store.list_run_tasks(run_id)[1].status == "running"
    store.update_run_task(second, "completed", 1)
    window._build_event("task_completed", {"key": "second", "title": "second"})
    refresh_workspace(window)
    assert window.run_stage.text() == "Check · task verified"
    assert all(task.status == "completed" for task in store.list_run_tasks(run_id))
    assert not (root / "first.txt").exists()
    window.orchestration_worker = None
