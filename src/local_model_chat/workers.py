from __future__ import annotations

import threading
from uuid import uuid4
from PySide6.QtCore import QThread, Signal

from .context import estimate_messages
from .domain import GenerationMetrics
from .inference.adapters.ollama import OllamaStreamAdapter, local_offering_id
from .inference.contracts import InferenceEventKind, InferencePurpose, InferenceRequest
from .inference.ledger import InferenceLedger
from .inference.policy import ControlledGateway, InferencePolicy
from .ollama import OllamaClient
from .molicoder.orchestrator import OrchestrationResult, Phase2Orchestrator


class ChatWorker(QThread):
    content_delta = Signal(str)
    thinking_delta = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self, client: OllamaClient, model: str, messages: list[dict[str, str]],
        options: dict, policy: InferencePolicy, project_id: int | None,
    ) -> None:
        super().__init__()
        self.client = client
        self.model = model
        self.messages = messages
        self.options = options
        self.policy = policy
        self.project_id = project_id
        self.request_id = str(uuid4())
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            request = InferenceRequest(
                request_id=self.request_id, project_id=self.project_id,
                offering_id=local_offering_id(self.model), model_id=self.model,
                purpose=InferencePurpose.CHAT, messages=tuple(self.messages),
                sensitivity="private", required_capabilities=("stream",),
                max_input_tokens=max(1, estimate_messages(self.messages)),
                max_output_tokens=int(self.options["max_output_tokens"]),
                timeout_seconds=600, stream=True,
                options={key: value for key, value in self.options.items() if key != "max_output_tokens"},
            )
            generator = ControlledGateway(
                self.policy, OllamaStreamAdapter(self.client), InferenceLedger(self.policy.registry.store),
            ).stream(request)
            while not self._cancelled:
                try:
                    event = next(generator)
                except StopIteration:
                    self.failed.emit("Inference stream ended without a completion event.")
                    return
                if event.kind == InferenceEventKind.CONTENT_DELTA:
                    self.content_delta.emit(str(event.payload["text"]))
                elif event.kind == InferenceEventKind.REASONING_DELTA:
                    self.thinking_delta.emit(str(event.payload["text"]))
                elif event.kind == InferenceEventKind.COMPLETED:
                    self.completed.emit(event.payload.get("metrics") or GenerationMetrics())
                    return
            close = getattr(generator, "close", None)
            if close:
                close()
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


class DoctorWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            from .environment.discovery import discover_environment
            from .environment.doctor import doctor_report
            from .environment.manifests import load_manifests

            self.ready.emit(doctor_report(discover_environment(), load_manifests()))
        except Exception as exc:
            self.failed.emit(str(exc))


class ManagedInstallWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, store, plan, manifest, managed_root) -> None:
        super().__init__()
        self.store = store
        self.plan = plan
        self.manifest = manifest
        self.managed_root = managed_root

    def run(self) -> None:
        try:
            import subprocess

            from .environment.install import install_managed_archive

            receipt = install_managed_archive(
                self.store, self.plan, self.manifest, self.managed_root,
            )
            result = subprocess.run(
                [receipt.executable, "--version"], capture_output=True, text=True,
                timeout=10, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or self.manifest.version not in (result.stdout + result.stderr):
                raise RuntimeError("managed executable failed its version check")
            self.completed.emit(receipt)
        except Exception as exc:
            self.failed.emit(str(exc))


class OrchestrationWorker(QThread):
    event = Signal(str, object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, orchestrator: Phase2Orchestrator, request: str, mode: str, resume_run_id: int | None = None) -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self.request = request
        self.mode = mode
        self.resume_run_id = resume_run_id
        self._cancelled = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self.orchestrator.should_cancel = self._cancelled.is_set
        self.orchestrator.wait_if_paused = self._resume.wait

    def cancel(self) -> None:
        self._cancelled.set()
        self._resume.set()
        cancel = getattr(self.orchestrator, "cancel", None)
        if cancel:
            cancel()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def relay_event(self, kind: str, payload: dict) -> None:
        self.event.emit(kind, payload)

    def run(self) -> None:
        try:
            if self.resume_run_id is not None:
                result: OrchestrationResult = self.orchestrator.resume(self.resume_run_id)
            else:
                result = self.orchestrator.run(self.request, mode=self.mode)
            self.completed.emit(result)
        except Exception as exc:  # worker boundary: surface failures to the UI
            self.failed.emit(str(exc))
