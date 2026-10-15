from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..ollama import OllamaClient


_CLI_NAMES = ("codex", "opencode", "claude", "copilot", "kilo", "qwen", "gemini")


@dataclass(frozen=True, slots=True)
class ToolDiscovery:
    tool_id: str
    installed: bool
    binary: str | None
    version: str | None
    configuration_state: str
    auth_state: str
    models: tuple[str, ...] = ()
    detail: str = ""


def _version(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0][:120] if (result.stdout or result.stderr).strip() else None


def discover_local_tools(
    *, client: OllamaClient | None = None,
    which: Callable[[str], str | None] = shutil.which,
    version: Callable[[str], str | None] = _version,
) -> tuple[ToolDiscovery, ...]:
    """Read local availability only; no login, setup, model call, or service start."""
    found: list[ToolDiscovery] = []
    if client is not None:
        try:
            tags = client.tags()
            models = tuple(str(item.get("name", "")) for item in tags if item.get("name"))
            found.append(ToolDiscovery("ollama", True, None, None, "ready", "not_required", models))
        except Exception as exc:
            found.append(ToolDiscovery("ollama", False, None, None, "unavailable", "not_required", detail=str(exc)))
    for name in _CLI_NAMES:
        binary = which(name)
        if binary is None:
            found.append(ToolDiscovery(name, False, None, None, "not_installed", "unknown"))
            continue
        resolved = str(Path(binary).resolve())
        detected_version = version(resolved)
        found.append(ToolDiscovery(
            name, True, resolved, detected_version,
            "installed" if detected_version else "limited", "unknown",
            detail="Version check does not prove authentication or account entitlement.",
        ))
    return tuple(found)
