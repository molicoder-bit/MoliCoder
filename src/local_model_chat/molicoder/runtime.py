from __future__ import annotations

import sys
import shutil
from pathlib import Path
from typing import Callable

from ..config import MODEL_PRESETS
from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..domain import Project
from ..inference.adapters.ollama import ensure_local_offering, local_offering_id
from ..inference.adapters.cli import BoundedOpaqueProvider
from ..inference.policy import InferencePolicy
from ..inference.registry import ProviderRegistry
from ..inference.router import RouteRequest, choose_route
from ..kandev.contracts import KandevConnection, KandevMode
from ..kandev.adapter import KandevTaskAdapter
from ..kandev.rest import KandevTransportError
from ..kandev.run import KandevRunOrchestrator
from ..intent import IntentKind
from ..ollama import OllamaClient
from ..storage import ChatStore
from .executor import AgentExecutor, ExecutionResult
from .gateway import InferenceGateway
from .orchestrator import SequentialRunOrchestrator, WorktreeOrchestrator
from .planner.local import LocalPlanner
from .providers import ProviderUnavailable, agent_profile, create_provider
from .projectstate import ensure_project_state
from .repair import RepairLoop, RepairOutcome
from .selective_review import SelectiveModelReviewer
from .tools import ToolDispatcher
from .validate import ProjectValidator, ValidationCommand
from .metrics import AdaptiveConcurrency
from .skills import SkillLibrary


def _python_executable(root: Path) -> str:
    candidates = (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python")
    project_python = next((candidate for candidate in candidates if candidate.is_file()), None)
    if project_python is not None:
        return str(project_python)
    if getattr(sys, "frozen", False):
        system_python = shutil.which("python") or shutil.which("python3")
        if not system_python:
            raise ValueError("Python validation requires a project .venv or Python installed in PATH")
        return system_python
    return sys.executable


def build_validator(
    root: str | Path,
    *,
    environment_root: str | Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[ValidationCommand, float], None] | None = None,
) -> ProjectValidator:
    execution_root = Path(root).resolve()
    config_root = Path(environment_root).resolve() if environment_root is not None else execution_root
    state = ensure_project_state(config_root)
    raw_commands = state.config.get("validation")
    if not isinstance(raw_commands, list) or not raw_commands:
        raise ValueError("project has no validation allowlist in .molicoder/project.json")
    commands: list[ValidationCommand] = []
    # Git worktrees intentionally exclude the project's ignored .venv. Resolve
    # Python from the source checkout while still running validation in the
    # isolated worktree.
    python = _python_executable(
        Path(environment_root).resolve() if environment_root is not None else state.root
    )
    for raw in raw_commands:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise ValueError("invalid validation command in .molicoder/project.json")
        if any(command.name == raw["name"] for command in commands):
            raise ValueError("validation command names must be unique")
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv):
            raise ValueError("validation argv must be a non-empty string list")
        timeout = raw.get("timeout_seconds", 300.0)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("validation timeout_seconds must be positive")
        kind = raw.get("kind", "test")
        if kind not in {"compile", "type", "lint", "test", "static", "security", "build"}:
            raise ValueError("validation kind is not supported")
        commands.append(
            ValidationCommand(
                raw["name"],
                tuple(value.replace("{python}", python) for value in argv),
                float(timeout),
                kind,
            )
        )
    return ProjectValidator(
        execution_root, commands, should_cancel=should_cancel, on_progress=on_progress,
    )


def validate_intermediate_task(
    root: str | Path,
    *,
    environment_root: str | Path | None = None,
    task: object | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[ValidationCommand, float], None] | None = None,
) -> str:
    """Validate a checkpoint without treating an intentionally empty test suite as final failure."""
    path = Path(root).resolve()
    validator = build_validator(
        path, environment_root=environment_root,
        should_cancel=should_cancel, on_progress=on_progress,
    )
    structural = validator.run_structural()
    if not structural.passed:
        return structural.summary()
    checks = tuple(getattr(task, "verification_checks", ("project-default",)))
    if checks != ("project-default",):
        return validator.run(checks).summary()
    expected_paths = tuple(str(item).lower() for item in getattr(task, "expected_paths", ()))
    task_text = " ".join(
        str(getattr(task, field, "")) for field in ("title", "objective")
    ).lower()
    touches_tests = any(
        path_part.startswith("tests/") or Path(path_part).name.startswith("test_")
        for path_part in expected_paths
    ) or " test" in f" {task_text}"
    if task is not None and not touches_tests:
        return "validation passed\nstructural safety passed; complete test suite deferred to test task and final gate"
    report = validator.run()
    if report.passed:
        return report.summary()
    python_tests = [
        item for item in path.rglob("test_*.py")
        if not any(part in {".git", ".molicoder", ".venv", "__pycache__"} for part in item.relative_to(path).parts)
    ]
    if not python_tests and len(report.results) == 1 and report.results[0].command.name == "pytest" and report.results[0].exit_code == 5:
        return "validation passed\npytest: no tests collected yet; final validation remains mandatory"
    return report.summary()


