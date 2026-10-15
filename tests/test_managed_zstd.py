import hashlib
import io
import tarfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import zstandard

from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
from local_model_chat.environment.install import _extract_zstd_safe, install_managed_archive
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.environment.policy import plan_setup
from local_model_chat.storage import ChatStore


def _archive(name="bin/ollama", *, link=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as contents:
        info = tarfile.TarInfo(name)
        if link is None:
            data = b"small fixture binary"
            info.size = len(data)
            contents.addfile(info, io.BytesIO(data))
        else:
            info.type = tarfile.SYMTYPE
            info.linkname = link
            contents.addfile(info)
    return zstandard.ZstdCompressor().compress(buffer.getvalue())


def test_pinned_linux_ollama_zstd_archive_installs_under_managed_root(tmp_path) -> None:
    store = ChatStore(tmp_path / "zstd.sqlite3")
    asset = _archive()
    original = next(item for item in load_manifests()
                    if item.tool_id == "ollama" and item.system == "Linux")
    manifest = replace(original, size_bytes=len(asset), sha256=hashlib.sha256(asset).hexdigest())
    environment = EnvironmentReport(
        1, "Linux", "x86_64", 4, 16 * 1024**3, 20 * 1024**3,
        "unknown", "fixture", (DiscoveredTool("ollama", None, None, "missing"),), (),
    )
    plan = plan_setup(store, environment, manifest)
    assert plan.estimated_disk_bytes == len(asset) * 4
    store.record_action_authorization(
        project_id=None, action="setup", scope=plan.scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )

    def download(_url, target, _size, _timeout):
        target.write_bytes(asset)

    root = tmp_path / "managed"
    receipt = install_managed_archive(
        store, plan_setup(store, environment, manifest), manifest, root,
        downloader=download,
    )
    assert (root / f"ollama-{manifest.version}" / "bin" / "ollama").read_bytes() == b"small fixture binary"
    assert Path(receipt.executable).parts[-2:] == ("bin", "ollama")
    assert (root / f"ollama-{manifest.version}" / "install-receipt.json").is_file()


@pytest.mark.parametrize("name,link", [
    ("../../outside", None),
    ("bin/ollama", "/etc/passwd"),
    ("bin/ollama", "../../outside"),
])
def test_zstd_archive_rejects_escaping_files_and_links(tmp_path, name, link) -> None:
    archive = tmp_path / "unsafe.tar.zst"
    archive.write_bytes(_archive(name, link=link))
    destination = tmp_path / "staging"
    destination.mkdir()
    with pytest.raises(ValueError, match="unsafe"):
        _extract_zstd_safe(archive, destination, 1024)
    assert not (tmp_path / "outside").exists()


def test_zstd_safe_relative_link_is_materialized_as_regular_file(tmp_path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as contents:
        data = b"shared library fixture"
        file = tarfile.TarInfo("lib/actual.so")
        file.size = len(data)
        contents.addfile(file, io.BytesIO(data))
        link = tarfile.TarInfo("lib/alias.so")
        link.type = tarfile.SYMTYPE
        link.linkname = "actual.so"
        contents.addfile(link)
    archive = tmp_path / "safe.tar.zst"
    archive.write_bytes(zstandard.ZstdCompressor().compress(buffer.getvalue()))
    destination = tmp_path / "staging"
    destination.mkdir()
    _extract_zstd_safe(archive, destination, 1024)
    alias = destination / "lib" / "alias.so"
    assert alias.read_bytes() == data and not alias.is_symlink()
