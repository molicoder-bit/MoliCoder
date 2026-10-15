from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.inference.catalog import CatalogRecord, import_catalog_record
from local_model_chat.environment.security import platform_security
from local_model_chat.inference import eligibility
from local_model_chat.inference.eligibility import EligibilityRequest
from local_model_chat.inference.health import deprecate_offering, enable_offering, record_health
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore


@pytest.fixture(autouse=True)
def _metered_fixture_has_verified_windows_backend(monkeypatch):
    monkeypatch.setattr(eligibility, "platform_security", lambda: platform_security("Windows"))


def test_catalog_and_health_do_not_auto_enable_paid_route(tmp_path) -> None:
    store = ChatStore(tmp_path / "health.sqlite3")
    registry = ProviderRegistry(store)
    registry.add_provider("p", "Provider", "https://provider.example")
    registry.add_model("m", "model", "v1")
    registry.add_offering("route", "p", "m", "direct", "metered")
    now = datetime.now(timezone.utc).isoformat()
    import_catalog_record(store, CatalogRecord(
        "route", "https://provider.example/pricing", now,
        "https://provider.example/terms", {"stream": True}, "private", True,
        0.1, 0.2, {},
    ))
    record_health(registry, "route", "healthy", actor="doctor", reason="endpoint status read")
    assert registry.get_offering("route").status == "discovered"
    request = EligibilityRequest("private", ("stream",), unattended=True, paid_allowed=True)
    with pytest.raises(PermissionError):
        enable_offering(registry, "route", request, project_id=None, actor="user")

    store.record_action_authorization(
        project_id=None, action="enable_offering", scope="route", risk="financial",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    enable_offering(registry, "route", request, project_id=None, actor="user")
    assert registry.get_offering("route").status == "eligible"
    deprecate_offering(registry, "route", actor="catalog", reason="retired upstream")
    assert registry.get_offering("route").status == "deprecated"
    with pytest.raises(ValueError, match="deprecated"):
        enable_offering(registry, "route", request, project_id=None, actor="user")
    events = registry.list_events("route")
    assert [item["kind"] for item in events] == [
        "discovered", "catalog_refresh", "state_change", "state_change", "state_change",
    ]
