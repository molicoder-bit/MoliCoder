from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from ..config import MODEL_PRESETS
from ..domain import Project
from ..ollama import OllamaClient
from ..storage import ChatStore
from .executor import AgentExecutor, ExecutionResult
from .gateway import InferenceGateway
from .orchestrator import Phase2Orchestrator
from .planner.local import LocalPlanner
from .projectstate import ensure_project_state
from .repair import RepairLoop
from .tools import ToolDispatcher
from .validate import ProjectValidator, ValidationCommand


def _python_executable(root: Path) -> str:
    candidates = (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python")
    return str(next((candidate for candidate in candidates if candidate.is_file()), Path(sys.executable)))


def build_validator(root: str | Path) -> ProjectValidator:
    state = ensure_project_state(root)
    raw_commands = state.config.get("validation")
    if not isinstance(raw_commands, list) or not raw_commands:
        raise ValueError("project has no validation allowlist in .molicoder/project.json")
    commands: list[ValidationCommand] = []
    python = _python_executable(state.root)
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
    on_event: Callable[[str, dict], None] | None = None,
) -> Phase2Orchestrator:
    root = Path(project.path).resolve()
    validator = build_validator(root)
    preset = MODEL_PRESETS["molicoder:latest"]
    gateway = InferenceGateway(client)
    planner = LocalPlanner(
        gateway=gateway,
        model=preset.name,
        context_window=preset.context_window,
        max_output_tokens=preset.max_output_tokens,
    )

    def execute(objective: str) -> ExecutionResult:
        dispatcher = ToolDispatcher(root, validation=validator.run_and_summarize)
        executor = AgentExecutor(
            gateway=gateway,
            dispatcher=dispatcher,
            model=preset.name,
            context_window=preset.context_window,
            max_output_tokens=preset.max_output_tokens,
            temperature=preset.temperature,
            top_p=preset.top_p,
        )
        return executor.run(objective)

    repair = RepairLoop(execute=execute, diagnose=LocalDiagnoser(gateway, preset.name))
    return Phase2Orchestrator(
        store=store,
        project=project,
        planner=planner,
        repair=repair,
        on_event=on_event,
    )
