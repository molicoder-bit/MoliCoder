from __future__ import annotations

from dataclasses import dataclass

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..storage import ChatStore
from .discovery import EnvironmentReport
from .manifests import InstallManifest


@dataclass(frozen=True, slots=True)
class SetupPlan:
    tool_id: str
    version: str
    action: str
    scope: str
    source_url: str | None
    sha256: str | None
    download_bytes: int
    estimated_disk_bytes: int
    privilege: str
    authorized: bool
    authorization_reason: str
    pending_auth: str | None
    steps: tuple[str, ...]


def plan_setup(store: ChatStore, environment: EnvironmentReport, manifest: InstallManifest) -> SetupPlan:
    if manifest.system != environment.system or manifest.architecture != environment.architecture:
        raise ValueError("manifest does not match this OS and architecture")
    tool = next((item for item in environment.tools if item.name == manifest.tool_id), None)
    if tool is None:
        raise ValueError("tool is absent from the environment inventory")
    existing = tool.state == "installed"
    action = "review_existing" if existing else "install_managed"
    scope = f"{action}:{manifest.tool_id}@{manifest.version}:{manifest.system}:{manifest.architecture}:{manifest.sha256}"
    risk = ActionRisk.PRIVILEGED if manifest.privilege == "administrator" else ActionRisk.MUTATION
    approval = AuthorizationPolicy(store).check(ActionRequest(None, "setup", scope, risk))
    return SetupPlan(
        manifest.tool_id, manifest.version, action, scope,
        None if existing else manifest.source_url,
        None if existing else manifest.sha256,
        0 if existing else manifest.size_bytes,
        0 if existing else manifest.size_bytes * 2,
        manifest.privilege, approval.allowed, approval.reason,
        "Administrator permission required" if manifest.privilege == "administrator" and not existing else None,
        (
            "Inspect the existing user-owned installation; do not overwrite it.",
            "Choose attached or managed ownership explicitly before any change.",
        ) if existing else (
            "Download exact official release asset to a temporary location.",
            f"Verify SHA-256 {manifest.sha256} and {manifest.signature_requirement}.",
            "Back up only MoliCoder-owned prior data and configuration.",
            "Install only into a MoliCoder-managed location after authorization.",
            manifest.rollback,
        ),
    )
