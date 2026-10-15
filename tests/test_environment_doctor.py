import json
import sys

from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
from local_model_chat.environment.doctor import doctor_report, render_human
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.molicoder.cli import main


def _environment() -> EnvironmentReport:
    return EnvironmentReport(
        1, "Windows", "x86_64", 8, 16 * 1024**3, 100 * 1024**3,
        "unknown", "fixture",
        (
            DiscoveredTool("python", "C:/Python/python.exe", "3.13.0", "installed"),
            DiscoveredTool("ollama", "C:/Ollama/ollama.exe", "0.30.0", "installed"),
            DiscoveredTool("codex", "C:/Tools/codex.exe", "1.0.0", "installed"),
            DiscoveredTool("kandev", None, None, "missing"),
        ),
        ("winget",),
    )


def test_doctor_distinguishes_updates_login_and_optional_missing_components() -> None:
    report = doctor_report(_environment(), load_manifests(), auth_states={"codex": "login_needed"})
    states = {item.name: item.state for item in report.components}
    assert states == {
        "python": "ready", "ollama": "update_available",
        "codex": "login_needed", "kandev": "limited", "platform security": "ready",
    }
    assert "credentials" not in render_human(report).lower()


def test_doctor_json_entrypoint_uses_read_only_report(monkeypatch, capsys) -> None:
    monkeypatch.setattr("local_model_chat.environment.doctor.discover_environment", _environment)
    monkeypatch.setattr(sys, "argv", ["molicoder", "doctor", "--json"])

    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["version"] == 1
    assert report["environment"]["system"] == "Windows"
    assert report["components"][1]["state"] == "update_available"
