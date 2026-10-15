from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.environment.policy import plan_setup
from local_model_chat.storage import ChatStore


def _environment(installed: bool = False) -> EnvironmentReport:
    return EnvironmentReport(
        1, "Windows", "x86_64", 8, 16 * 1024**3, 100 * 1024**3,
        "unknown", "fixture",
        (DiscoveredTool("kandev", "C:/Tools/kandev.exe" if installed else None,
                        "0.90.0" if installed else None,
                        "installed" if installed else "missing"),),
        (),
    )


def test_dry_run_reports_exact_asset_scope_and_waits_for_authorization(tmp_path) -> None:
    store = ChatStore(tmp_path / "setup.sqlite3")
    manifest = next(item for item in load_manifests() if item.tool_id == "kandev" and item.system == "Windows")
    plan = plan_setup(store, _environment(), manifest)

    assert plan.action == "install_managed"
    assert plan.download_bytes == manifest.size_bytes
    assert plan.estimated_disk_bytes >= plan.download_bytes
    assert plan.sha256 == manifest.sha256
    assert not plan.authorized
    assert "approval" in plan.authorization_reason
    assert not list(tmp_path.glob("*.tar.gz"))

    store.record_action_authorization(
        project_id=None, action="setup", scope=plan.scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    assert plan_setup(store, _environment(), manifest).authorized


def test_existing_user_owned_install_is_never_scheduled_for_overwrite(tmp_path) -> None:
    store = ChatStore(tmp_path / "setup.sqlite3")
    manifest = next(item for item in load_manifests() if item.tool_id == "kandev" and item.system == "Windows")
    plan = plan_setup(store, _environment(installed=True), manifest)
    assert plan.action == "review_existing"
    assert plan.download_bytes == 0
    assert "do not overwrite" in plan.steps[0]


def test_dry_run_rejects_wrong_platform_manifest(tmp_path) -> None:
    store = ChatStore(tmp_path / "setup.sqlite3")
    manifest = next(item for item in load_manifests() if item.tool_id == "kandev" and item.system == "Linux")
    with pytest.raises(ValueError, match="OS and architecture"):
        plan_setup(store, _environment(), manifest)
