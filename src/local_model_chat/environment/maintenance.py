from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..storage import ChatStore
from .install import InstallReceipt, install_managed_archive
from .manifests import InstallManifest
from .policy import SetupPlan


@dataclass(frozen=True, slots=True)
class MaintenancePlan:
    action: str
    tool_id: str
    version: str
    sha256: str
    scope: str
    risk: ActionRisk


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    tool_id: str
    from_version: str
    to_version: str
    from_sha256: str
    to_sha256: str
    scope: str
    risk: ActionRisk


def _owned(root: Path, tool_id: str, version: str) -> tuple[Path, InstallReceipt]:
    destination = (root / f"{tool_id}-{version}").resolve()
    if not destination.is_relative_to(root) or destination == root:
        raise ValueError("managed path escapes its root")
    receipt_file = destination / "install-receipt.json"
    if not receipt_file.is_file():
        raise FileNotFoundError("managed ownership receipt is missing")
    receipt = InstallReceipt(**json.loads(receipt_file.read_text(encoding="utf-8")))
    executable = Path(receipt.executable).resolve()
    if (receipt.tool_id != tool_id or receipt.version != version
            or Path(receipt.destination).resolve() != destination
            or not executable.is_relative_to(destination)):
        raise ValueError("invalid managed ownership receipt")
    return destination, receipt


def plan_maintenance(
    store: ChatStore, managed_root: Path, action: str, tool_id: str, version: str,
) -> MaintenancePlan:
    if action not in {"repair", "activate", "rollback", "uninstall"}:
        raise ValueError("unsupported managed maintenance action")
    root = managed_root.resolve()
    _, receipt = _owned(root, tool_id, version)
    risk = ActionRisk.DESTRUCTIVE if action == "uninstall" else ActionRisk.MUTATION
    scope = f"maintenance:{action}:{tool_id}@{version}:{receipt.sha256}"
    return MaintenancePlan(action, tool_id, version, receipt.sha256, scope, risk)


def _require_current_plan(store: ChatStore, root: Path, plan: MaintenancePlan) -> tuple[Path, InstallReceipt]:
    expected = plan_maintenance(store, root, plan.action, plan.tool_id, plan.version)
    if plan != expected:
        raise ValueError("maintenance plan does not match the managed receipt")
    AuthorizationPolicy(store).require(ActionRequest(None, "maintenance", plan.scope, plan.risk))
    return _owned(root.resolve(), plan.tool_id, plan.version)


def _state_path(root: Path, tool_id: str) -> Path:
    if not tool_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in tool_id):
        raise ValueError("invalid managed tool name")
    return root / f"{tool_id}-active.json"


def active_version(managed_root: Path, tool_id: str) -> str | None:
    path = _state_path(managed_root.resolve(), tool_id)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return str(value["version"])


def plan_update(
    managed_root: Path, tool_id: str, manifest: InstallManifest,
    *, system: str, architecture: str,
) -> UpdatePlan:
    root = managed_root.resolve()
    current = active_version(root, tool_id)
    if current is None:
        raise ValueError("activate a managed version before updating")
    _, installed = _owned(root, tool_id, current)
    if (manifest.tool_id != tool_id or manifest.system != system
            or manifest.architecture != architecture or manifest.install_kind != "archive"):
        raise ValueError("update manifest is incompatible with this tool or platform")
    if manifest.version == current:
        raise ValueError("target version is already active")
    breaking = manifest.version.split(".", 1)[0] != current.split(".", 1)[0]
    risk = ActionRisk.PRIVILEGED if breaking or manifest.privilege == "administrator" else ActionRisk.MUTATION
    scope = (
        f"maintenance:update:{tool_id}@{current}:{installed.sha256}"
        f"->{manifest.version}:{manifest.sha256}:{system}:{architecture}"
    )
    return UpdatePlan(tool_id, current, manifest.version, installed.sha256, manifest.sha256, scope, risk)


def update_managed(
    store: ChatStore, managed_root: Path, plan: UpdatePlan,
    manifest: InstallManifest, setup_plan: SetupPlan, *, downloader: Callable,
    system: str, architecture: str,
) -> InstallReceipt:
    root = managed_root.resolve()
    expected = plan_update(root, plan.tool_id, manifest, system=system, architecture=architecture)
    if plan != expected:
        raise ValueError("update plan no longer matches active installation")
    AuthorizationPolicy(store).require(ActionRequest(None, "maintenance", plan.scope, plan.risk))
    receipt = install_managed_archive(store, setup_plan, manifest, root, downloader=downloader)
    state_path = _state_path(root, plan.tool_id)
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": receipt.version, "previous_version": plan.from_version}, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(state_path)
    return receipt


def perform_maintenance(
    store: ChatStore, managed_root: Path, plan: MaintenancePlan, *,
    manifest: InstallManifest | None = None,
    setup_plan: SetupPlan | None = None,
    downloader: Callable | None = None,
) -> InstallReceipt | None:
    root = managed_root.resolve()
    destination, receipt = _require_current_plan(store, root, plan)
    state_path = _state_path(root, plan.tool_id)
    if plan.action in {"activate", "rollback"}:
        if not Path(receipt.executable).is_file():
            raise FileNotFoundError("managed executable is missing; repair first")
        if plan.action == "rollback":
            if not state_path.is_file():
                raise ValueError("no active managed version to roll back")
            current_state = json.loads(state_path.read_text(encoding="utf-8"))
            if current_state.get("previous_version") != plan.version:
                raise ValueError("target is not the previous managed version")
        previous = active_version(root, plan.tool_id)
        state = {"version": plan.version, "previous_version": previous}
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        temporary.replace(state_path)
        return receipt
    if plan.action == "repair":
        if manifest is None or setup_plan is None or downloader is None:
            raise ValueError("repair needs a pinned manifest, setup approval, and downloader")
        if (manifest.tool_id, manifest.version, manifest.sha256) != (
            receipt.tool_id, receipt.version, receipt.sha256,
        ):
            raise ValueError("repair manifest differs from installed receipt")
        quarantine = root / f"{plan.tool_id}-{plan.version}.repair-backup"
        if quarantine.exists():
            raise FileExistsError("repair backup already exists")
        destination.rename(quarantine)
        try:
            repaired = install_managed_archive(
                store, setup_plan, manifest, root, downloader=downloader,
            )
        except Exception:
            if destination.exists():
                raise RuntimeError("repair failed with a partial replacement; backup retained")
            quarantine.rename(destination)
            raise
        return repaired
    if active_version(root, plan.tool_id) == plan.version:
        raise ValueError("activate another managed version before uninstall")
    quarantine = root / f"{plan.tool_id}-{plan.version}.uninstalled"
    if quarantine.exists():
        raise FileExistsError("uninstall quarantine already exists")
    destination.rename(quarantine)
    return None
