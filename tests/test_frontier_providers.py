from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from local_model_chat.molicoder.providers import (
    AGENT_PROFILES,
    ClaudeProvider,
    CodexProvider,
    CopilotProvider,
    CliFrontierProvider,
    OpenCodeProvider,
    ProviderResult,
    ProviderStatus,
    agent_profile,
    create_provider,
    redact_text,
)
from local_model_chat.molicoder.recon import ReconPacket


PLAN = {
    "objective": "Build a calculator",
    "tasks": [
        {
            "key": "calculator",
            "title": "Build calculator",
            "objective": "Implement and test the calculator.",
            "risk": "low",
            "depends_on": [],
            "expected_paths": ["src/calculator.py", "tests/test_calculator.py"],
        }
    ],
}


def test_codex_commands_are_read_only_for_planning_and_writable_only_in_worktree(tmp_path) -> None:
    binary = tmp_path / "codex.exe"
    binary.touch()
    provider = CodexProvider(binary=binary, model="gpt-test")

    plan_command, _output = provider.plan_command(tmp_path)
    execute_command = provider.execute_command(tmp_path)

    assert plan_command[plan_command.index("-s") + 1] == "read-only"
    assert execute_command.index("--approve-for-me") < execute_command.index("exec")
    assert "--dangerously-bypass-approvals-and-sandbox" not in execute_command
    assert plan_command[plan_command.index("-m") + 1] == "gpt-test"
    assert "--ephemeral" in plan_command and "--ephemeral" in execute_command
    assert str(tmp_path.resolve()) in execute_command


