from __future__ import annotations

import hashlib
import json
import stat
import shutil
import tarfile
import tempfile
import time
import urllib.request
import zipfile
import zstandard
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


def _extract_zip_safe(archive: Path, destination: Path, maximum_bytes: int) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as contents:
        members = contents.infolist()
        if sum(member.file_size for member in members) > maximum_bytes:
            raise ValueError("archive exceeds the approved extracted size")
        for member in members:
            target = (root / member.filename).resolve()
            mode = member.external_attr >> 16
            if (not target.is_relative_to(root) or member.filename.startswith(("/", "\\"))
                    or "\\" in member.filename or stat.S_ISLNK(mode)):
                raise ValueError("archive contains an unsafe path or link")
        for member in members:
            target = root / member.filename
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with contents.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _extract_stream_tar_safe(contents: tarfile.TarFile, destination: Path, maximum_bytes: int) -> None:
    root = destination.resolve()
    links: list[tuple[Path, str, bool]] = []
    total = 0
    count = 0
    for member in contents:
        count += 1
        if count > 4096 or "\\" in member.name:
            raise ValueError("archive has too many entries or an unsafe path")
        target = (root / member.name).resolve()
        if not target.is_relative_to(root):
            raise ValueError("archive contains an unsafe path")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.isfile():
            total += member.size
            if total > maximum_bytes:
                raise ValueError("archive exceeds the approved extracted size")
            target.parent.mkdir(parents=True, exist_ok=True)
            original = contents.extractfile(member)
            if original is None:
                raise ValueError("archive file could not be read")
            with original, target.open("xb") as output:
                shutil.copyfileobj(original, output, length=1024 * 1024)
        elif member.issym() or member.islnk():
            links.append((target, member.linkname, member.issym()))
        else:
            raise ValueError("archive contains an unsupported special file")
    for target, linkname, symbolic in links:
        if not linkname or "\\" in linkname or Path(linkname).is_absolute():
            raise ValueError("archive contains an unsafe link")
        linked = ((target.parent if symbolic else root) / linkname).resolve()
        if not linked.is_relative_to(root) or not linked.is_file() or target.exists() or target.is_symlink():
            raise ValueError("archive link target is unsafe or missing")
        target.parent.mkdir(parents=True, exist_ok=True)
        total += linked.stat().st_size
        if total > maximum_bytes:
            raise ValueError("archive exceeds the approved extracted size")
        # Materialize only verified in-root regular files. This avoids leaving
        # symlink traversal opportunities in the managed installation.
        shutil.copy2(linked, target)


def _extract_zstd_safe(archive: Path, destination: Path, maximum_bytes: int) -> None:
    with archive.open("rb") as source, zstandard.ZstdDecompressor().stream_reader(source) as decoded:
        with tarfile.open(fileobj=decoded, mode="r|") as contents:
            _extract_stream_tar_safe(contents, destination, maximum_bytes)


def _extract_tgz_safe(archive: Path, destination: Path, maximum_bytes: int) -> None:
    with tarfile.open(archive, mode="r|gz") as contents:
        _extract_stream_tar_safe(contents, destination, maximum_bytes)


def install_managed_archive(
    store: ChatStore, plan: SetupPlan, manifest: InstallManifest,
    managed_root: Path, *, downloader: Callable[[str, Path, int, float], None] = _download,
    timeout_seconds: float = 300,
) -> InstallReceipt:
    if plan.action != "install_managed" or manifest.install_kind != "archive":
        raise ValueError("only an approved managed archive install is supported")
    if not manifest.source_url.endswith((".tar.gz", ".tgz", ".tar.zst", ".zip")):
        raise ValueError("managed archive format is unsupported")
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
    if shutil.disk_usage(root).free < plan.estimated_disk_bytes:
        raise OSError("insufficient free disk for the approved managed install")
    temporary = Path(tempfile.mkdtemp(prefix="install-", dir=root)).resolve()
    if not temporary.is_relative_to(root):
        raise ValueError("temporary install path escapes managed root")
    try:
        archive = temporary / (
            "asset.zip" if manifest.source_url.endswith(".zip") else
            "asset.tar.zst" if manifest.source_url.endswith(".tar.zst") else "asset.tar.gz"
        )
        downloader(manifest.source_url, archive, manifest.size_bytes, timeout_seconds)
        with archive.open("rb") as asset:
            digest = hashlib.file_digest(asset, "sha256").hexdigest()
        if digest != manifest.sha256:
            raise ValueError("download SHA-256 does not match the official manifest")
        staging = temporary / "staging"
        staging.mkdir()
        if manifest.source_url.endswith(".zip"):
            _extract_zip_safe(archive, staging, manifest.size_bytes * 12)
        elif manifest.source_url.endswith(".tar.zst"):
            _extract_zstd_safe(archive, staging, manifest.size_bytes * 12)
        elif manifest.source_url.endswith(".tgz"):
            _extract_tgz_safe(archive, staging, manifest.size_bytes * 12)
        else:
            _extract_safe(archive, staging, manifest.size_bytes * 12)
        matches = list(staging.rglob(manifest.expected_executable))
        if len(matches) != 1 or not matches[0].is_file():
            raise ValueError("expected executable was not found exactly once")
        if manifest.system != "Windows":
            matches[0].chmod(0o700)
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
