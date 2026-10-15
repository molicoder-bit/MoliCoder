import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.environment.policy import plan_setup
from local_model_chat.environment.windows_installer import install_verified_ollama
from local_model_chat.storage import ChatStore


def test_official_windows_installer_needs_exact_approval_digest_and_signature(tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "installer.sqlite3")
    asset = b"small official fixture"
    original = next(item for item in load_manifests()
                    if item.tool_id == "ollama" and item.system == "Windows")
    manifest = replace(original, size_bytes=len(asset), sha256=hashlib.sha256(asset).hexdigest())
    environment = EnvironmentReport(
        1, "Windows", "x86_64", 4, 16 * 1024**3, 20 * 1024**3,
        "unknown", "fixture", (DiscoveredTool("ollama", None, None, "missing"),), (),
    )
    plan = plan_setup(store, environment, manifest)
    assert plan.estimated_disk_bytes == len(asset) * 4
    root = tmp_path / "managed"
    calls = []

    def download(_url, target, _size, _timeout):
        calls.append("download")
        target.write_bytes(asset)

    def run_installer(_asset, destination, _timeout):
        calls.append("installer")
        destination.mkdir()
        (destination / "ollama.exe").write_bytes(b"fixture")
        return 0

    def version(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="ollama version " + manifest.version, stderr="")

    monkeypatch.setattr("local_model_chat.environment.windows_installer.subprocess.run", version)
    with pytest.raises(PermissionError, match="approval"):
        install_verified_ollama(store, plan, manifest, root, downloader=download,
                                signature_checker=lambda _asset: True,
                                installer_runner=run_installer)
    assert calls == []
    store.record_action_authorization(
        project_id=None, action="setup", scope=plan.scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    approved = plan_setup(store, environment, manifest)
    with pytest.raises(ValueError, match="Authenticode"):
        install_verified_ollama(store, approved, manifest, root, downloader=download,
                                signature_checker=lambda _asset: False,
                                installer_runner=run_installer)
    assert calls == ["download"]
    receipt = install_verified_ollama(
        store, approved, manifest, root, downloader=download,
        signature_checker=lambda _asset: True, installer_runner=run_installer,
    )
    assert calls == ["download", "download", "installer"]
    assert receipt.sha256 == manifest.sha256
    assert (root / f"ollama-{manifest.version}" / "install-receipt.json").is_file()
    with pytest.raises(FileExistsError, match="already exists"):
        install_verified_ollama(store, approved, manifest, root, downloader=download,
                                signature_checker=lambda _asset: True,
                                installer_runner=run_installer)


def test_ollama_installer_rejects_bad_digest_before_signature_or_execution(tmp_path) -> None:
    store = ChatStore(tmp_path / "installer.sqlite3")
    original = next(item for item in load_manifests()
                    if item.tool_id == "ollama" and item.system == "Windows")
    manifest = replace(original, size_bytes=4, sha256="a" * 64)
    environment = EnvironmentReport(
        1, "Windows", "x86_64", 4, 16 * 1024**3, 20 * 1024**3,
        "unknown", "fixture", (DiscoveredTool("ollama", None, None, "missing"),), (),
    )
    plan = plan_setup(store, environment, manifest)
    store.record_action_authorization(
        project_id=None, action="setup", scope=plan.scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )

    def download(_url, target, _size, _timeout):
        target.write_bytes(b"bad!")

    with pytest.raises(ValueError, match="SHA-256"):
        install_verified_ollama(
            store, plan_setup(store, environment, manifest), manifest, tmp_path / "managed",
            downloader=download,
            signature_checker=lambda _asset: pytest.fail("signature checked before digest"),
            installer_runner=lambda *_args: pytest.fail("installer launched before digest"),
        )
