"""Unit suites use fixtures for host discovery; live qualification runs separately."""
import pytest


@pytest.fixture(autouse=True)
def isolated_startup_runtime_discovery(monkeypatch, request):
    if request.node.get_closest_marker('integration') is None:
        monkeypatch.setattr('local_model_chat.orchestration.catalog.discover_local_tools', lambda: ())
