import pytest

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

