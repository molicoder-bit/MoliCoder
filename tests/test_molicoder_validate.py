import sys
import pytest

from local_model_chat.knowledge import ProjectKnowledge
from local_model_chat.molicoder.validate import (
    ProjectValidator, ValidationCommand, assess_architectural_invariants,
    check_architectural_invariants,
)
from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from types import SimpleNamespace
from local_model_chat.storage import ChatStore


def test_explicit_approved_path_invariant_flags_conflicting_edit(tmp_path) -> None:
    store = ChatStore(tmp_path / "invariant.sqlite3")
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    knowledge = ProjectKnowledge(store)
    decision = knowledge.propose_decision(
        project.id, title="Protect auth", body="Do not modify src/auth.py",
        rationale="Security boundary", author="user",
    )
    assert check_architectural_invariants(store, project.id, ("src/auth.py",)) == ()
    knowledge.approve_decision(decision.id, actor="user")

    conflicts = check_architectural_invariants(store, project.id, ("src/auth.py", "src/other.py"))

    assert len(conflicts) == 1
    assert conflicts[0].decision_id == decision.id
    assert "src/auth.py" in conflicts[0].message


def test_validator_runs_only_configured_commands_and_saves_logs(tmp_path) -> None:
    validator = ProjectValidator(
        tmp_path,
        [
            ValidationCommand(
                "syntax",
                (sys.executable, "-c", "print('validation passed')"),
            )
        ],
    )

    report = validator.run()

    assert report.passed is True
    assert report.results[0].exit_code == 0
    assert "validation passed" in report.results[0].output
    assert report.results[0].log_path.is_file()


def test_validator_stops_after_first_failure_and_extracts_error(tmp_path) -> None:
    validator = ProjectValidator(
        tmp_path,
        [
            ValidationCommand(
                "failure",
                (sys.executable, "-c", "import sys; print('ERROR broken'); sys.exit(2)"),
            ),
            ValidationCommand("must-not-run", (sys.executable, "-c", "raise SystemExit(9)")),
        ],
    )

    report = validator.run()

    assert report.passed is False
    assert len(report.results) == 1
    assert report.results[0].exit_code == 2
    assert report.results[0].errors == ("ERROR broken",)


def test_structural_validation_rejects_invalid_toml_before_commands(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project\nname='broken'\n", encoding="utf-8")
    validator = ProjectValidator(
        tmp_path,
        [ValidationCommand("should-not-run", (sys.executable, "-c", "raise SystemExit(99)"))],
    )

    report = validator.run()

    assert not report.passed
    assert report.results[0].command.name == "structural-safety"
    assert "invalid pyproject.toml" in report.summary()


def test_structural_validation_rejects_unrestricted_eval(tmp_path) -> None:
    (tmp_path / "calculator.py").write_text("def calculate(text):\n    return eval(text)\n", encoding="utf-8")
    validator = ProjectValidator(
        tmp_path,
        [ValidationCommand("ok", (sys.executable, "-c", "print('ok')"))],
    )

    report = validator.run()

    assert not report.passed
    assert "unrestricted eval" in report.summary()


def test_named_checks_run_only_approved_commands_with_category_and_timeout(tmp_path) -> None:
    validator = ProjectValidator(tmp_path, [
        ValidationCommand("lint", (sys.executable, "-c", "print('lint ok')"), kind="lint"),
        ValidationCommand("security", (sys.executable, "-c", "raise SystemExit(9)"), kind="security"),
    ])

    report = validator.run(("lint",))

    assert report.passed and len(report.results) == 1
    assert "lint/lint: exit 0" in report.summary()
    with pytest.raises(ValueError, match="approved validation allowlist"):
        validator.run(("unapproved-command",))

    timed = ProjectValidator(tmp_path, [
        ValidationCommand("slow", (sys.executable, "-c", "import time; time.sleep(2)"), 0.1, "test"),
    ]).run(("slow",))
    assert not timed.passed and timed.results[0].exit_code == 124
    assert "timed out" in timed.results[0].log_path.read_text()


def test_passing_validation_cannot_override_approved_path_rule(tmp_path) -> None:
    store = ChatStore(tmp_path / "architecture.sqlite3")
    project = store.upsert_project("Sample", str(tmp_path), "python")
    knowledge = ProjectKnowledge(store)
    decision = knowledge.propose_decision(
        project.id, title="Protect auth", body="Do not modify src/auth.py",
        rationale="Security boundary", author="user",
    )
    knowledge.approve_decision(decision.id, actor="user")
    validator = ProjectValidator(tmp_path, [
        ValidationCommand("test", (sys.executable, "-c", "print('all tests passed')")),
    ])
    assert validator.run().passed

    orchestrator = object.__new__(SequentialRunOrchestrator)
    orchestrator.store = store
    orchestrator.project = project
    orchestrator._changed_paths = lambda _manager, _workspace: ("src/auth.py",)
    reason = orchestrator._scope_error(
        SimpleNamespace(expected_paths=("src/auth.py",)), None, tmp_path,
    )

    assert "architectural invariant conflict" in reason
    assert f"decision #{decision.id}" in reason


def test_ambiguous_approved_architecture_rule_requires_review(tmp_path) -> None:
    store = ChatStore(tmp_path / "architecture-review.sqlite3")
    project = store.upsert_project("Sample", str(tmp_path), "python")
    knowledge = ProjectKnowledge(store)
    decision = knowledge.propose_decision(
        project.id, title="Architecture: persistence", body="Keep storage local and auditable",
        rationale="User privacy", author="user",
    )
    knowledge.approve_decision(decision.id, actor="user")
    assessment = assess_architectural_invariants(store, project.id, ("src/storage.py",))

    assert assessment.conflicts == ()
    assert f"decision #{decision.id}" in assessment.review_required[0]

