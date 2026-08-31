import json

import pytest

from local_model_chat.molicoder.projectstate import detect_project_kind, ensure_project_state


@pytest.mark.parametrize(
    ("marker", "kind"),
    [
        ("pyproject.toml", "python"),
        ("package.json", "node"),
        ("pubspec.yaml", "flutter"),
    ],
)
def test_detects_project_kind_from_marker(tmp_path, marker: str, kind: str) -> None:
    (tmp_path / marker).write_text("{}", encoding="utf-8")
    assert detect_project_kind(tmp_path) == kind


def test_ensure_project_state_creates_local_metadata_without_overwriting_instructions(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    first = ensure_project_state(tmp_path)
    instructions = tmp_path / ".molicoder" / "instructions.md"
    instructions.write_text("Keep this rule.\n", encoding="utf-8")
    second = ensure_project_state(tmp_path)

    payload = json.loads((tmp_path / ".molicoder" / "project.json").read_text(encoding="utf-8"))
    assert first.kind == second.kind == "python"
    assert payload["kind"] == "python"
    assert payload["validation"][0]["name"] == "pytest"
    assert instructions.read_text(encoding="utf-8") == "Keep this rule.\n"
    assert (tmp_path / ".molicoder" / ".gitignore").read_text(encoding="utf-8") == "logs/\n"


def test_unknown_non_git_folder_is_supported(tmp_path) -> None:
    state = ensure_project_state(tmp_path)
    assert state.kind == "unknown"

