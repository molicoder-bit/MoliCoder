from local_model_chat.inference.discovery import discover_local_tools


class FakeOllama:
    def tags(self):
        return [{"name": "coder:7b"}]

    def stream_chat(self, *_args, **_kwargs):
        raise AssertionError("discovery must not run inference")

    def ensure_running(self):
        raise AssertionError("discovery must not start Ollama")


def test_discovery_separates_binary_version_and_authentication() -> None:
    queried = []

    def which(name):
        return "C:/tools/codex.exe" if name == "codex" else None

    def version(binary):
        queried.append(binary)
        return "codex 1.0"

    found = {item.tool_id: item for item in discover_local_tools(
        client=FakeOllama(), which=which, version=version,
    )}

    assert found["ollama"].models == ("coder:7b",)
    assert found["ollama"].auth_state == "not_required"
    assert found["codex"].installed
    assert found["codex"].version == "codex 1.0"
    assert found["codex"].auth_state == "unknown"
    assert not found["opencode"].installed
    assert len(queried) == 1
