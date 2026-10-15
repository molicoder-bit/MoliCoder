from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from ..environment.manifests import load_manifests


@dataclass(frozen=True, slots=True)
class KandevDiscovery:
    executable: str | None
    version: str | None
    endpoint: str | None
    data_path: str
    config_path: str | None
    compatibility: str
    detail: str


def _probe_version(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"\b(?:v)?(\d+\.\d+\.\d+)\b", (result.stdout or result.stderr)[:200])
    return match.group(1) if match else None


def discover_kandev(
    *,
    binary: str | Path | None = None,
    endpoint: str | None = None,
    data_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    probe_version: Callable[[str], str | None] = _probe_version,
) -> KandevDiscovery:
    """Inspect known paths and one CLI version flag; never start or alter Kandev."""
    env = os.environ if environ is None else environ
    executable = str(binary) if binary is not None else which("kandev")
    resolved_binary = str(Path(executable).resolve()) if executable and Path(executable).is_file() else None
    version = probe_version(resolved_binary) if resolved_binary else None
    root = Path(data_path or env.get("KANDEV_HOME_DIR") or (home or Path.home()) / ".kandev").expanduser().resolve()
    candidates = (cwd or Path.cwd(), root, Path("/etc/kandev"))
    config = next((folder / "config.yaml" for folder in candidates if (folder / "config.yaml").is_file()), None)
    port_text = env.get("KANDEV_BACKEND_PORT") or env.get("KANDEV_PORT")
    if endpoint is None and port_text and port_text.isdecimal() and 1 <= int(port_text) <= 65535:
        endpoint = f"http://127.0.0.1:{port_text}/mcp"
    known_versions = {item.version for item in load_manifests() if item.tool_id == "kandev"}
    compatibility = "pinned_version" if version in known_versions else "unverified"
    detail = (
        "No executable found; paths are configuration candidates only." if resolved_binary is None else
        "Executable found; endpoint and adapter capabilities require connection validation."
    )
    return KandevDiscovery(
        resolved_binary, version, endpoint, str(root), str(config) if config else None,
        compatibility, detail,
    )
