from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


_OFFICIAL_RELEASE_PREFIX = {
    "ollama": "https://github.com/ollama/ollama/releases/download/",
    "kandev": "https://github.com/kdlbs/kandev/releases/download/",
}


@dataclass(frozen=True, slots=True)
class InstallManifest:
    tool_id: str
    version: str
    system: str
    architecture: str
    source_url: str
    sha256: str
    size_bytes: int
    license_id: str
    license_url: str
    prerequisites: tuple[str, ...]
    privilege: str
    install_kind: str
    expected_executable: str
    rollback: str
    source_checked_at: str
    signature_requirement: str


def load_manifests(path: Path | None = None) -> tuple[InstallManifest, ...]:
    path = path or Path(__file__).with_name("releases.json")
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("manifest catalog must be a list")
    manifests = []
    for record in records:
        manifest = InstallManifest(
            **{**record, "prerequisites": tuple(record["prerequisites"])}
        )
        prefix = _OFFICIAL_RELEASE_PREFIX.get(manifest.tool_id)
        if not prefix or not manifest.source_url.startswith(prefix):
            raise ValueError("manifest source must be an official release asset")
        if f"/v{manifest.version}/" not in manifest.source_url:
            raise ValueError("manifest version must match its release URL")
        if not re.fullmatch(r"[0-9a-f]{64}", manifest.sha256):
            raise ValueError("manifest needs a pinned SHA-256")
        if manifest.size_bytes <= 0 or manifest.system not in {"Windows", "Darwin", "Linux"}:
            raise ValueError("manifest platform and asset size are required")
        if manifest.privilege not in {"user", "administrator"}:
            raise ValueError("manifest privilege is invalid")
        if not manifest.version or not manifest.architecture or not manifest.rollback:
            raise ValueError("manifest version, architecture and rollback are required")
        if Path(manifest.expected_executable).name != manifest.expected_executable:
            raise ValueError("expected executable must be a filename")
        if urlparse(manifest.license_url).scheme != "https":
            raise ValueError("manifest license needs an HTTPS source")
        manifests.append(manifest)
    return tuple(manifests)
