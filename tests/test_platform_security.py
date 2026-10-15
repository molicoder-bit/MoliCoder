import pytest

from local_model_chat.environment.auth import CredentialUnavailable, ProtectedCredentialStore
from local_model_chat.environment.security import platform_security
from local_model_chat.inference.eligibility import EligibilityRequest, evaluate_eligibility
from local_model_chat.inference.registry import OfferingRecord, ProviderRegistry
from local_model_chat.storage import ChatStore


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_platform_security_discloses_actual_credential_and_sandbox_boundary(system):
    state = platform_security(system)
    assert state.strict_agent_sandbox is False
    assert state.metered_api_allowed is (system == "Windows")
    assert (state.credential_backend == "Windows DPAPI") is (system == "Windows")


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_non_windows_metered_route_is_ineligible_without_credential_backend(monkeypatch, tmp_path, system):
    from local_model_chat.inference import eligibility

    monkeypatch.setattr(eligibility, "platform_security", lambda: platform_security(system))
    registry = ProviderRegistry(ChatStore(tmp_path / "security.sqlite3"))
    offering = OfferingRecord(
        "paid", "host", "model", "direct", None, "metered", "eligible",
        {"stream": True}, "private", True, 1.0, 1.0, {},
        "2099-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00",
        "https://host.example", "https://host.example/terms", "healthy",
    )
    result = evaluate_eligibility(offering, EligibilityRequest("private", ("stream",),
                                                           paid_allowed=True), registry)
    assert not result.eligible
    assert "metered API route needs a supported OS credential backend" in result.reasons
    with pytest.raises(CredentialUnavailable):
        monkeypatch.setattr("local_model_chat.environment.auth.platform.system", lambda: system)
        ProtectedCredentialStore(tmp_path / "keys")
