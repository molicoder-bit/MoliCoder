import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from local_model_chat.molicoder.runtime import build_validator, validate_intermediate_task


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


def test_intermediate_validation_allows_no_tests_but_final_validation_does_not(tmp_path) -> None:
    _write_python_project(tmp_path)
    (tmp_path / "calculator_logic.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    intermediate = validate_intermediate_task(tmp_path)
    final = build_validator(tmp_path).run_and_summarize()

    assert intermediate.startswith("validation passed")
    assert "no tests collected yet" in intermediate
    assert final.startswith("validation failed")


def test_intermediate_non_test_checkpoint_defers_failing_suite_but_test_task_runs_it(tmp_path) -> None:
    _write_python_project(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_feature.py").write_text("def test_failure():\n    assert False\n", encoding="utf-8")
    implementation_task = SimpleNamespace(
        title="Implement feature", objective="Change feature behavior", expected_paths=("feature.py",)
    )
    test_task = SimpleNamespace(
        title="Update tests", objective="Test feature behavior", expected_paths=("tests/test_feature.py",)
    )

    implementation = validate_intermediate_task(tmp_path, task=implementation_task)
    test_validation = validate_intermediate_task(tmp_path, task=test_task)

    assert implementation.startswith("validation passed")
    assert "deferred" in implementation
    assert test_validation.startswith("validation failed")


def test_task_worktree_cannot_replace_approved_source_validation_command(tmp_path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    _write_python_project(source)
    _write_python_project(worktree)
    source_config = source / ".molicoder" / "project.json"
    source_config.write_text(json.dumps({
        "version": 1, "kind": "python", "constraints": [],
        "validation": [{"name": "approved", "kind": "static", "argv": ["{python}", "-c", "print('approved')"]}],
    }), encoding="utf-8")
    worktree_config = worktree / ".molicoder" / "project.json"
    worktree_config.write_text(json.dumps({
        "version": 1, "kind": "python", "constraints": [],
        "validation": [{"name": "unapproved", "argv": ["{python}", "-c", "raise SystemExit(55)"]}],
    }), encoding="utf-8")

    validator = build_validator(worktree, environment_root=source)
    report = validator.run(("approved",))

    assert report.passed
    assert [command.name for command in validator.commands] == ["approved"]
    assert "approved" in report.results[0].output


def test_task_named_checks_select_approved_profile_and_reject_unknown(tmp_path) -> None:
    _write_python_project(tmp_path)
    config = tmp_path / ".molicoder" / "project.json"
    config.write_text(json.dumps({
        "version": 1, "kind": "python", "constraints": [],
        "validation": [
            {"name": "compile", "kind": "compile", "argv": ["{python}", "-c", "print('compiled')"]},
            {"name": "failing-test", "kind": "test", "argv": ["{python}", "-c", "raise SystemExit(7)"]},
        ],
    }), encoding="utf-8")
    selected = SimpleNamespace(
        title="Compile", objective="Compile package", expected_paths=("src/app.py",),
        verification_checks=("compile",),
    )
    assert validate_intermediate_task(tmp_path, task=selected).startswith("validation passed")
    selected.verification_checks = ("unapproved",)
    with pytest.raises(ValueError, match="approved validation allowlist"):
        validate_intermediate_task(tmp_path, task=selected)
