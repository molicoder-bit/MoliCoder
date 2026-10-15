from local_model_chat.config import MODEL_PRESETS
from local_model_chat.domain import GenerationMetrics
from local_model_chat.inference.adapters.ollama import ensure_local_offering, local_offering_id
from local_model_chat.inference.ledger import InferenceLedger
from local_model_chat.inference.policy import InferencePolicy
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore
from local_model_chat.workers import ChatWorker


class StreamingClient:
    def __init__(self):
        self.calls = 0

    def stream_chat(self, *_args, **_kwargs):
        self.calls += 1
        yield {"message": {"content": "Hello", "thinking": "Checking"}}
        return GenerationMetrics(prompt_tokens=12, output_tokens=3, done_reason="stop")


def test_chat_worker_streams_through_approved_local_offering(qtbot, tmp_path) -> None:
    store = ChatStore(tmp_path / "chat-policy.sqlite3")
    registry = ProviderRegistry(store)
    preset = next(iter(MODEL_PRESETS.values()))
    ensure_local_offering(registry, preset.name, context_window=preset.context_window,
                          max_output_tokens=preset.max_output_tokens)
    registry.set_offering_state(
        local_offering_id(preset.name), status="eligible", health_state="healthy",
        actor="test", reason="model available",
    )
    client = StreamingClient()
    worker = ChatWorker(
        client, preset.name, [{"role": "user", "content": "Hi"}],
        {"context_window": preset.context_window, "max_output_tokens": 20},
        InferencePolicy(registry), None,
    )
    content = []
    thinking = []
    complete = []
    worker.content_delta.connect(content.append)
    worker.thinking_delta.connect(thinking.append)
    worker.completed.connect(complete.append)

    worker.run()

    assert content == ["Hello"]
    assert thinking == ["Checking"]
    assert complete[0].prompt_tokens == 12
    assert client.calls == 1
    assert [event["status"] for event in InferenceLedger(store).events(worker.request_id)] == ["started", "completed"]


def test_chat_worker_denial_starts_no_model_call(qtbot, tmp_path) -> None:
    store = ChatStore(tmp_path / "denied-chat.sqlite3")
    registry = ProviderRegistry(store)
    preset = next(iter(MODEL_PRESETS.values()))
    ensure_local_offering(registry, preset.name, context_window=preset.context_window,
                          max_output_tokens=preset.max_output_tokens)
    client = StreamingClient()
    worker = ChatWorker(
        client, preset.name, [{"role": "user", "content": "Hi"}],
        {"context_window": preset.context_window, "max_output_tokens": 20},
        InferencePolicy(registry), None,
    )
    errors = []
    worker.failed.connect(errors.append)

    worker.run()

    assert errors and "offering status" in errors[0]
    assert client.calls == 0
    assert [event["status"] for event in InferenceLedger(store).events(worker.request_id)] == ["denied"]
