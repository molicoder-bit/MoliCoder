import sqlite3

import pytest

from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore


def test_same_model_has_distinct_provider_offerings_and_account_refs(tmp_path) -> None:
    db = tmp_path / "catalog.sqlite3"
    store = ChatStore(db)
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    store.set_project_preferences(project.id, provider="codex", strategy="HYBRID")
    registry = ProviderRegistry(store)
    registry.add_provider("p1", "Provider one", "https://one.example")
    registry.add_provider("p2", "Provider two", "https://two.example")
    registry.add_model("model-v1", "model", "v1")
    registry.add_account("account-1", "p1", "personal", secret_ref="os-keychain:one")
    registry.add_offering("one-model", "p1", "model-v1", "one-route", "metered", account_id="account-1")
    registry.add_offering("two-model", "p2", "model-v1", "two-route", "recurring_free")

    first = registry.get_offering("one-model")
    second = ProviderRegistry(ChatStore(db)).get_offering("two-model")

    assert first.model_id == second.model_id == "model-v1"
    assert first.provider_id != second.provider_id
    assert first.offering_class == "metered" and second.offering_class == "recurring_free"
    assert first.input_usd_per_million is None
    assert first.account_id == "account-1" and second.account_id is None
    account_statuses = registry.list_account_statuses()
    assert [(item.id, item.provider_id, item.auth_status, item.credential_present)
            for item in account_statuses] == [("account-1", "p1", "unknown", True)]
    assert "os-keychain:one" not in repr(account_statuses)
    assert store.get_project_preferences(project.id).provider == "codex"
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == ChatStore.SCHEMA_VERSION
        assert connection.execute("SELECT secret_ref FROM inference_accounts WHERE id='account-1'").fetchone()[0] == "os-keychain:one"
    with pytest.raises(ValueError, match="opaque secret reference"):
        registry.add_account("bad", "p1", "personal", secret_ref="sk-plaintext")


def test_version_four_database_upgrades_with_catalog_backup(tmp_path) -> None:
    db = tmp_path / "v4.sqlite3"
    store = ChatStore(db)
    conversation = store.create_conversation("local-test")
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA user_version = 4")
        connection.execute("DROP TABLE inference_offering_events")
        connection.execute("DROP TABLE inference_offerings")
        connection.execute("DROP TABLE inference_accounts")
        connection.execute("DROP TABLE inference_models")
        connection.execute("DROP TABLE inference_providers")

    reopened = ChatStore(db)

    assert reopened.get_conversation(conversation.id) is not None
    assert len(list(tmp_path.glob("v4.sqlite3.pre-v11-*.bak"))) == 1
