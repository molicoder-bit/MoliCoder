"""Stage a remote candidate without applying it to the user's checkout."""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .leases import RemoteLease


MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_EXTRACTED_BYTES = 32 * 1024 * 1024
MAX_FILES = 256


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CandidateManifest:
    task_id: int
    epoch: int
    base_commit: str
    candidate_commit: str
    files: tuple[ArtifactFile, ...]


def _safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (bool(name) and not name.startswith(("/", "\\")) and "\\" not in name
            and ":" not in name and len(path.parts) <= 8
            and all(part not in {"", ".", "..", ".git"} for part in path.parts)
            and str(path) == name)


def stage_candidate(lease: RemoteLease, manifest: CandidateManifest, archive: Path,
                    staging_root: Path) -> Path:
    """Validate the exact signed manifest against a bounded archive and lease."""
    if lease.state not in {"active", "reconciling"}:
        raise ValueError("candidate lease is not active or reconciling")
    if ((manifest.task_id, manifest.epoch, manifest.base_commit) !=
            (lease.task_id, lease.epoch, lease.base_commit)
            or not re.fullmatch(r"[0-9a-f]{40,64}", manifest.candidate_commit)):
        raise ValueError("candidate revision does not match the lease")
    if not manifest.files or len(manifest.files) > MAX_FILES:
        raise ValueError("candidate file count is invalid")
    expected = {}
    for item in manifest.files:
        if (not _safe_path(item.path) or item.path.casefold() in expected
                or item.size < 0 or not re.fullmatch(r"[0-9a-f]{64}", item.sha256)):
            raise ValueError("candidate manifest contains an unsafe file")
        expected[item.path.casefold()] = item
    if sum(item.size for item in manifest.files) > MAX_EXTRACTED_BYTES:
        raise ValueError("candidate extracted size exceeds limit")
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("candidate archive exceeds size limit")
    root = staging_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="candidate-", dir=root)).resolve()
    try:
        with zipfile.ZipFile(archive) as contents:
            entries = contents.infolist()
            if len(entries) != len(expected):
                raise ValueError("candidate archive differs from its manifest")
            seen = set()
            for entry in entries:
                key = entry.filename.casefold()
                mode = entry.external_attr >> 16
                if (not _safe_path(entry.filename) or key in seen or key not in expected
                        or entry.filename != expected[key].path
                        or entry.is_dir() or stat.S_ISLNK(mode)
                        or entry.file_size != expected[key].size):
                    raise ValueError("candidate archive contains an unsafe or unexpected file")
                seen.add(key)
                target = (destination / entry.filename).resolve()
                if not target.is_relative_to(destination):
                    raise ValueError("candidate file escapes staging")
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                count = 0
                with contents.open(entry) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        count += len(chunk)
                        if count > expected[key].size:
                            raise ValueError("candidate file exceeded declared size")
                        digest.update(chunk)
                        output.write(chunk)
                if count != expected[key].size or digest.hexdigest() != expected[key].sha256:
                    raise ValueError("candidate file hash differs from its manifest")
        return destination
    except Exception:
        shutil.rmtree(destination)
        raise