def test_opencode_uses_documented_headless_flags_and_requires_profile(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "opencode.exe"
    binary.touch()
    provider = OpenCodeProvider(binary=binary, model="fixture/model")
    planning, _ = provider.plan_command(tmp_path)
    execution = provider.execute_command(tmp_path)
    assert planning[:5] == [str(binary.resolve()), "--pure", "run", "--format", "json"]
    assert planning[planning.index("--agent") + 1] == "plan"
    assert execution[execution.index("--agent") + 1] == "build"
    assert "--auto" not in planning + execution
    assert json.loads(provider.invocation_environment()["OPENCODE_PERMISSION"])["edit"] == "deny"

    commands = []
    def fake_run(command, **_kwargs):
        commands.append(command)
        output = "opencode 1.2.3" if "--version" in command else "fixture\n"
        return subprocess.CompletedProcess(command, 0, output, "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    status = provider.probe()
    assert status.installed and status.authenticated
    assert commands[-1][-2:] == ["auth", "list"]
    assert "credential" in status.detail.lower()


def test_opencode_mock_invocation_passes_prompt_as_documented_argument(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "opencode.exe"
    binary.touch()
    provider = OpenCodeProvider(binary=binary, model="fixture/model")
    captured = {}
    def fake_invoke(_self, command, prompt, **kwargs):
        captured.update(command=command, prompt=prompt, kwargs=kwargs,
                        permissions=json.loads(_self.invocation_environment()["OPENCODE_PERMISSION"]))
        return ProviderResult("completed", "", events=({"part": {"type": "text", "text": "done"}},))
    monkeypatch.setattr(CliFrontierProvider, "_invoke", fake_invoke)
    result = provider._invoke(provider.execute_command(tmp_path), "Only mock work", cwd=tmp_path,
                              purpose="implementation", timeout_seconds=1)
    assert captured["command"][-1] == "Only mock work"
    assert captured["prompt"] == ""
    assert captured["permissions"]["edit"] == "allow"
    assert captured["permissions"]["bash"] == "deny"
    assert result.message == "done"


def test_opencode_local_gateway_allowlists_endpoint_and_request_id(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "opencode.exe"
    binary.touch()
    provider = OpenCodeProvider(binary=binary)
    provider.configure_local_gateway("http://127.0.0.1:11434/v1", "molicoder:latest", "local-token-0123456789abcdefghijkl")
    captured = {}
    def fake_invoke(instance, command, prompt, **_kwargs):
        captured.update(command=command, prompt=prompt, environment=instance.invocation_environment())
        return ProviderResult("completed", "done")
    monkeypatch.setattr(CliFrontierProvider, "_invoke", fake_invoke)
    provider._invoke(provider.plan_command(tmp_path)[0], "Plan", cwd=tmp_path,
                     purpose="planning", timeout_seconds=1)
    config = json.loads(captured["environment"]["OPENCODE_CONFIG_CONTENT"])
    assert config["enabled_providers"] == ["molicoder-local"]
    assert config["model"] == config["small_model"] == "molicoder-local/molicoder:latest"
    assert config["provider"]["molicoder-local"]["options"]["baseURL"] == "http://127.0.0.1:11434/v1"
    assert config["provider"]["molicoder-local"]["options"]["apiKey"].startswith("local-token-")
    assert config["provider"]["molicoder-local"]["options"]["headers"]["X-MoliCoder-Request-ID"]
    assert not any("API_KEY" in key or "TOKEN" in key for key in captured["environment"])
    assert "--title" in captured["command"]
    with pytest.raises(ValueError, match="loopback"):
        provider.configure_local_gateway("https://paid.example/v1", "molicoder:latest", "local-token-0123456789abcdefghijkl")


def test_claude_and_copilot_commands_are_noninteractive_and_scoped(tmp_path) -> None:
    claude_binary = tmp_path / "claude.exe"
    copilot_binary = tmp_path / "copilot.exe"
    claude_binary.touch()
    copilot_binary.touch()

    claude = ClaudeProvider(binary=claude_binary)
    copilot = CopilotProvider(binary=copilot_binary)

    claude_command, _ = claude.plan_command(tmp_path)
    copilot_command, _ = copilot.plan_command(tmp_path)
    assert "--permission-mode" in claude_command
    assert "plan" in claude_command
    assert "--no-session-persistence" in claude_command
    assert '"$schema"' not in claude_command[claude_command.index("--json-schema") + 1]
    assert "--no-ask-user" in copilot_command
    assert "--allow-tool" in copilot.execute_command(tmp_path)


def test_provider_plan_parses_structured_result_and_caps_plan(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "claude.exe"
    binary.touch()
    provider = ClaudeProvider(binary=binary)
    monkeypatch.setattr(
        provider,
        "_invoke",
        lambda *_args, **_kwargs: ProviderResult(
            status="completed",
            message=json.dumps({"structured_output": PLAN}),
            seconds=0.1,
        ),
    )

    plan = provider.plan(ReconPacket.empty("calculator", "python"))

    assert plan.tasks[0].key == "calculator"


def test_provider_repairs_an_invalid_plan_once(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "claude.exe"
    binary.touch()
    provider = ClaudeProvider(binary=binary)
    replies = iter(
        [
            ProviderResult("completed", '{"objective":"bad","tasks":[]}'),
            ProviderResult("completed", json.dumps({"structured_output": PLAN})),
        ]
    )
    monkeypatch.setattr(provider, "_invoke", lambda *_args, **_kwargs: next(replies))

    plan = provider.plan(ReconPacket.empty("calculator", "python"))

    assert plan.objective == "Build a calculator"


def test_probe_reports_missing_without_invoking_wrapper(tmp_path) -> None:
    provider = CopilotProvider(binary=None, search_path=str(tmp_path))

    status = provider.probe()

    assert status == ProviderStatus(
        provider="copilot", installed=False, authenticated=False,
        version="", model="CLI default", detail="GitHub Copilot CLI is not installed.", binary="",
    )


def test_codex_discovery_prefers_newest_desktop_runtime(monkeypatch, tmp_path) -> None:
    bin_root = tmp_path / "OpenAI" / "Codex" / "bin"
    current = bin_root / "versioned" / "codex.exe"
    stale = bin_root / "codex.exe"
    current.parent.mkdir(parents=True)
    stale.touch()
    current.touch()
    os.utime(stale, (1, 1))
    os.utime(current, (2, 2))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert CodexProvider().discover_binary() == current.resolve()


def test_create_provider_and_redaction() -> None:
    provider = create_provider("codex", model="frontier-test")
    assert provider.name == "codex" and provider.model == "frontier-test"
    assert "[REDACTED]" in redact_text("Authorization: Bearer abcdefghijklmnop")
    assert "abcdefghijklmnop" not in redact_text("api_key=abcdefghijklmnop")


def test_agent_profiles_expose_route_auth_permissions_and_availability() -> None:
    assert {profile.provider for profile in AGENT_PROFILES} == {"codex", "claude", "copilot", "opencode"}
    for profile in AGENT_PROFILES:
        assert profile.auth_guide.startswith("https://")
        assert profile.permission_model and profile.cancellation and profile.limitation
        assert profile.sandbox and profile.capabilities
    for name in ("codex", "claude", "copilot"):
        profile = agent_profile(name)
        assert profile.selectable and profile.route_class == "opaque_agent"
        assert profile.terms_url.startswith("https://")
    assert not agent_profile("opencode").selectable
    assert agent_profile("opencode").route_class == "local_gateway_preview"
    assert "unverified" in agent_profile("opencode").sandbox
    assert "external side effects" in agent_profile("codex").sandbox
    assert "operating-system sandbox" in agent_profile("claude").sandbox
    assert "operating-system sandbox" in agent_profile("copilot").sandbox
    with pytest.raises(ValueError, match="Unsupported agent profile"):
        agent_profile("unknown")
    with pytest.raises(ValueError, match="unsupported frontier provider"):
        create_provider("opencode")


def test_cancel_terminates_active_provider_process_within_five_seconds(tmp_path) -> None:
    script = tmp_path / "sleeping_cli.py"
    script.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(60)\n", encoding="utf-8")

    class SleepingProvider(CliFrontierProvider):
        name = "sleeping"
        display_name = "Sleeping test provider"

        def execute_command(self, worktree):
            return [sys.executable, str(script)]

        def plan_command(self, root):
            return [sys.executable, str(script)], None

    provider = SleepingProvider(binary=sys.executable)
    result = []
    worker = threading.Thread(target=lambda: result.append(provider.execute(tmp_path, "wait", timeout_seconds=30)))
    worker.start()
    deadline = time.monotonic() + 3
    while provider._process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    provider.cancel()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert time.monotonic() - started < 5
    assert result[0].status == "cancelled"
