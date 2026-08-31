from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from .domain import GenerationMetrics
from .ollama import OllamaClient
from .molicoder.orchestrator import OrchestrationResult, Phase2Orchestrator


class ChatWorker(QThread):
    content_delta = Signal(str)
    thinking_delta = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, client: OllamaClient, model: str, messages: list[dict[str, str]], options: dict) -> None:
        super().__init__()
        self.client = client
        self.model = model
        self.messages = messages
        self.options = options
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            generator = self.client.stream_chat(self.model, self.messages, **self.options)
            while not self._cancelled:
                try:
                    chunk = next(generator)
                except StopIteration as stop:
                    self.completed.emit(stop.value or GenerationMetrics())
                    return
                message = chunk.get("message", {})
                content = str(message.get("content", ""))
                thinking = str(message.get("thinking", ""))
                if content:
                    self.content_delta.emit(content)
                if thinking:
                    self.thinking_delta.emit(thinking)
            generator.close()
            self.completed.emit(GenerationMetrics(done_reason="cancelled"))
        except Exception as exc:  # worker boundary: surface failures to the UI
            self.failed.emit(str(exc))


class StartupWorker(QThread):
    ready = Signal(bool, object)

    def __init__(self, client: OllamaClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        available = self.client.ensure_running()
        models = []
        if available:
            try:
                models = self.client.tags()
            except Exception:
                available = False
        self.ready.emit(available, models)


class OrchestrationWorker(QThread):
    event = Signal(str, object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, orchestrator: Phase2Orchestrator, request: str, mode: str) -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self.request = request
        self.mode = mode

    def relay_event(self, kind: str, payload: dict) -> None:
        self.event.emit(kind, payload)

    def run(self) -> None:
        try:
            result: OrchestrationResult = self.orchestrator.run(self.request, mode=self.mode)
            self.completed.emit(result)
        except Exception as exc:  # worker boundary: surface failures to the UI
            self.failed.emit(str(exc))
