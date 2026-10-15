from datetime import datetime, timezone

import pytest

from local_model_chat.inference.catalog import CatalogRecord, import_catalog_record
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore


def _catalog(store: ChatStore) -> ProviderRegistry:
    registry = ProviderRegistry(store)
    registry.add_provider("provider", "Provider", "https://provider.example")
    registry.add_model("model-v1", "model", "v1")
    registry.add_offering("route", "provider", "model-v1", "hosted", "metered")
    return registry


def test_catalog_import_keeps_unknown_price_disabled_and_preserves_history(tmp_path) -> None:
    store = ChatStore(tmp_path / "catalog.sqlite3")
    registry = _catalog(store)
    checked = datetime.now(timezone.utc).isoformat()
    first = CatalogRecord(
        "route", "https://provider.example/models", checked, "",
        {"tools": True, "stream": True}, "unknown", None, None, None, {},
    )
    import_catalog_record(store, first)
    offering = registry.get_offering("route")
    assert offering.status == "discovered"
    assert offering.input_usd_per_million is None
    assert offering.price_checked_at is None
    assert offering.terms_checked_at is None

    updated = CatalogRecord(
        "route", "https://provider.example/pricing", checked,
        "https://provider.example/terms", {"tools": True, "stream": True},
        "private", True, 0.2, 0.5, {"limit": "account-specific"},
    )
    import_catalog_record(store, updated, manual_override=True, actor="user", reason="verified account rate")
    changed = registry.get_offering("route")
    assert changed.input_usd_per_million == 0.2
    assert changed.output_usd_per_million == 0.5
    assert changed.status == "discovered"
    events = registry.list_events("route")
    assert [item["kind"] for item in events] == ["discovered", "catalog_refresh", "manual_override"]
    with pytest.raises(ValueError, match="actor and reason"):
        import_catalog_record(store, updated, manual_override=True)
