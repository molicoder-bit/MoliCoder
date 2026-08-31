import json
import sys

from local_model_chat.molicoder.runtime import build_validator


def test_build_validator_uses_project_allowlist_and_expands_python(tmp_path) -> None:
    state = tmp_path / ".molicoder"
    state.mkdir()
    (state / "project.json").write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "python",
                "validation": [
                    {"name": "syntax", "argv": ["{python}", "-c", "print('ok')"]}
                ],
                "constraints": [],
            }
        ),
        encoding="utf-8",
    )

    validator = build_validator(tmp_path)

    assert validator.commands[0].argv[0] == sys.executable
    assert validator.run().passed


def test_build_validator_rejects_an_empty_allowlist(tmp_path) -> None:
    state = tmp_path / ".molicoder"
    state.mkdir()
    (state / "project.json").write_text(
        json.dumps({"version": 1, "kind": "unknown", "validation": [], "constraints": []}),
        encoding="utf-8",
    )

    try:
        build_validator(tmp_path)
    except ValueError as exc:
        assert "validation" in str(exc).lower()
    else:
        raise AssertionError("empty validation allowlist must be rejected")
