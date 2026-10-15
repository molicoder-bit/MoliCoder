"""Verified, user-approved launch of Ollama's official Windows installer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..storage import ChatStore
from .install import InstallReceipt, _download
from .manifests import InstallManifest
from .policy import SetupPlan


def windows_authenticode_valid(asset: Path) -> bool:
    """Require Windows to validate the downloaded executable's signature."""
    if os.name != "nt":
        return False
    quoted = str(asset).replace("'", "''")
    command = (
        f"$s = Get-AuthenticodeSignature -LiteralPath '{quoted}'; "
        "if ($s.Status -eq 'Valid') { exit 0 } else { exit 1 }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, timeout=30, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _run_installer(asset: Path, destination: Path, timeout: float) -> int:
    # Flags match Ollama's official Windows installer script. The installer
    # itself owns its uninstaller and user profile data.
    result = subprocess.run(
        [str(asset), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES",
         f'/DIR="{destination}"'],
        capture_output=True, timeout=timeout, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode


def install_verified_ollama(
    store: ChatStore, plan: SetupPlan, manifest: InstallManifest, managed_root: Path,
    *, downloader: Callable[[str, Path, int, float], None] = _download,
    signature_checker: Callable[[Path], bool] = windows_authenticode_valid,
    installer_runner: Callable[[Path, Path, float], int] = _run_installer,
) -> InstallReceipt:
    if (manifest.tool_id != "ollama" or manifest.system != "Windows"
            or manifest.install_kind != "installer" or not manifest.source_url.endswith("OllamaSetup.exe")
            or plan.action != "install_managed"
            or (plan.tool_id, plan.version, plan.sha256, plan.source_url) !=
               (manifest.tool_id, manifest.version, manifest.sha256, manifest.source_url)):
        raise ValueError("exact pinned Ollama installer plan is required")
    expected_scope = (
        f"install_managed:{manifest.tool_id}@{manifest.version}:"
        f"{manifest.system}:{manifest.architecture}:{manifest.sha256}"
    )
    if plan.scope != expected_scope or plan.privilege != "user":
        raise ValueError("installer approval scope is invalid")
    AuthorizationPolicy(store).require(ActionRequest(
        None, "setup", plan.scope, ActionRisk.MUTATION,
    ))
    root = managed_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / f"ollama-{manifest.version}").resolve()
    if not destination.is_relative_to(root) or destination.exists():
        raise FileExistsError("managed Ollama destination already exists; review it before changing")
    if shutil.disk_usage(root).free < manifest.size_bytes * 4:
        raise OSError("insufficient free disk for installer download and extraction")
    temporary = Path(tempfile.mkdtemp(prefix="ollama-installer-", dir=root)).resolve()
    try:
        asset = temporary / "OllamaSetup.exe"
        downloader(manifest.source_url, asset, manifest.size_bytes, 1800)
        with asset.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != manifest.sha256:
                raise ValueError("Ollama installer SHA-256 differs from the pinned official asset")
        if not signature_checker(asset):
            raise ValueError("Ollama installer Authenticode signature is not valid")
        if installer_runner(asset, destination, 900) != 0:
            raise RuntimeError("official Ollama installer failed; inspect its registered installation")
        executable = destination / "ollama.exe"
        if not executable.is_file():
            raise FileNotFoundError("official installer did not create the expected executable")
        result = subprocess.run(
            [str(executable), "--version"], capture_output=True, text=True,
            timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or manifest.version not in (result.stdout + result.stderr):
            raise RuntimeError("installed Ollama executable did not pass its version check")
        receipt = InstallReceipt(
            manifest.tool_id, manifest.version, manifest.sha256,
            str(destination), str(executable), datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        (destination / "install-receipt.json").write_text(
            json.dumps(asdict(receipt), sort_keys=True, indent=2), encoding="utf-8",
        )
        return receipt
    finally:
        if temporary.exists() and temporary.is_relative_to(root):
            shutil.rmtree(temporary)
