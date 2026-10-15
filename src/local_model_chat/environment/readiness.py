from __future__ import annotations

from dataclasses import dataclass

from .doctor import ComponentReadiness, DoctorReport


@dataclass(frozen=True, slots=True)
class ReadinessDashboard:
    shell_ready: bool
    local_chat_ready: bool
    local_build_ready: bool
    components: tuple[ComponentReadiness, ...]


def readiness_dashboard(report: DoctorReport) -> ReadinessDashboard:
    states = {item.name: item.state for item in report.components}
    local_chat = states.get("ollama") in {"ready", "update_available"}
    local_build = local_chat and states.get("git") == "ready" and states.get("python") == "ready"
    return ReadinessDashboard(True, local_chat, local_build, report.components)
