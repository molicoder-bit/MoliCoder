import json
import subprocess

import pytest

from local_model_chat.molicoder.planner.codex_cli import (
    CallBudget,
    CodexCliConsultant,
    CodexCliPlanner,
    ExternalUnavailable,
    FallbackPlanner,
    RoutedPlanner,
)
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.recon import ReconPacket


PLAN = {
    "objective": "Feature",
    "tasks": [
        {
            "key": "one",
            "title": "One",
            "objective": "Edit app.py",
            "risk": "low",
            "depends_on": [],
            "expected_paths": ["app.py"],
        }
    ],
}


class StubPlanner:
    def __init__(self, provider="local", error=None):
        self.provider = provider
        self.error = error
        self.calls = 0

    def plan(self, _packet):
        self.calls += 1
        if self.error:
            raise self.error
        return parse_plan(PLAN)


def test_codex_cli_planner_uses_read_only_schema_ephemeral_and_stdin(tmp_path) -> None:
    binary = tmp_path / "codex.exe"
    binary.touch()
    observed = {}

    def runner(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        output = command[command.index("-o") + 1]
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(PLAN, handle)
        return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":4}}\n', "")

    planner = CodexCliPlanner(project_root=tmp_path, binary=binary, runner=runner)
    plan = planner.plan(ReconPacket.empty("Feature", "python"))

    command = observed["command"]
    assert plan.tasks[0].key == "one"
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert [command[command.index("-s") + 1], command[command.index("--output-schema") + 1]] == ["read-only", str(planner.schema_path)]
    assert command[-1] == "-"
    assert "Feature" in observed["kwargs"]["input"]
    calls = planner.drain_external_calls()
    assert calls[0].input_tokens == 10 and calls[0].output_tokens == 4


def test_nonzero_codex_exit_falls_back_to_local(tmp_path) -> None:
    binary = tmp_path / "codex.exe"
    binary.touch()

    def unavailable(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "usage limit reached")

    external = CodexCliPlanner(project_root=tmp_path, binary=binary, runner=unavailable)
    local = StubPlanner()
    planner = FallbackPlanner(external, local)

    assert planner.plan(ReconPacket.empty("Feature")).tasks[0].key == "one"
    assert local.calls == 1
    assert planner.fallback_reason == "usage limit reached"


def test_auto_routes_high_risk_requests_externally_and_simple_requests_locally() -> None:
    external = StubPlanner("codex-cli")
    local = StubPlanner("local")

    high = RoutedPlanner("AUTO", local=local, external=external)
    high.plan(ReconPacket.empty("Change payment authentication migration", "python"))
    low = RoutedPlanner("AUTO", local=local, external=external)
    low.plan(ReconPacket.empty("Rename a label", "python"))

    assert external.calls == 1
    assert local.calls == 1


def test_external_call_budget_is_a_hard_ceiling() -> None:
    budget = CallBudget(max_external_calls=1, max_planning_calls=1, max_rescue_calls=0)
    budget.consume("planning")

    with pytest.raises(ExternalUnavailable, match="budget"):
        budget.consume("planning")


def test_codex_consultant_uses_read_only_rescue_and_review_commands(tmp_path) -> None:
    binary = tmp_path / "codex.exe"
    binary.touch()
    commands = []

    def runner(command, **kwargs):
        commands.append((command, kwargs))
        output = command[command.index("-o") + 1]
        with open(output, "w", encoding="utf-8") as handle:
            handle.write("Use a smaller repair strategy.")
        return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed","usage":{"input_tokens":8,"output_tokens":3}}', "")

    consultant = CodexCliConsultant(project_root=tmp_path, binary=binary, runner=runner)

    assert "smaller" in consultant.rescue("failure packet")
    assert "smaller" in consultant.review(tmp_path, "abc123")
    assert "-s" in commands[0][0] and "read-only" in commands[0][0]
    assert commands[1][0][1:3] == ["exec", "review"]
    assert "--base" in commands[1][0]
    assert [call.purpose for call in consultant.drain_external_calls()] == ["rescue", "review"]


def test_direct_external_patch_is_a_separate_explicit_writable_path(tmp_path) -> None:
    binary = tmp_path / "codex.exe"
    binary.touch()
    observed = []

    def runner(command, **_kwargs):
        observed.append(command)
        output = command[command.index("-o") + 1]
        with open(output, "w", encoding="utf-8") as handle:
            handle.write("patched")
        return subprocess.CompletedProcess(command, 0, "", "")

    consultant = CodexCliConsultant(project_root=tmp_path, binary=binary, runner=runner)
    assert consultant.direct_patch(tmp_path, "Apply the explicitly approved small fix") == "patched"
    command = observed[0]
    assert command[command.index("-s") + 1] == "workspace-write"
    call = consultant.drain_external_calls()[0]
    assert call.reason == "human_requested_direct_patch"
