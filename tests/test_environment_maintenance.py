import hashlib
import io
import tarfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
from local_model_chat.environment.install import install_managed_archive
from local_model_chat.environment.maintenance import (
    active_version, perform_maintenance, plan_maintenance, plan_update, update_managed,
)
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.environment.policy import plan_setup
from local_model_chat.storage import ChatStore


def _asset(path):
    with tarfile.open(path, "w:gz") as archive:
        data = b"fake executable\n"
        info = tarfile.TarInfo("package/kandev.exe")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return path.read_bytes()


def _approve(store, action, scope, risk):
    store.record_action_authorization(
        project_id=None, action=action, scope=scope, risk=risk,
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )


def _install(tmp_path, store, root, version):
    asset = _asset(tmp_path / f"{version}.tar.gz")
    base = next(item for item in load_manifests() if item.tool_id == "kandev" and item.system == "Windows")
    manifest = replace(base, version=version, source_url=base.source_url.replace("v0.97.0", f"v{version}"),
                       sha256=hashlib.sha256(asset).hexdigest(), size_bytes=len(asset))
    inventory = EnvironmentReport(
        1, "Windows", "x86_64", 8, 16 * 1024**3, 100 * 1024**3,
        "unknown", "fixture", (DiscoveredTool("kandev", None, None, "missing"),), (),
    )
    setup = plan_setup(store, inventory, manifest)
    _approve(store, "setup", setup.scope, "mutation")
    receipt = install_managed_archive(
        store, setup, manifest, root,
        downloader=lambda _url, target, _size, _timeout: target.write_bytes(asset),
    )
    return asset, manifest, setup, receipt


def test_version_activation_rollback_and_preserving_uninstall(tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    root = tmp_path / "managed"
    _install(tmp_path, store, root, "0.97.0")
    _install(tmp_path, store, root, "0.98.0")
    for action, version in (("activate", "0.97.0"), ("activate", "0.98.0"),
                            ("rollback", "0.97.0")):
        plan = plan_maintenance(store, root, action, "kandev", version)
        _approve(store, "maintenance", plan.scope, plan.risk.value)
        perform_maintenance(store, root, plan)
        assert active_version(root, "kandev") == version
    (root / "kandev-0.98.0" / "user-data.txt").write_text("preserve me", encoding="utf-8")
    plan = plan_maintenance(store, root, "uninstall", "kandev", "0.98.0")
    with pytest.raises(PermissionError):
        perform_maintenance(store, root, plan)
    _approve(store, "maintenance", plan.scope, plan.risk.value)
    perform_maintenance(store, root, plan)
    assert (root / "kandev-0.98.0.uninstalled" / "user-data.txt").read_text() == "preserve me"


def test_repair_restores_original_on_bad_download(tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    root = tmp_path / "managed"
    _, manifest, setup, receipt = _install(tmp_path, store, root, "0.97.0")
    plan = plan_maintenance(store, root, "repair", "kandev", "0.97.0")
    _approve(store, "maintenance", plan.scope, plan.risk.value)
    with pytest.raises(ValueError, match="SHA-256"):
        perform_maintenance(
            store, root, plan, manifest=manifest, setup_plan=setup,
            downloader=lambda _url, target, _size, _timeout: target.write_bytes(b"bad"),
        )
    assert (root / "kandev-0.97.0" / "install-receipt.json").is_file()
    assert not (root / "kandev-0.97.0.repair-backup").exists()


def test_update_requires_exact_approval_and_preserves_previous_version(tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    root = tmp_path / "managed"
    _install(tmp_path, store, root, "0.97.0")
    activate = plan_maintenance(store, root, "activate", "kandev", "0.97.0")
    _approve(store, "maintenance", activate.scope, activate.risk.value)
    perform_maintenance(store, root, activate)
    asset, manifest, setup, _ = _install(tmp_path, store, root, "0.98.0")
    plan = plan_update(root, "kandev", manifest, system="Windows", architecture="x86_64")
    with pytest.raises(PermissionError):
        update_managed(
            store, root, plan, manifest, setup,
            downloader=lambda _url, target, _size, _timeout: target.write_bytes(asset),
            system="Windows", architecture="x86_64",
        )
    _approve(store, "maintenance", plan.scope, plan.risk.value)
    update_managed(
        store, root, plan, manifest, setup,
        downloader=lambda _url, target, _size, _timeout: target.write_bytes(asset),
        system="Windows", architecture="x86_64",
    )
    assert active_version(root, "kandev") == "0.98.0"
    assert (root / "kandev-0.97.0" / "install-receipt.json").is_file()
    with pytest.raises(ValueError, match="incompatible"):
        plan_update(root, "kandev", manifest, system="Linux", architecture="x86_64")
