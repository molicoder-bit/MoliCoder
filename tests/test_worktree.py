import subprocess

import pytest

from local_model_chat.molicoder.worktree import GitOperationError, IntegrationConflict, WorktreeManager


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def test_task_worktrees_commit_and_merge_into_isolated_integration(tmp_path) -> None:
    root = _repo(tmp_path)
    manager = WorktreeManager(root, run_id=7, base_dir=tmp_path / "trees")
    first = manager.create_task("alpha")
    second = manager.create_task("beta")
    (first.path / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    (second.path / "beta.txt").write_text("beta\n", encoding="utf-8")

    assert manager.commit(first, "alpha task") == ("alpha.txt",)
    assert manager.commit(second, "beta task") == ("beta.txt",)
    integration = manager.create_integration()
    manager.merge_into(integration, first)
    manager.merge_into(integration, second)

    assert (integration.path / "alpha.txt").read_text(encoding="utf-8") == "alpha\n"
    assert (integration.path / "beta.txt").read_text(encoding="utf-8") == "beta\n"
    assert not (root / "alpha.txt").exists()
    assert _git(root, "status", "--porcelain").stdout == ""


def test_merge_conflict_is_reported_with_paths(tmp_path) -> None:
    root = _repo(tmp_path)
    manager = WorktreeManager(root, run_id=8, base_dir=tmp_path / "trees")
    first = manager.create_task("first")
    second = manager.create_task("second")
    (first.path / "base.txt").write_text("first\n", encoding="utf-8")
    (second.path / "base.txt").write_text("second\n", encoding="utf-8")
    manager.commit(first, "first")
    manager.commit(second, "second")
    integration = manager.create_integration()
    manager.merge_into(integration, first)

    with pytest.raises(IntegrationConflict) as caught:
        manager.merge_into(integration, second)

    assert "base.txt" in caught.value.paths


def test_conflict_resolved_to_existing_integration_content_concludes_merge(tmp_path) -> None:
    root = _repo(tmp_path)
    manager = WorktreeManager(root, run_id=9, base_dir=tmp_path / "trees")
    first = manager.create_task("first")
    second = manager.create_task("second")
    (first.path / "base.txt").write_text("first\n", encoding="utf-8")
    (second.path / "base.txt").write_text("second\n", encoding="utf-8")
    manager.commit(first, "first")
    manager.commit(second, "second")
    integration = manager.create_integration()
    manager.merge_into(integration, first)

    with pytest.raises(IntegrationConflict) as caught:
        manager.merge_into(integration, second, preserve_conflict=True)

    (integration.path / "base.txt").write_text("first\n", encoding="utf-8")
    manager.complete_conflict_merge(integration, caught.value.paths)

    merge_head = manager._git(
        "rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=integration.path, check=False
    )
    parents = _git(integration.path, "show", "-s", "--format=%P", "HEAD").stdout.split()
    assert merge_head.returncode != 0
    assert len(parents) == 2


def test_default_worktree_paths_distinguish_same_named_repositories(tmp_path, monkeypatch) -> None:
    first_parent = tmp_path / "one"
    second_parent = tmp_path / "two"
    first_parent.mkdir()
    second_parent.mkdir()
    first = _repo(first_parent)
    second = _repo(second_parent)
    monkeypatch.setattr("local_model_chat.molicoder.worktree.data_dir", lambda: tmp_path / "data")

    first_manager = WorktreeManager(first, run_id=1)
    second_manager = WorktreeManager(second, run_id=1)

    assert first_manager.base_dir != second_manager.base_dir


def test_task_commit_excludes_python_cache_artifacts(tmp_path) -> None:
    root = _repo(tmp_path)
    manager = WorktreeManager(root, run_id=10, base_dir=tmp_path / "trees")
    workspace = manager.create_task("cache-safe")
    (workspace.path / "feature.py").write_text("value = 1\n", encoding="utf-8")
    cache = workspace.path / "__pycache__"
    cache.mkdir()
    (cache / "feature.cpython-313.pyc").write_bytes(b"generated")
    pytest_cache = workspace.path / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "README.md").write_text("generated\n", encoding="utf-8")

    changed = manager.commit(workspace, "cache-safe task")

    assert changed == ("feature.py",)
    tracked = _git(workspace.path, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "__pycache__" not in tracked
    assert ".pytest_cache" not in tracked


def test_task_commit_does_not_explicitly_add_ignored_cache(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / ".gitignore").write_text(".pytest_cache/\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-m", "ignore cache")
    manager = WorktreeManager(root, run_id=11, base_dir=tmp_path / "trees")
    workspace = manager.create_run()
    (workspace.path / "feature.py").write_text("value = 1\n", encoding="utf-8")
    cache = workspace.path / ".pytest_cache"
    cache.mkdir()
    (cache / "README.md").write_text("generated\n", encoding="utf-8")

    changed = manager.commit(workspace, "cache-safe run")

    assert changed == ("feature.py",)


def test_retry_stops_for_ignored_attempt_output_instead_of_reusing_it(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-m", "ignore attempt output")
    manager = WorktreeManager(root, run_id=12, base_dir=tmp_path / "trees")
    workspace = manager.create_run()
    (workspace.path / "ignored.txt").write_text("failed output", encoding="utf-8")

    with pytest.raises(GitOperationError, match="ignored attempt output requires review"):
        manager.preserve_failed_attempt(workspace, "failed attempt")

    assert (workspace.path / "ignored.txt").read_text() == "failed output"
