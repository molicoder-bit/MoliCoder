from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..environment.discovery import EnvironmentReport
from ..environment.install import InstallReceipt, install_managed_archive
from ..environment.maintenance import active_version, perform_maintenance, plan_maintenance
from ..environment.manifests import InstallManifest, load_manifests
from ..environment.policy import SetupPlan, plan_setup
from ..storage import ChatStore


@dataclass(frozen=True, slots=True)
class ManagedKandevPlan:
    managed_root: str
    version: str
    sha256: str
    system: str
    architecture: str
    scope: str
    setup: SetupPlan


@dataclass(frozen=True, slots=True)
class KandevDataBackup:
    backup_id: str
    managed_root: str
    version: str
    tree_sha256: str
    backup_path: str
    created_at: str


def _pinned(manifest: InstallManifest) -> None:
    if manifest.tool_id != "kandev" or manifest.install_kind != "archive":
        raise ValueError("a pinned Kandev archive manifest is required")
    if manifest not in load_manifests():
        raise ValueError("Kandev release is not in the verified manifest catalog")


def plan_managed_kandev(
    store: ChatStore, environment: EnvironmentReport, manifest: InstallManifest, managed_root: Path,
) -> ManagedKandevPlan:
    _pinned(manifest)
    if manifest.system != environment.system or manifest.architecture != environment.architecture:
        raise ValueError("Kandev release is unsupported on this platform")
    setup = plan_setup(store, environment, manifest)
    if setup.action != "install_managed":
        raise ValueError("existing Kandev installation must be attached or reviewed separately")
    root = managed_root.resolve()
    scope = f"kandev:managed:{root}:{manifest.version}:{manifest.sha256}:{manifest.system}:{manifest.architecture}"
    return ManagedKandevPlan(str(root), manifest.version, manifest.sha256,
                             manifest.system, manifest.architecture, scope, setup)


def _data_root(root: Path) -> Path:
    path = (root / "kandev-data").resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("managed Kandev data path escapes its root")
    return path


def _require_owned_data(root: Path) -> Path:
    data = _data_root(root)
    marker = data / "molicoder-owner.json"
    if not marker.is_file():
        raise FileNotFoundError("MoliCoder-owned Kandev data receipt is missing")
    receipt = json.loads(marker.read_text(encoding="utf-8"))
    if receipt.get("managed_root") != str(root) or receipt.get("data_root") != str(data):
        raise ValueError("Kandev data ownership receipt does not match this root")
    return data


def _tree_hash(root: Path, *, ignore_database: bool = False) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("managed Kandev data contains a symbolic link")
        if path.is_file():
            relative = path.relative_to(root).as_posix().encode("utf-8")
            if ignore_database and relative in {
                b"data/kandev.db", b"data/kandev.db-wal", b"data/kandev.db-shm",
            }:
                continue
            digest.update(len(relative).to_bytes(4, "big") + relative)
            with path.open("rb") as source:
                while block := source.read(1024 * 1024):
                    digest.update(block)
    return digest.hexdigest()


