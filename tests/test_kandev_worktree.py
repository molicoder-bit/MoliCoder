import subprocess

from local_model_chat.kandev.contracts import KandevTaskRef, KandevTaskSnapshot
from local_model_chat.kandev.worktree import verify_kandev_worktree


def _git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _fixture(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "fixture@example.test")
    _git(root, "config", "user.name", "Fixture")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    worktree = tmp_path / "task-worktree"
    _git(root, "worktree", "add", "-b", "feature/task", str(worktree))
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "task")
    snapshot = KandevTaskSnapshot(
        KandevTaskRef(1, 2, "run:1", "remote-1"), "verifying", "now",
        "session-1", str(worktree), "", "", "feature/task", base, "repo-1",
    )
    return root, worktree, snapshot


def test_registered_clean_worktree_produces_checkpoint(tmp_path):
    root, worktree, snapshot = _fixture(tmp_path)
    check = verify_kandev_worktree(root, snapshot, expected_repository_id="repo-1")
    assert check.valid
    assert check.path == str(worktree.resolve())
    assert check.checkpoint == _git(worktree, "rev-parse", "HEAD")
    assert check.base_commit == snapshot.base_commit_sha


def test_repository_branch_and_dirty_worktree_mismatch_require_review(tmp_path):
    from dataclasses import replace

    root, worktree, snapshot = _fixture(tmp_path)
    assert not verify_kandev_worktree(root, snapshot, expected_repository_id="other-repo").valid
    assert not verify_kandev_worktree(root, replace(snapshot, worktree_branch="other"),
                                      expected_repository_id="repo-1").valid
    (worktree / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    check = verify_kandev_worktree(root, snapshot, expected_repository_id="repo-1")
    assert not check.valid and "uncommitted" in check.reason
    assert not verify_kandev_worktree(root, replace(snapshot, worktree=str(tmp_path / "missing")),
                                      expected_repository_id="repo-1").valid


def test_foreign_repository_worktree_is_rejected(tmp_path):
    from dataclasses import replace

    root, _worktree, snapshot = _fixture(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git(foreign, "init", "-b", "feature/task")
    _git(foreign, "config", "user.email", "fixture@example.test")
    _git(foreign, "config", "user.name", "Fixture")
    (foreign / "README.md").write_text("foreign", encoding="utf-8")
    _git(foreign, "add", "README.md")
    _git(foreign, "commit", "-m", "foreign")
    check = verify_kandev_worktree(
        root, replace(snapshot, worktree=str(foreign)), expected_repository_id="repo-1",
    )
    assert not check.valid and "not registered" in check.reason
