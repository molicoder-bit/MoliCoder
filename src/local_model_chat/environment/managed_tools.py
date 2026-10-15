"""Resolve only receipt-backed executables under the app's managed tool root."""

from __future__ import annotations

import json
import platform
from pathlib import Path

from ..config import data_dir
from .manifests import load_manifests


def managed_executable(tool_id: str, *, root: Path | None = None) -> Path | None:
    root = (root or data_dir() / "tools").resolve()
    architecture = {
        "amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64",
    }.get(platform.machine().lower(), platform.machine())
    candidates = (
        item for item in load_manifests()
        if item.tool_id == tool_id and item.system == platform.system()
        and item.architecture == architecture
    )
    for manifest in candidates:
        destination = (root / f"{manifest.tool_id}-{manifest.version}").resolve()
        if not destination.is_relative_to(root):
            continue
        receipt_path = destination / "install-receipt.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            executable = Path(receipt["executable"]).resolve()
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if (receipt.get("tool_id") == manifest.tool_id
                and receipt.get("version") == manifest.version
                and receipt.get("sha256") == manifest.sha256
                and executable.is_relative_to(destination)
                and executable.name == manifest.expected_executable
                and executable.is_file()):
            return executable
    return None
