from __future__ import annotations

import threading
from uuid import uuid4
from PySide6.QtCore import QThread, Signal

from .context import estimate_messages
from .domain import GenerationMetrics
from .inference.adapters.ollama import OllamaStreamAdapter, local_offering_id
from .inference.budget import BudgetManager
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


class ModelInspectWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, model: str) -> None:
        super().__init__()
        self.model = model

    def run(self) -> None:
        try:
            from .environment.discovery import discover_environment
            from .environment.models import official_model_size, plan_local_model

            size, source = official_model_size(self.model)
            self.ready.emit(plan_local_model(discover_environment(), self.model, size, source=source))
        except Exception as exc:
            self.failed.emit(str(exc))


class ModelPullWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, store, client, plan) -> None:
        super().__init__()
        self.store = store
        self.client = client
        self.plan = plan

    def run(self) -> None:
        try:
            from .environment.models import pull_approved_local_model

            pull_approved_local_model(self.store, self.client, self.plan)
            if self.plan.model not in {str(item.get("name", "")) for item in self.client.tags()}:
                raise RuntimeError("model pull returned without an installed model")
            self.completed.emit(self.plan.model)
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
                timeout_seconds=1800 if self.manifest.size_bytes > 1_000_000_000 else 300,
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


class WindowsOllamaInstallWorker(QThread):
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
            from .environment.windows_installer import install_verified_ollama

            self.completed.emit(install_verified_ollama(
                self.store, self.plan, self.manifest, self.managed_root,
            ))
        except Exception as exc:
            self.failed.emit(str(exc))


class ManagedRepairWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, store, plan, setup_plan, manifest, managed_root) -> None:
        super().__init__()
        self.store = store
        self.plan = plan
        self.setup_plan = setup_plan
        self.manifest = manifest
        self.managed_root = managed_root

    def run(self) -> None:
        try:
            import subprocess

            from .environment.install import _download
            from .environment.maintenance import perform_maintenance

            receipt = perform_maintenance(
                self.store, self.managed_root, self.plan,
                manifest=self.manifest, setup_plan=self.setup_plan, downloader=_download,
            )
            result = subprocess.run(
                [receipt.executable, "--version"], capture_output=True, text=True,
                timeout=10, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or self.manifest.version not in (result.stdout + result.stderr):
                raise RuntimeError("repaired executable failed its version check; backup retained")
            self.completed.emit(receipt)
        except Exception as exc:
            self.failed.emit(str(exc))


class CustomApiWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, store, project_id: int, offering_id: str, prompt: str) -> None:
        super().__init__()
        self.store = store
        self.project_id = project_id
        self.offering_id = offering_id
        self.prompt = prompt
        self._cancel_requested = threading.Event()
        self._adapter = None
        self._request_id: str | None = None

    def cancel(self) -> None:
        self._cancel_requested.set()
        if self._adapter is not None and self._request_id is not None:
            self._adapter.cancel(self._request_id)

    def run(self) -> None:
        request = None
        try:
            from .config import data_dir
            from .environment.auth import ProtectedCredentialStore
            from .inference.adapters.openai_compatible import OpenAICompatibleAdapter
            from .inference.api_dispatch import prepare_custom_chat
            from .inference.registry import ProviderRegistry

            if self._cancel_requested.is_set():
                return
            registry = ProviderRegistry(self.store)
            offering = registry.get_offering(self.offering_id)
            if offering is None:
                raise ValueError("custom offering is unavailable")
            credentials = (ProtectedCredentialStore(data_dir() / "credentials")
                           if offering.account_id else None)
            adapter = OpenAICompatibleAdapter(registry, credentials)
            self._adapter = adapter
            request = prepare_custom_chat(
                registry, self.project_id, self.offering_id, self.prompt,
            )
            self._request_id = request.request_id
            if self._cancel_requested.is_set():
                adapter.cancel(request.request_id)
            result = ControlledGateway(
                InferencePolicy(registry), adapter, InferenceLedger(self.store),
            ).invoke(request)
            if result.status == "completed":
                self.completed.emit(result)
            else:
                self.failed.emit(f"Provider request {result.status}: {result.error or 'unknown'}")
        except Exception as exc:
            if request is not None and request.reservation_id:
                manager = BudgetManager(self.store)
                if manager.active_amount(request.reservation_id, request.request_id, request.project_id) is not None:
                    manager.release(request.reservation_id)
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
