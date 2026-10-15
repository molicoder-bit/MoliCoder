from __future__ import annotations

import os
import subprocess

import pytest

from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from local_model_chat.molicoder.providers import ClaudeProvider, CodexProvider
from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.molicoder.runtime import build_validator, validate_intermediate_task
from local_model_chat.storage import ChatStore


codex_live = pytest.mark.skipif(
    os.environ.get("MOLICODER_LIVE_CODEX") != "1",
    reason="set MOLICODER_LIVE_CODEX=1 to spend Codex subscription quota",
)
claude_live = pytest.mark.skipif(
    os.environ.get("MOLICODER_LIVE_CLAUDE") != "1",
    reason="set MOLICODER_LIVE_CLAUDE=1 to spend Claude subscription quota",
)


def _git(root, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


@codex_live
def test_live_codex_builds_and_applies_a_tested_tkinter_calculator(tmp_path) -> None:
    root = tmp_path / "calculator"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\nname='live-calculator'\nversion='0.1.0'\nrequires-python='>=3.11'\n\n"
        "[tool.pytest.ini_options]\npythonpath=['src']\ntestpaths=['tests']\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "molicoder@localhost")
    _git(root, "config", "user.name", "MoliCoder")
    store = ChatStore(tmp_path / "live.sqlite3")
    project = ProjectRegistry(store).add(root)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")

    provider = CodexProvider(project_root=root)
    status = provider.probe()
    assert status.installed and status.authenticated, status.detail
    orchestrator = SequentialRunOrchestrator(
        store=store,
        project=project,
        planner=provider,
        provider=provider,
        validate=lambda worktree: build_validator(worktree, environment_root=root).run_and_summarize(),
        validate_task=lambda worktree, task: validate_intermediate_task(worktree, environment_root=root, task=task),
        worktree_base=tmp_path / "worktrees",
        auto_apply=True,
        max_task_attempts=2,
    )

    result = orchestrator.run(
        "Build a small but polished Tkinter calculator. Separate arithmetic logic from the UI, "
        "support addition, subtraction, multiplication, division, decimals, clear, and friendly "
        "division-by-zero handling. Add deterministic pytest tests for the arithmetic logic. "
        "Use no third-party dependencies and never use eval.",
        mode="FRONTIER_ONLY",
    )

    assert result.status == "completed", result.error
    report = build_validator(root, environment_root=root).run()
    assert report.passed, report.summary()
    python_sources = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
    assert "tkinter" in python_sources.lower()
    assert "eval(" not in python_sources
    assert store.get_run(result.run_id)["provider"] == "codex"


@claude_live
def test_live_claude_updates_an_existing_tested_project(tmp_path) -> None:
    root = tmp_path / "greeting"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\nname='greeting'\nversion='0.1.0'\n\n"
        "[tool.pytest.ini_options]\ntestpaths=['tests']\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (root / "greeting.py").write_text('def greeting():\n    return "hello"\n', encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_greeting.py").write_text(
        'from greeting import greeting\n\ndef test_greeting():\n    assert greeting() == "hello"\n',
        encoding="utf-8",
    )
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "molicoder@localhost")
    _git(root, "config", "user.name", "MoliCoder")
    store = ChatStore(tmp_path / "claude.sqlite3")
    project = ProjectRegistry(store).add(root)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")

    provider = ClaudeProvider(project_root=root)
    status = provider.probe()
    assert status.installed and status.authenticated, status.detail
    orchestrator = SequentialRunOrchestrator(
        store=store, project=project, planner=provider, provider=provider,
        validate=lambda worktree: build_validator(worktree, environment_root=root).run_and_summarize(),
        validate_task=lambda worktree, task: validate_intermediate_task(worktree, environment_root=root, task=task),
        worktree_base=tmp_path / "worktrees", auto_apply=True, max_task_attempts=2,
    )

    result = orchestrator.run(
        'Change greeting() to return exactly "hello world" and update its deterministic pytest test. '
        "Do not edit project configuration or add dependencies.",
        mode="FRONTIER_ONLY",
    )

    assert result.status == "completed", result.error
    assert 'return "hello world"' in (root / "greeting.py").read_text(encoding="utf-8")
    assert build_validator(root, environment_root=root).run().passed
    assert store.get_run(result.run_id)["provider"] == "claude"
