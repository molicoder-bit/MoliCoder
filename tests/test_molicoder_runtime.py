import json
from pathlib import Path

from local_model_chat.molicoder.runtime import build_validator


def _write_python_project(root: Path) -> None:
    (root / ".molicoder").mkdir(parents=True)
    (root / ".molicoder" / "project.json").write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "python",
                "validation": [{"name": "pytest", "argv": ["{python}", "-m", "pytest"]}],
                "constraints": [],
            }
        ),
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")


def test_worktree_validator_reuses_source_project_virtual_environment(tmp_path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    _write_python_project(source)
    _write_python_project(worktree)
    environment_python = source / ".venv" / "Scripts" / "python.exe"
    environment_python.parent.mkdir(parents=True)
    environment_python.touch()

    validator = build_validator(worktree, environment_root=source)

    assert validator.commands[0].argv[0] == str(environment_python)
