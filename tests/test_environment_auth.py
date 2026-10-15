import platform

import pytest

from local_model_chat.environment.auth import ProtectedCredentialStore, store_provider_key
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows DPAPI backend")
def test_dpapi_keeps_dummy_key_out_of_sqlite_and_disk_plaintext(tmp_path) -> None:
    store = ChatStore(tmp_path / "accounts.sqlite3")
    registry = ProviderRegistry(store)
    registry.add_provider("test-provider", "Test Provider", "https://provider.example")
    protected = ProtectedCredentialStore(tmp_path / "credentials")
    dummy_key = "dummy-test-key-no-real-account-123456"
    reference = protected.put("test-account", dummy_key)
    registry.add_account("test-account", "test-provider", "user", secret_ref=reference)

    files = list((tmp_path / "credentials").glob("*.dpapi"))
    assert len(files) == 1
    assert dummy_key.encode() not in files[0].read_bytes()
    assert dummy_key.encode() not in (tmp_path / "accounts.sqlite3").read_bytes()
    assert protected.get(reference) == dummy_key
    protected.delete(reference)
    assert not files[0].exists()


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows DPAPI backend")
def test_registered_provider_key_onboarding_keeps_route_inactive(tmp_path) -> None:
    store = ChatStore(tmp_path / "accounts.sqlite3")
    registry = ProviderRegistry(store)
    registry.add_provider("fixture-api", "Fixture API", "https://provider.example")
    protected = ProtectedCredentialStore(tmp_path / "credentials")
    dummy_key = "dummy-key-for-fixture-provider"
    reference = store_provider_key(registry, protected, "fixture-api", dummy_key)
    assert protected.get(reference) == dummy_key
    assert registry.get_account_status("manual-key:fixture-api") == "unknown"
    assert registry.list_offerings() == []
    assert dummy_key.encode() not in (tmp_path / "accounts.sqlite3").read_bytes()
    with pytest.raises(ValueError, match="not registered"):
        store_provider_key(registry, protected, "unregistered", dummy_key)
