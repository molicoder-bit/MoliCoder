from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .discovery import EnvironmentReport, discover_environment
from .manifests import InstallManifest, load_manifests


_STATES = {
    "ready", "limited", "login_needed", "permission_needed",
    "unsupported", "failed", "update_available",
}


@dataclass(frozen=True, slots=True)
class ComponentReadiness:
    name: str
    state: str
    evidence: str
    next_action: str

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError("unsupported doctor state")


@dataclass(frozen=True, slots=True)
class DoctorReport:
    version: int
    observed_at: str
    environment: EnvironmentReport
    components: tuple[ComponentReadiness, ...]


def _version_tuple(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    match = re.search(r"\d+(?:\.\d+)+", value)
    return tuple(int(part) for part in match.group().split(".")) if match else None


def doctor_report(
    environment: EnvironmentReport, manifests: tuple[InstallManifest, ...],
    *, auth_states: dict[str, str] | None = None,
) -> DoctorReport:
    auth_states = auth_states or {}
    components: list[ComponentReadiness] = []
    for tool in environment.tools:
        candidates = [item for item in manifests if item.tool_id == tool.name
                      and item.system == environment.system
                      and item.architecture == environment.architecture]
        manifest = max(candidates, key=lambda item: _version_tuple(item.version) or ()) if candidates else None
        if tool.state == "missing":
            state = "unsupported" if tool.name in {"ollama", "kandev"} and manifest is None else "limited"
            action = (
                "No verified managed-install manifest for this platform. Use the tool's official setup instructions."
                if state == "unsupported" else
                "Review the pinned manifest and authorize setup only if this component is needed."
                if manifest else "Optional tool; install separately only if needed."
            )
            components.append(ComponentReadiness(tool.name, state, "not found on PATH", action))
            continue
        authentication = auth_states.get(tool.name, "unknown")
        if authentication == "login_needed":
            state, action = "login_needed", "Sign in using the tool's supported flow, then rerun doctor."
        elif authentication == "permission_needed":
            state, action = "permission_needed", "Grant the needed permission explicitly, then rerun doctor."
        elif authentication == "failed":
            state, action = "failed", "Inspect the tool's local diagnostics; credentials are not read by doctor."
        elif manifest and _version_tuple(tool.version) and _version_tuple(tool.version) < _version_tuple(manifest.version):
            state, action = "update_available", "Review the pinned release and its checksum before an explicit update."
        elif tool.version is None:
            state, action = "limited", "Executable found, but version could not be verified."
        elif tool.name in {"codex", "opencode", "claude", "copilot", "kilo", "qwen", "gemini"} and authentication != "ready":
            state, action = "limited", "Binary found; authentication and account entitlement remain unverified."
        else:
            state, action = "ready", "No action needed."
        components.append(ComponentReadiness(
            tool.name, state, f"{tool.path or 'path unknown'}; version {tool.version or 'unknown'}", action,
        ))
    return DoctorReport(
        1, datetime.now(timezone.utc).isoformat(timespec="seconds"),
        environment, tuple(components),
    )


def render_human(report: DoctorReport) -> str:
    lines = [
        f"MoliCoder doctor · {report.environment.system} {report.environment.architecture}",
        f"CPU: {report.environment.cpu_count or 'unknown'} · RAM: {report.environment.memory_bytes or 'unknown'} bytes",
        f"Disk free: {report.environment.disk_free_bytes or 'unknown'} bytes · GPU: {report.environment.gpu}",
    ]
    lines.extend(
        f"{item.name}: {item.state} — {item.evidence}. {item.next_action}"
        for item in report.components
    )
    return "\n".join(lines)


def doctor_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only MoliCoder environment doctor")
    parser.add_argument("--json", action="store_true", help="Print machine-readable report")
    args = parser.parse_args(argv)
    report = doctor_report(discover_environment(), load_manifests())
    print(json.dumps(asdict(report), indent=2) if args.json else render_human(report))
    return 0
