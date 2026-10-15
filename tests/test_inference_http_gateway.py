import io
import json
import urllib.error
import urllib.request

import pytest

from local_model_chat.inference.adapters.ollama import ensure_local_offering
from local_model_chat.inference.http_gateway import LocalPolicyHttpGateway
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.molicoder.providers import OpenCodeProvider
from local_model_chat.storage import ChatStore


class FakeResponse(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}


def test_loopback_gateway_auth_policy_ledger_and_agent_profile(tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    run_id = store.create_run(project.id, "Build locally", "LOCAL_OFFLINE")
    registry = ProviderRegistry(store)
    offering_id = ensure_local_offering(registry, "molicoder:latest", context_window=4096,
                                        max_output_tokens=256)
    registry.set_offering_state(offering_id, status="eligible", health_state="healthy",
                                actor="test", reason="fixture model")
    forwarded = []

    def opener(request, timeout):
        forwarded.append((request.full_url, dict(request.headers), json.loads(request.data)))
        return FakeResponse(json.dumps({
            "id": "mock", "choices": [{"message": {"content": "done"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }).encode())

    gateway = LocalPolicyHttpGateway(store, project.id, run_id, "molicoder:latest",
                                     context_window=4096, max_output_tokens=256, opener=opener)
    gateway.start()
    try:
        provider = OpenCodeProvider(binary=tmp_path / "opencode.exe")
        provider.configure_local_gateway(gateway.endpoint, gateway.model, gateway.token)
        profile = json.loads(provider.invocation_environment()["OPENCODE_CONFIG_CONTENT"])
        assert profile["enabled_providers"] == ["molicoder-local"]
        options = profile["provider"]["molicoder-local"]["options"]
        assert options["baseURL"] == gateway.endpoint
        payload = json.dumps({"model": gateway.model, "messages": [{"role": "user", "content": "hi"}],
                              "max_tokens": 32}).encode()
        request = urllib.request.Request(
            gateway.endpoint + "/chat/completions", data=payload, method="POST",
            headers={"Authorization": f"Bearer {options['apiKey']}",
                     "Content-Type": "application/json", "X-MoliCoder-Request-ID": "agent-task-1"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert json.load(response)["choices"][0]["message"]["content"] == "done"
        assert forwarded[0][0] == "http://127.0.0.1:11434/v1/chat/completions"
        assert "Authorization" not in forwarded[0][1]
        assert store.run_inference_usage(run_id)["local_requests"] == 1
        assert store.run_inference_usage(run_id)["local_output_tokens"] == 3
        with pytest.raises(urllib.error.HTTPError) as denial:
            urllib.request.urlopen(urllib.request.Request(
                gateway.endpoint + "/chat/completions", data=payload, method="POST",
                headers={"Content-Type": "application/json"}), timeout=3)
        assert denial.value.code == 401
    finally:
        gateway.stop()


def test_gateway_denies_unavailable_offering_before_upstream_call(tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    run_id = store.create_run(project.id, "Build locally", "LOCAL_OFFLINE")
    ensure_local_offering(ProviderRegistry(store), "molicoder:latest", context_window=4096,
                          max_output_tokens=256)
    forwarded = []
    gateway = LocalPolicyHttpGateway(
        store, project.id, run_id, "molicoder:latest", context_window=4096,
        max_output_tokens=256, opener=lambda request, timeout: forwarded.append(request),
    )
    gateway.start()
    try:
        payload = json.dumps({"model": gateway.model, "messages": [{"role": "user", "content": "hi"}]}).encode()
        with pytest.raises(urllib.error.HTTPError) as denial:
            urllib.request.urlopen(urllib.request.Request(
                gateway.endpoint + "/chat/completions", data=payload, method="POST",
                headers={"Authorization": f"Bearer {gateway.token}", "Content-Type": "application/json"},
            ), timeout=3)
        assert denial.value.code == 403
        assert not forwarded
        assert store.run_inference_usage(run_id)["local_requests"] == 1
    finally:
        gateway.stop()
