from __future__ import annotations

import sys
import shutil
from pathlib import Path
from typing import Callable

from ..config import MODEL_PRESETS
from ..domain import Project
from ..ollama import OllamaClient
from ..storage import ChatStore
from .executor import AgentExecutor, ExecutionResult
from .gateway import InferenceGateway
from .orchestrator import WorktreeOrchestrator
from .planner.local import LocalPlanner
from .planner.codex_cli import CallBudget, CodexCliConsultant, CodexCliPlanner, ExternalUnavailable, RoutedPlanner
from .projectstate import ensure_project_state
from .repair import RepairLoop, RepairOutcome
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
) -> ProjectValidator:
    state = ensure_project_state(root)
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
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv):
            raise ValueError("validation argv must be a non-empty string list")
        timeout = raw.get("timeout_seconds", 300.0)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("validation timeout_seconds must be positive")
        commands.append(
            ValidationCommand(
                raw["name"],
                tuple(value.replace("{python}", python) for value in argv),
                float(timeout),
            )
        )
    return ProjectValidator(state.root, commands)


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
    mode: str = "LOCAL_ONLY",
    on_event: Callable[[str, dict], None] | None = None,
) -> WorktreeOrchestrator:
    root = Path(project.path).resolve()
    preset = MODEL_PRESETS["molicoder:latest"]
    gateway = InferenceGateway(client)
    local_planner = LocalPlanner(
        gateway=gateway,
        model=preset.name,
        context_window=preset.context_window,
        max_output_tokens=preset.max_output_tokens,
    )
    external_planner = None
    consultant = None
    if mode.upper() in {"AUTO", "FRONTIER_PLAN"}:
        try:
            budget = CallBudget(max_external_calls=3, max_planning_calls=1, max_rescue_calls=1)
            external_planner = CodexCliPlanner(project_root=root, budget=budget)
            consultant = CodexCliConsultant(project_root=root, budget=budget)
        except ExternalUnavailable:
            external_planner = None
            consultant = None
    planner = RoutedPlanner(mode, local=local_planner, external=external_planner)

    def execute(worktree: Path, objective: str) -> RepairOutcome:
        validator = build_validator(worktree, environment_root=root)
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
            rescue=consultant.rescue if consultant else None,
        )
        return loop.run(SkillLibrary(worktree).augment(objective))

    return WorktreeOrchestrator(
        store=store,
        project=project,
        planner=planner,
        execute_task=execute,
        repair_conflict=execute,
        validate_integration=lambda worktree: build_validator(
            worktree, environment_root=root
        ).run_and_summarize(),
        review_integration=consultant.review if consultant and mode.upper() == "FRONTIER_PLAN" else None,
        external_sources=(consultant,) if consultant else (),
        max_workers=AdaptiveConcurrency(max_workers=4).recommend(project.kind),
        on_event=on_event,
    )
