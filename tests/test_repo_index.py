import subprocess

from local_model_chat.molicoder.repo_index import build_repository_index


def test_repository_index_excludes_secrets_and_changes_with_revision(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "app.py").write_text("def parse():\n    return 1\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=value", encoding="utf-8")
    generated = root / "build"
    generated.mkdir()
    (generated / "generated.py").write_text("def generated(): pass", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_parse(): pass", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "app.py", "tests/test_app.py"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "base"], check=True, capture_output=True)

    first = build_repository_index(root)
    (root / "app.py").write_text("def parse():\n    return 2\n", encoding="utf-8")
    second = build_repository_index(root)

    assert first.commit is not None
    assert first.entry("app.py").symbols == ("parse",)
    assert first.entry("tests/test_app.py").is_test
    assert first.entry(".env") is None
    assert first.entry("build/generated.py") is None
    assert second.index_id != first.index_id
    assert second.entry("app.py").sha256 != first.entry("app.py").sha256