class LocalDiagnoser:
    def __init__(self, gateway: InferenceGateway, model: str) -> None:
        self.gateway = gateway
        self.model = model

    def __call__(self, failure: str) -> str:
        preset = MODEL_PRESETS[self.model]
        response = self.gateway.chat(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Diagnose the coding failure using only the supplied evidence. "
                        "Name the most likely root cause and a concrete next repair. Be concise."
                    ),
                },
                {"role": "user", "content": failure},
            ],
            tools=None,
            context_window=preset.context_window,
            max_output_tokens=min(1024, preset.max_output_tokens),
            temperature=0.1,
            top_p=preset.top_p,
        )
        return str(response.message.get("content", "")).strip()


def build_orchestrator(
    *,
    store: ChatStore,
    project: Project,
    client: OllamaClient | None = None,
    mode: str = "HYBRID",
    provider_name: str = "codex",
    model: str = "",
    authorization_scope: str | None = None,
    fallback_provider_name: str = "",
    fallback_authorization_scope: str | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    kandev_connection: KandevConnection | None = None,
    kandev_adapter: KandevTaskAdapter | None = None,
    kandev_authorize_launch: Callable | None = None,
) -> SequentialRunOrchestrator | KandevRunOrchestrator:
    connection = kandev_connection or KandevConnection()
    if connection.mode != KandevMode.DISABLED:
        if kandev_adapter is None or kandev_adapter.connection != connection:
            raise ProviderUnavailable(
                f"Kandev {connection.mode.value} execution is unavailable until adapter readiness is verified; "
                "configure the local Kandev connection or disable it to use internal Build"
            )
        try:
            readiness = kandev_adapter.probe()
        except KandevTransportError as exc:
            raise ProviderUnavailable(f"Kandev connection check failed: {exc}") from None
        if readiness.health_state != "ready":
            raise ProviderUnavailable("Kandev adapter readiness failed: required task capabilities are unavailable")
        return KandevRunOrchestrator(
            store=store, project=project, adapter=kandev_adapter, on_event=on_event,
            authorize_launch=kandev_authorize_launch,
            require_plan_approval=True,
            validate=lambda worktree: build_validator(
                worktree, environment_root=Path(project.path).resolve(),
            ).run_and_summarize(),
        )
    root = Path(project.path).resolve()
    preset = MODEL_PRESETS["molicoder:latest"]
    client = client or OllamaClient()
    inference_registry = ProviderRegistry(store)
    ensure_local_offering(
        inference_registry, preset.name,
        context_window=preset.context_window,
        max_output_tokens=preset.max_output_tokens,
    )
    try:
        local_models = {str(item.get("name", "")) for item in client.tags()}
    except Exception:
        local_models = set()
    local_ready = preset.name in local_models
    inference_registry.set_offering_state(
        local_offering_id(preset.name),
        status="eligible" if local_ready else "unavailable",
        health_state="healthy" if local_ready else "unavailable",
        actor="build-preflight", reason="local model present" if local_ready else "local model missing",
    )
    gateway = InferenceGateway(
        client, policy=InferencePolicy(inference_registry), project_id=project.id,
    )
    local_planner = LocalPlanner(
        gateway=gateway,
        model=preset.name,
        context_window=preset.context_window,
        max_output_tokens=preset.max_output_tokens,
    )
    strategy = SequentialRunOrchestrator.normalize_strategy(mode)
    route_decision: dict[str, object]
    if strategy == "LOCAL_OFFLINE":
        route = choose_route(inference_registry, RouteRequest(
            "Local Build execution", project.id, sensitivity="private",
            explicit_intent=IntentKind.EXECUTION,
            candidate_offering_ids=(local_offering_id(preset.name),),
        ))
        if route.offering_id != local_offering_id(preset.name):
            raise ProviderUnavailable(route.reason + ": " + "; ".join(route.alternatives))
        route_decision = {
            "selected": route.offering_id, "reason": route.reason,
            "constraints": ["local model healthy", "tools verified", "private repository stays local"],
            "alternatives": list(route.alternatives), "usage_accuracy": "local_zero",
        }
    else:
        route_decision = {
            "selected": f"opaque-cli:{provider_name}",
            "reason": "explicit Build provider and strategy selection",
            "constraints": ["exact Build authorization", "CLI call and duration ceilings",
                            "internal token and subscription usage unknown"],
            "alternatives": [local_offering_id(preset.name)] if local_ready else [],
            "usage_accuracy": "unknown",
        }
    provider = create_provider(provider_name, model=model, project_root=root)
    alternate_provider = None
    if fallback_provider_name:
        if (strategy == "LOCAL_OFFLINE" or fallback_provider_name == provider_name
                or not fallback_authorization_scope or not agent_profile(fallback_provider_name).selectable):
            raise ProviderUnavailable("fallback needs a distinct selectable provider and exact authorization scope")
        preferences = store.get_project_preferences(project.id)
        if fallback_provider_name not in preferences.trusted_providers:
            raise ProviderUnavailable("fallback provider is not trusted for this project")
        AuthorizationPolicy(store).require(ActionRequest(
            project.id, "build_run", fallback_authorization_scope, ActionRisk.EXTERNAL,
        ))
        candidate = create_provider(
            fallback_provider_name, model=preferences.model_overrides.get(fallback_provider_name, ""),
            project_root=root,
        )
        candidate_status = candidate.probe()
        if (not candidate_status.installed or not candidate_status.auth_verified
                or not candidate_status.authenticated):
            raise ProviderUnavailable(candidate_status.detail or "fallback provider authentication is unverified")
        alternate_provider = BoundedOpaqueProvider(
            candidate, store, project.id, fallback_authorization_scope,
            max_calls=2, max_seconds=1200,
        )
    if strategy != "LOCAL_OFFLINE":
        if not authorization_scope:
            raise ProviderUnavailable("Build needs an exact user authorization scope before launching a CLI agent")
        status = provider.probe()
        if not status.installed:
            raise ProviderUnavailable(status.detail)
        if not status.authenticated:
            raise ProviderUnavailable(status.detail or f"{provider_name} is not authenticated")
        provider = BoundedOpaqueProvider(
            provider, store, project.id, authorization_scope,
            max_calls=8, max_seconds=3600,
        )
    planner = local_planner if strategy == "LOCAL_OFFLINE" else provider

    orchestrator: SequentialRunOrchestrator | None = None

    def cancelled() -> bool:
        return orchestrator is not None and orchestrator.should_cancel()

    def validation_progress(command: ValidationCommand, elapsed: float) -> None:
        if orchestrator is not None and orchestrator._active_run_id is not None:
            orchestrator._event(
                orchestrator._active_run_id, "validation_command_progress",
                {"name": command.name, "kind": command.kind,
                 "elapsed_seconds": round(elapsed, 1), "timeout_seconds": command.timeout_seconds},
                orchestrator._active_task_id,
            )

    def execute(worktree: Path, objective: str) -> RepairOutcome:
        validator = build_validator(
            worktree, environment_root=root,
            should_cancel=cancelled, on_progress=validation_progress,
        )
        dispatcher = ToolDispatcher(worktree, validation=validator.run_and_summarize)
        executor = AgentExecutor(
            gateway=gateway,
            dispatcher=dispatcher,
            model=preset.name,
            context_window=preset.context_window,
            max_output_tokens=preset.max_output_tokens,
            temperature=preset.temperature,
            top_p=preset.top_p,
        )
        loop = RepairLoop(
            execute=executor.run,
            diagnose=LocalDiagnoser(gateway, preset.name),
            rescue=None,
            max_attempts=1,
        )
        return loop.run(SkillLibrary(worktree).augment(objective))

    orchestrator = SequentialRunOrchestrator(
        store=store,
        project=project,
        planner=planner,
        provider=provider,
        local_executor=execute,
        validate=lambda worktree: build_validator(
            worktree, environment_root=root,
            should_cancel=cancelled, on_progress=validation_progress,
        ).run_and_summarize(),
        validate_task=lambda worktree, task: validate_intermediate_task(
            worktree, environment_root=root, task=task,
            should_cancel=cancelled, on_progress=validation_progress,
        ),
        on_event=on_event,
        auto_apply=False,
        require_plan_approval=True,
        max_task_attempts=2,
        task_timeout_seconds=1200.0,
        route_decision=route_decision,
        inference_gateway=gateway,
        model_reviewer=SelectiveModelReviewer(
            gateway, preset.name, context_window=preset.context_window,
        ),
        alternate_provider=alternate_provider,
    )
    return orchestrator
