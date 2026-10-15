import json

from local_model_chat.ollama import OllamaClient, _metrics_from_response


def test_managed_ollama_binary_can_start_local_service_without_path(tmp_path, monkeypatch) -> None:
    binary = tmp_path / "ollama"
    binary.write_bytes(b"fixture")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("local_model_chat.ollama.shutil.which", lambda _name: None)
    monkeypatch.setattr("local_model_chat.environment.managed_tools.managed_executable",
                        lambda tool_id: binary if tool_id == "ollama" else None)
    client = OllamaClient()
    checks = iter((False, True))
    monkeypatch.setattr(client, "is_available", lambda: next(checks))
    captured = {}
    monkeypatch.setattr("local_model_chat.ollama.subprocess.Popen", lambda command, **_kwargs:
                        captured.update(command=command) or object())
    assert client.ensure_running(wait_seconds=1)
    assert captured["command"] == [str(binary), "serve"]


def test_metrics_conversion() -> None:
    metrics = _metrics_from_response(
        {
            "prompt_eval_count": 100,
            "prompt_eval_duration": 2_000_000_000,
            "eval_count": 40,
            "eval_duration": 1_000_000_000,
            "load_duration": 500_000_000,
            "done_reason": "stop",
        },
        4.25,
    )
    assert metrics.prompt_tokens_per_second == 50.0
    assert metrics.output_tokens_per_second == 40.0
    assert metrics.load_seconds == 0.5
    assert metrics.total_seconds == 4.25


def test_non_streaming_chat_sends_tools_and_returns_metrics(monkeypatch) -> None:
    captured = {}

    def fake_request(self, path, payload=None, timeout=None):
        captured.update(path=path, payload=payload, timeout=timeout)
        return {
            "message": {"content": json.dumps({"name": "list_dir", "arguments": {"path": "."}})},
            "prompt_eval_count": 10,
            "eval_count": 5,
        }

    monkeypatch.setattr(OllamaClient, "_request", fake_request)
    client = OllamaClient()
    response = client.chat(
        "molicoder:latest",
        [{"role": "user", "content": "Inspect the repository"}],
        tools=[{"type": "function", "function": {"name": "list_dir"}}],
        context_window=16384,
        max_output_tokens=2048,
        temperature=0.2,
        top_p=0.9,
    )

    assert captured["path"] == "/api/chat"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["tools"][0]["function"]["name"] == "list_dir"
    assert response.message["content"].startswith("{")
    assert response.metrics.prompt_tokens == 10
