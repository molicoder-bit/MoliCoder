from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..storage import ChatStore
from .manifests import InstallManifest
from .policy import SetupPlan


@dataclass(frozen=True, slots=True)
class InstallReceipt:
    tool_id: str
    version: str
    sha256: str
    destination: str
    executable: str
    installed_at: str


def _download(url: str, target: Path, expected_size: int, timeout: float) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "MoliCoder-Managed-Setup"})
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as output:
        total = 0
        while chunk := response.read(1024 * 1024):
            if time.monotonic() > deadline:
                raise TimeoutError("managed download exceeded its time limit")
            total += len(chunk)
            if total > expected_size:
                raise ValueError("download exceeded the pinned asset size")
            output.write(chunk)
    if total != expected_size:
        raise ValueError("download size did not match the pinned asset")


def _extract_safe(archive: Path, destination: Path, maximum_bytes: int) -> None:
    with tarfile.open(archive, "r:gz") as contents:
        members = contents.getmembers()
        if sum(member.size for member in members) > maximum_bytes:
            raise ValueError("archive exceeds the approved extracted size")
        for member in members:
            target = (destination / member.name).resolve()
            if (not target.is_relative_to(destination.resolve())
                    or member.issym() or member.islnk() or member.isdev()):
                raise ValueError("archive contains an unsafe path or link")
        contents.extractall(destination, filter="data")


def install_managed_archive(
    store: ChatStore, plan: SetupPlan, manifest: InstallManifest,
    managed_root: Path, *, downloader: Callable[[str, Path, int, float], None] = _download,
    timeout_seconds: float = 300,
) -> InstallReceipt:
    if plan.action != "install_managed" or manifest.install_kind != "archive":
        raise ValueError("only an approved managed archive install is supported")
    if (plan.tool_id, plan.version, plan.sha256, plan.source_url) != (
        manifest.tool_id, manifest.version, manifest.sha256, manifest.source_url,
    ):
        raise ValueError("setup plan and manifest do not match")
    expected_scope = (
        f"install_managed:{manifest.tool_id}@{manifest.version}:"
        f"{manifest.system}:{manifest.architecture}:{manifest.sha256}"
    )
    if plan.scope != expected_scope or plan.privilege != manifest.privilege:
        raise ValueError("setup authorization scope does not match the manifest")
    risk = ActionRisk.PRIVILEGED if manifest.privilege == "administrator" else ActionRisk.MUTATION
    AuthorizationPolicy(store).require(ActionRequest(None, "setup", plan.scope, risk))
    root = managed_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / f"{manifest.tool_id}-{manifest.version}").resolve()
    if not destination.is_relative_to(root) or destination == root:
        raise ValueError("managed destination escapes its root")
    receipt_path = destination / "install-receipt.json"
    if destination.exists():
        if not receipt_path.is_file():
            raise FileExistsError("existing installation has no MoliCoder ownership receipt")
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
        if data.get("sha256") != manifest.sha256 or data.get("tool_id") != manifest.tool_id:
            raise FileExistsError("existing managed version differs; use an explicit update plan")
        if not Path(data["executable"]).is_file():
            raise FileNotFoundError("managed executable is missing; use repair")
        return InstallReceipt(**data)
    temporary = Path(tempfile.mkdtemp(prefix="install-", dir=root)).resolve()
    if not temporary.is_relative_to(root):
        raise ValueError("temporary install path escapes managed root")
    try:
        archive = temporary / "asset.tar.gz"
        downloader(manifest.source_url, archive, manifest.size_bytes, timeout_seconds)
        with archive.open("rb") as asset:
            digest = hashlib.file_digest(asset, "sha256").hexdigest()
        if digest != manifest.sha256:
            raise ValueError("download SHA-256 does not match the official manifest")
        staging = temporary / "staging"
        staging.mkdir()
        _extract_safe(archive, staging, manifest.size_bytes * 12)
        matches = list(staging.rglob(manifest.expected_executable))
        if len(matches) != 1 or not matches[0].is_file():
            raise ValueError("expected executable was not found exactly once")
        executable = str(destination / matches[0].relative_to(staging))
        receipt = InstallReceipt(
            manifest.tool_id, manifest.version, manifest.sha256, str(destination), executable,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        (staging / "install-receipt.json").write_text(
            json.dumps(asdict(receipt), sort_keys=True, indent=2), encoding="utf-8",
        )
        staging.rename(destination)
        return receipt
    finally:
        if temporary.exists():
            if not temporary.resolve().is_relative_to(root):
                raise ValueError("refusing to clean a path outside managed root")
            shutil.rmtree(temporary)
