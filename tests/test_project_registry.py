import pytest
import subprocess
import json

from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.storage import ChatStore


def test_registry_add_select_persist_and_remove(tmp_path) -> None:
    database = tmp_path / "chats.sqlite3"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "pyproject.toml").write_text("[project]\nname='first'\n", encoding="utf-8")
    (second_root / "package.json").write_text("{}", encoding="utf-8")
    registry = ProjectRegistry(ChatStore(database))

    first = registry.add(first_root)
    second = registry.add(second_root, name="Web app")
    registry.select(second.id)

    reopened = ProjectRegistry(ChatStore(database))
    assert [project.name for project in reopened.list()] == ["first", "Web app"]
    assert reopened.selected().id == second.id
    assert reopened.selected().kind == "node"

    reopened.remove(first.id)
    assert [project.id for project in reopened.list()] == [second.id]
    assert first_root.is_dir()
    assert (first_root / ".molicoder" / "project.json").is_file()


def test_registry_rejects_missing_folder(tmp_path) -> None:
    registry = ProjectRegistry(ChatStore(tmp_path / "chats.sqlite3"))
    with pytest.raises(ValueError, match="does not exist"):
        registry.add(tmp_path / "missing")


def test_registry_creates_a_clean_committed_python_project(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "local_model_chat.molicoder.projectstate._setup_python_environment",
        lambda _root: None,
    )
    registry = ProjectRegistry(ChatStore(tmp_path / "chats.sqlite3"))

    project = registry.create(tmp_path, "fresh-app", "python")

    root = tmp_path / "fresh-app"
    assert project.path == str(root.resolve())
    assert project.kind == "python"
    assert (root / "pyproject.toml").is_file()
    assert (root / "README.md").is_file()
    assert (root / ".molicoder" / "project.json").is_file()
    assert subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        check=True, capture_output=True, text=True,
    ).stdout.strip() == "main"
    assert subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout == ""
    assert subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip() == "1"


def test_registry_can_finish_a_project_folder_that_only_has_molicoder_metadata(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "local_model_chat.molicoder.projectstate._setup_python_environment",
        lambda _root: None,
    )
    root = tmp_path / "existing-empty-project"
    root.mkdir()
    ProjectRegistry(ChatStore(tmp_path / "first.sqlite3")).add(root)

    project = ProjectRegistry(ChatStore(tmp_path / "second.sqlite3")).create(
        tmp_path, root.name, "python"
    )

    assert project.kind == "python"
    assert (root / ".git").is_dir()


def test_created_node_project_uses_dependency_free_validation(tmp_path) -> None:
    registry = ProjectRegistry(ChatStore(tmp_path / "node.sqlite3"))

    project = registry.create(tmp_path, "node-app", "node")

    config = json.loads(
        (tmp_path / "node-app" / ".molicoder" / "project.json").read_text(encoding="utf-8")
    )
    assert project.kind == "node"
    assert config["validation"][0]["argv"] == ["npm", "test"]


@pytest.mark.parametrize("name", ["../escape", "nested/project", "", "project name"])
def test_project_creation_rejects_unsafe_names(tmp_path, name) -> None:
    registry = ProjectRegistry(ChatStore(tmp_path / "unsafe.sqlite3"))

    with pytest.raises(ValueError, match="project name"):
        registry.create(tmp_path, name, "python")


def test_project_creation_refuses_a_nonempty_existing_folder(tmp_path) -> None:
    root = tmp_path / "existing"
    root.mkdir()
    user_file = root / "important.txt"
    user_file.write_text("keep me", encoding="utf-8")
    registry = ProjectRegistry(ChatStore(tmp_path / "safe.sqlite3"))

    with pytest.raises(ValueError, match="empty folder"):
        registry.create(tmp_path, root.name, "python")

    assert user_file.read_text(encoding="utf-8") == "keep me"

