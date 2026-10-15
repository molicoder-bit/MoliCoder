import hashlib
import io
import json
import sqlite3
from contextlib import closing
import tarfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
from local_model_chat.environment.maintenance import active_version, perform_maintenance, plan_maintenance
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.kandev.manager import (
    _tree_hash, backup_managed_kandev_data, install_managed_kandev, plan_managed_kandev,
    rollback_managed_kandev,
)
from local_model_chat.storage import ChatStore


def _approve(store, action, scope, risk):
    store.record_action_authorization(
        project_id=None, action=action, scope=scope, risk=risk,
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )


def _fixture_release(tmp_path, version):
    archive = tmp_path / f"kandev-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        content = f"fixture {version}".encode()
        member = tarfile.TarInfo("package/kandev.exe")
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    asset = archive.read_bytes()
    base = next(item for item in load_manifests() if item.tool_id == "kandev" and item.system == "Windows")
    manifest = replace(base, version=version, source_url=base.source_url.replace("v0.97.0", f"v{version}"),
                       sha256=hashlib.sha256(asset).hexdigest(), size_bytes=len(asset))
    return manifest, asset


def _environment():
    return EnvironmentReport(
        1, "Windows", "x86_64", 8, 16 * 1024**3, 100 * 1024**3,
        "unknown", "fixture", (DiscoveredTool("kandev", None, None, "missing"),), (),
    )


def _install(store, root, environment, manifest, asset):
    plan = plan_managed_kandev(store, environment, manifest, root)
    _approve(store, "kandev_managed_setup", plan.scope, "mutation")
    _approve(store, "setup", plan.setup.scope, "mutation")
    return install_managed_kandev(
        store, plan, manifest,
        downloader=lambda _url, target, _size, _timeout: target.write_bytes(asset),
    )


def test_managed_install_backup_and_rollback_keep_data_owned_and_local(tmp_path, monkeypatch):
    first, first_asset = _fixture_release(tmp_path, "0.97.0")
    second, second_asset = _fixture_release(tmp_path, "0.98.0")
    monkeypatch.setattr("local_model_chat.kandev.manager.load_manifests", lambda: (first, second))
    store = ChatStore(tmp_path / "state.sqlite3")
    root = tmp_path / "managed"
    receipt = _install(store, root, _environment(), first, first_asset)
    assert receipt.executable.endswith("kandev.exe")
    data = root / "kandev-data"
    assert (data / "config.yaml").read_text() == "server:\n  host: 127.0.0.1\n"
    assert json.loads((data / "molicoder-owner.json").read_text())["managed_root"] == str(root.resolve())

    activate = plan_maintenance(store, root, "activate", "kandev", first.version)
    _approve(store, "maintenance", activate.scope, activate.risk.value)
    perform_maintenance(store, root, activate)
    (data / "task.txt").write_text("before update", encoding="utf-8")
    database = data / "data" / "kandev.db"
    database.parent.mkdir()
    active_db = sqlite3.connect(database)
    active_db.execute("PRAGMA journal_mode=WAL")
    active_db.execute("CREATE TABLE items(value TEXT)")
    active_db.execute("INSERT INTO items VALUES ('before update')")
    active_db.commit()
    tree_hash = _tree_hash(data)
    _approve(store, "kandev_backup", f"kandev:backup:{root.resolve()}:{first.version}:{tree_hash}", "mutation")
    backup = backup_managed_kandev_data(store, root)
    with closing(sqlite3.connect(root / "kandev-backups" / backup.backup_id / "data" / "data" / "kandev.db")) as snapshot_db:
        assert snapshot_db.execute("SELECT value FROM items").fetchone() == ("before update",)
    active_db.close()
    snapshot_file = root / "kandev-backups" / backup.backup_id / "data" / "task.txt"
    snapshot_file.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash differs"):
        rollback_managed_kandev(store, root, backup)
    snapshot_file.write_text("before update", encoding="utf-8")

    _install(store, root, _environment(), second, second_asset)
    activate = plan_maintenance(store, root, "activate", "kandev", second.version)
    _approve(store, "maintenance", activate.scope, activate.risk.value)
    perform_maintenance(store, root, activate)
    (data / "task.txt").write_text("after update", encoding="utf-8")
    restore_scope = f"kandev:restore:{root.resolve()}:{backup.backup_id}:{backup.version}:{backup.tree_sha256}"
    _approve(store, "kandev_restore", restore_scope, "destructive")
    rollback = plan_maintenance(store, root, "rollback", "kandev", first.version)
    _approve(store, "maintenance", rollback.scope, rollback.risk.value)
    rollback_managed_kandev(store, root, backup)

    assert active_version(root, "kandev") == first.version
    assert (data / "task.txt").read_text() == "before update"
    assert (root / f"kandev-data.before-rollback-{backup.backup_id}" / "task.txt").read_text() == "after update"


def test_managed_install_rejects_unsupported_platform_and_unapproved_setup(tmp_path, monkeypatch):
    manifest, asset = _fixture_release(tmp_path, "0.97.0")
    monkeypatch.setattr("local_model_chat.kandev.manager.load_manifests", lambda: (manifest,))
    store = ChatStore(tmp_path / "state.sqlite3")
    root = tmp_path / "managed"
    linux = replace(_environment(), system="Linux")
    with pytest.raises(ValueError, match="unsupported"):
        plan_managed_kandev(store, linux, manifest, root)
    plan = plan_managed_kandev(store, _environment(), manifest, root)
    with pytest.raises(PermissionError):
        install_managed_kandev(store, plan, manifest,
                               downloader=lambda _url, target, _size, _timeout: target.write_bytes(asset))
    assert not root.exists()


def test_backup_rejects_unowned_data(tmp_path, monkeypatch):
    manifest, asset = _fixture_release(tmp_path, "0.97.0")
    monkeypatch.setattr("local_model_chat.kandev.manager.load_manifests", lambda: (manifest,))
    store = ChatStore(tmp_path / "state.sqlite3")
    root = tmp_path / "managed"
    root.mkdir()
    (root / "kandev-data").mkdir()
    with pytest.raises(FileNotFoundError, match="receipt is missing"):
        backup_managed_kandev_data(store, root)
    assert not list(root.glob("kandev-backups/*"))