def install_managed_kandev(
    store: ChatStore, plan: ManagedKandevPlan, manifest: InstallManifest,
    *, downloader: Callable,
) -> InstallReceipt:
    _pinned(manifest)
    root = Path(plan.managed_root).resolve()
    if (plan.version, plan.sha256, plan.system, plan.architecture) != (
        manifest.version, manifest.sha256, manifest.system, manifest.architecture
    ):
        raise ValueError("managed Kandev plan differs from the pinned release")
    expected_scope = f"kandev:managed:{root}:{manifest.version}:{manifest.sha256}:{manifest.system}:{manifest.architecture}"
    if plan.scope != expected_scope:
        raise ValueError("managed Kandev scope does not match the destination")
    AuthorizationPolicy(store).require(ActionRequest(None, "kandev_managed_setup", plan.scope, ActionRisk.MUTATION))
    data = _data_root(root)
    if data.exists():
        _require_owned_data(root)
    receipt = install_managed_archive(store, plan.setup, manifest, root, downloader=downloader)
    if not data.exists():
        data.mkdir()
        (data / "config.yaml").write_text("server:\n  host: 127.0.0.1\n", encoding="utf-8")
        (data / "molicoder-owner.json").write_text(json.dumps({
            "managed_root": str(root), "data_root": str(data),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, sort_keys=True), encoding="utf-8")
    return receipt


def backup_managed_kandev_data(store: ChatStore, managed_root: Path) -> KandevDataBackup:
    root = managed_root.resolve()
    data = _require_owned_data(root)
    version = active_version(root, "kandev")
    if version is None:
        raise ValueError("activate a managed Kandev version before backing up data")
    source_sha = _tree_hash(data, ignore_database=True)
    tree_sha = _tree_hash(data)
    scope = f"kandev:backup:{root}:{version}:{tree_sha}"
    AuthorizationPolicy(store).require(ActionRequest(None, "kandev_backup", scope, ActionRisk.MUTATION))
    backup_id = uuid4().hex
    backup_root = (root / "kandev-backups").resolve()
    if not backup_root.is_relative_to(root) or backup_root == root:
        raise ValueError("Kandev backup path escapes managed root")
    backup_root.mkdir(exist_ok=True)
    destination = backup_root / backup_id
    temporary = Path(tempfile.mkdtemp(prefix="backup-", dir=backup_root))
    try:
        database = data / "data" / "kandev.db"
        def ignore_database(folder: str, names: list[str]) -> set[str]:
            return {"kandev.db", "kandev.db-wal", "kandev.db-shm"} & set(names) if Path(folder) == database.parent else set()

        shutil.copytree(data, temporary / "data", ignore=ignore_database)
        if database.is_file():
            target_db = temporary / "data" / "data" / "kandev.db"
            target_db.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as source_db:
                with closing(sqlite3.connect(target_db)) as backup_db:
                    source_db.backup(backup_db)
        if _tree_hash(data, ignore_database=True) != source_sha:
            raise ValueError("Kandev data changed during backup")
        tree_sha = _tree_hash(temporary / "data")
        receipt = KandevDataBackup(
            backup_id, str(root), version, tree_sha, str(destination),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        (temporary / "backup-receipt.json").write_text(json.dumps(asdict(receipt), sort_keys=True), encoding="utf-8")
        temporary.rename(destination)
        return receipt
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def rollback_managed_kandev(
    store: ChatStore, managed_root: Path, backup: KandevDataBackup,
) -> InstallReceipt:
    root = managed_root.resolve()
    data = _require_owned_data(root)
    backup_root = (root / "kandev-backups").resolve()
    if not backup_root.is_relative_to(root) or backup_root == root:
        raise ValueError("Kandev backup path escapes managed root")
    backup_dir = (backup_root / backup.backup_id).resolve()
    if (backup.managed_root != str(root) or backup.backup_path != str(backup_dir)
            or not backup_dir.is_relative_to(backup_root) or backup_dir == backup_root
            or not backup_dir.is_dir()):
        raise ValueError("Kandev backup is outside the managed root")
    stored = KandevDataBackup(**json.loads((backup_dir / "backup-receipt.json").read_text(encoding="utf-8")))
    if stored != backup or _tree_hash(backup_dir / "data") != backup.tree_sha256:
        raise ValueError("Kandev backup receipt or data hash differs")
    scope = f"kandev:restore:{root}:{backup.backup_id}:{backup.version}:{backup.tree_sha256}"
    AuthorizationPolicy(store).require(ActionRequest(None, "kandev_restore", scope, ActionRisk.DESTRUCTIVE))
    maintenance = plan_maintenance(store, root, "rollback", "kandev", backup.version)
    displaced = root / f"kandev-data.before-rollback-{backup.backup_id}"
    if displaced.exists():
        raise FileExistsError("previous rollback data is already retained")
    staged = root / f"kandev-data.restore-{backup.backup_id}"
    if staged.exists():
        raise FileExistsError("staged rollback data already exists")
    shutil.copytree(backup_dir / "data", staged)
    try:
        data.rename(displaced)
        try:
            staged.rename(data)
            try:
                return perform_maintenance(store, root, maintenance)
            except Exception:
                data.rename(staged)
                raise
        finally:
            if not data.exists() and displaced.exists():
                displaced.rename(data)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
