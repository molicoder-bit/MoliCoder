import hashlib
import io
import tarfile
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
from local_model_chat.environment.install import install_managed_archive
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.environment.policy import plan_setup
from local_model_chat.storage import ChatStore


def _archive(path, *, name="package/kandev.exe") -> bytes:
    with tarfile.open(path, "w:gz") as archive:
        data = b"fake executable\n"
        info = tarfile.TarInfo(name)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return path.read_bytes()


def test_pinned_zip_install_extracts_only_safe_managed_files(tmp_path) -> None:
    archive_path = tmp_path / "opencode.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("package/opencode.exe", b"fixture executable")
    asset = archive_path.read_bytes()
    store = ChatStore(tmp_path / "zip-install.sqlite3")
    base = next(item for item in load_manifests()
                if item.tool_id == "opencode" and item.system == "Windows")
    manifest = replace(base, sha256=hashlib.sha256(asset).hexdigest(), size_bytes=len(asset))
    inventory = EnvironmentReport(
        1, "Windows", "x86_64", 8, 16 * 1024**3, 100 * 1024**3,
        "unknown", "fixture", (DiscoveredTool("opencode", None, None, "missing"),), (),
    )
    plan = plan_setup(store, inventory, manifest)
    store.record_action_authorization(
        project_id=None, action="setup", scope=plan.scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    receipt = install_managed_archive(
        store, plan, manifest, tmp_path / "managed",
        downloader=lambda _url, target, _size, _timeout: target.write_bytes(asset),
    )
    assert (tmp_path / "managed" / "opencode-1.19.0" / "package" / "opencode.exe").is_file()
    assert receipt.sha256 == manifest.sha256


def test_zip_extraction_rejects_path_escape(tmp_path) -> None:
    from local_model_chat.environment.install import _extract_zip_safe

    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.txt", b"bad")
    target = tmp_path / "staging"
    target.mkdir()
    with pytest.raises(ValueError, match="unsafe"):
        _extract_zip_safe(archive_path, target, 100)
    assert not (tmp_path / "outside.txt").exists()


def _approved_plan(tmp_path, asset: bytes):
    store = ChatStore(tmp_path / "install.sqlite3")
    base = next(item for item in load_manifests() if item.tool_id == "kandev" and item.system == "Windows")
    manifest = replace(base, sha256=hashlib.sha256(asset).hexdigest(), size_bytes=len(asset))
    inventory = EnvironmentReport(
        1, "Windows", "x86_64", 8, 16 * 1024**3, 100 * 1024**3,
        "unknown", "fixture", (DiscoveredTool("kandev", None, None, "missing"),), (),
    )
    plan = plan_setup(store, inventory, manifest)
    store.record_action_authorization(
        project_id=None, action="setup", scope=plan.scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    return store, manifest, plan


def test_verified_archive_install_is_idempotent_and_receipted(tmp_path) -> None:
    asset = _archive(tmp_path / "fixture.tar.gz")
    store, manifest, plan = _approved_plan(tmp_path, asset)
    root = tmp_path / "managed"
    calls = []

    def downloader(_url, target, _size, _timeout):
        calls.append(1)
        target.write_bytes(asset)

    first = install_managed_archive(store, plan, manifest, root, downloader=downloader)
    second = install_managed_archive(store, plan, manifest, root, downloader=downloader)

    assert first == second
    assert len(calls) == 1
    assert (root / "kandev-0.97.0" / "install-receipt.json").is_file()


def test_checksum_failure_leaves_no_managed_install(tmp_path) -> None:
    asset = _archive(tmp_path / "fixture.tar.gz")
    store, manifest, plan = _approved_plan(tmp_path, asset)
    root = tmp_path / "managed"

    with pytest.raises(ValueError, match="SHA-256"):
        install_managed_archive(
            store, plan, manifest, root,
            downloader=lambda _url, target, _size, _timeout: target.write_bytes(b"tampered"),
        )
    assert not (root / "kandev-0.97.0").exists()
    assert not list(root.glob("install-*"))


def test_archive_traversal_is_rejected(tmp_path) -> None:
    asset = _archive(tmp_path / "unsafe.tar.gz", name="../escape.txt")
    store, manifest, plan = _approved_plan(tmp_path, asset)
    root = tmp_path / "managed"

    with pytest.raises(ValueError, match="unsafe path"):
        install_managed_archive(
            store, plan, manifest, root,
            downloader=lambda _url, target, _size, _timeout: target.write_bytes(asset),
        )
    assert not (tmp_path / "escape.txt").exists()


def test_forged_approved_scope_cannot_install_other_manifest(tmp_path) -> None:
    asset = _archive(tmp_path / "fixture.tar.gz")
    store, manifest, plan = _approved_plan(tmp_path, asset)
    forged = replace(plan, scope="setup:unrelated-approved-action")
    store.record_action_authorization(
        project_id=None, action="setup", scope=forged.scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    with pytest.raises(ValueError, match="scope"):
        install_managed_archive(store, forged, manifest, tmp_path / "managed")
