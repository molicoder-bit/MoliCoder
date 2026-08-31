import sys

from local_model_chat.molicoder.validate import ProjectValidator, ValidationCommand


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

