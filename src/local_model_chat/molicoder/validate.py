from __future__ import annotations

import ast
import json
import os
import re
import signal
import subprocess
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..knowledge import ProjectKnowledge
from ..storage import ChatStore
from .providers import redact_text


@dataclass(frozen=True, slots=True)
class ValidationCommand:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 300.0
    kind: str = "test"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    command: ValidationCommand
    exit_code: int
    output: str
    errors: tuple[str, ...]
    log_path: Path


@dataclass(frozen=True, slots=True)
class ValidationReport:
    passed: bool
    results: tuple[ValidationResult, ...]

    def summary(self) -> str:
        lines = ["validation passed" if self.passed else "validation failed"]
        for result in self.results:
            lines.append(
                f"{result.command.kind}/{result.command.name}: exit {result.exit_code}; log={result.log_path}"
            )
            lines.extend(result.errors[:20])
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ArchitectureConflict:
    decision_id: int
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ArchitectureAssessment:
    conflicts: tuple[ArchitectureConflict, ...]
    review_required: tuple[str, ...]


def check_architectural_invariants(
    store: ChatStore, project_id: int, changed_paths: tuple[str, ...],
) -> tuple[ArchitectureConflict, ...]:
    """Enforce explicit approved path-protection decisions; leave prose rules for review."""
    conflicts: list[ArchitectureConflict] = []
    for decision in ProjectKnowledge(store).approved_decisions(project_id):
        match = re.fullmatch(r"Do not modify ([^\s]+)", decision.body.strip(), re.IGNORECASE)
        if match is None:
            continue
        protected = match.group(1).strip("/").replace("\\", "/")
        for path in changed_paths:
            normalized = path.replace("\\", "/")
            if normalized == protected or normalized.startswith(protected + "/"):
                conflicts.append(ArchitectureConflict(
                    decision.id, normalized,
                    f"{normalized} violates approved decision #{decision.id} r{decision.revision}: {decision.body}",
                ))
    return tuple(conflicts)


def assess_architectural_invariants(
    store: ChatStore, project_id: int, changed_paths: tuple[str, ...],
) -> ArchitectureAssessment:
    conflicts = check_architectural_invariants(store, project_id, changed_paths)
    review: list[str] = []
    if changed_paths:
        for decision in ProjectKnowledge(store).approved_decisions(project_id):
            if re.fullmatch(r"Do not modify ([^\s]+)", decision.body.strip(), re.IGNORECASE):
                continue
            title = decision.title.strip().lower()
            if title.startswith(("architecture:", "rule:")):
                review.append(
                    f"approved decision #{decision.id} r{decision.revision} needs human review: "
                    f"{decision.title} — {decision.body}"
                )
    return ArchitectureAssessment(conflicts, tuple(review))


_ERROR_LINE = re.compile(r"(?:error|failed|failure|traceback|exception)", re.IGNORECASE)


def extract_errors(output: str, limit: int = 50) -> tuple[str, ...]:
    found: list[str] = []
    for line in output.splitlines():
        normalized = line.strip()
        if normalized and _ERROR_LINE.search(normalized) and normalized not in found:
            found.append(normalized)
            if len(found) == limit:
                break
    return tuple(found)


class ProjectValidator:
    """Runs an application-supplied allowlist; the model never supplies commands."""

    def __init__(
        self, root: str | Path, commands: list[ValidationCommand], *,
        should_cancel: Callable[[], bool] | None = None,
        on_progress: Callable[[ValidationCommand, float], None] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"project root does not exist: {self.root}")
        if not commands:
            raise ValueError("at least one validation command is required")
        self.commands = tuple(commands)
        self.log_dir = self.root / ".molicoder" / "logs"
        self.should_cancel = should_cancel or (lambda: False)
        self.on_progress = on_progress

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            # A validation command can spawn test workers. Stop the process tree,
            # then fall back to the direct child if taskkill is unavailable.
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, check=False, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _run_command(self, command: ValidationCommand) -> tuple[int, str]:
        if self.should_cancel():
            return 130, "ERROR validation cancelled by user before command start\n"
        started = time.monotonic()
        process = subprocess.Popen(
            command.argv, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", shell=False,
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0),
            start_new_session=os.name != "nt",
        )
        last_progress_tick = -1
        while True:
            elapsed = time.monotonic() - started
            if self.on_progress is not None and int(elapsed // 5) > last_progress_tick:
                last_progress_tick = int(elapsed // 5)
                try:
                    self.on_progress(command, elapsed)
                except BaseException:
                    self._stop_process(process)
                    process.communicate()
                    raise
            if self.should_cancel():
                self._stop_process(process)
                stdout, stderr = process.communicate()
                return 130, stdout + stderr + "\nERROR validation cancelled by user\n"
            remaining = command.timeout_seconds - elapsed
            if remaining <= 0:
                self._stop_process(process)
                stdout, stderr = process.communicate()
                return 124, stdout + stderr + f"\nERROR validation timed out after {command.timeout_seconds}s\n"
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue
            return process.returncode, stdout + stderr

    def run(self, check_names: tuple[str, ...] = ("project-default",)) -> ValidationReport:
        available = {command.name: command for command in self.commands}
        if check_names == ("project-default",):
            selected = self.commands
        else:
            if not check_names or any(name not in available for name in check_names):
                raise ValueError("task requested a check outside the approved validation allowlist")
            selected = tuple(available[name] for name in dict.fromkeys(check_names))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        results: list[ValidationResult] = []
        structural = self.run_structural()
        if not structural.passed:
            return structural
        for index, command in enumerate(selected, start=1):
            exit_code, output = self._run_command(command)
            output = redact_text(output)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", command.name).strip("-") or "validation"
            log_path = self.log_dir / f"{timestamp}-{index}-{safe_name}.log"
            log_path.write_text(output, encoding="utf-8")
            result = ValidationResult(command, exit_code, output, extract_errors(output), log_path)
            results.append(result)
            if exit_code != 0:
                break
        return ValidationReport(all(result.exit_code == 0 for result in results), tuple(results))

    def run_structural(self) -> ValidationReport:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        structural_errors = self._structural_errors()
        if not structural_errors:
            return ValidationReport(True, ())
        output = redact_text("\n".join(f"ERROR {message}" for message in structural_errors) + "\n")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        log_path = self.log_dir / f"{timestamp}-0-structural-safety.log"
        log_path.write_text(output, encoding="utf-8")
        command = ValidationCommand("structural-safety", ("molicoder", "structural-safety"), kind="static")
        return ValidationReport(
            False,
            (ValidationResult(command, 2, output, tuple(output.splitlines()), log_path),),
        )

    def _structural_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        pyproject = self.root / "pyproject.toml"
        if pyproject.is_file():
            try:
                tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
                errors.append(f"invalid pyproject.toml: {exc}")
        package = self.root / "package.json"
        if package.is_file():
            try:
                json.loads(package.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(f"invalid package.json: {exc}")
        ignored = {".git", ".molicoder", ".venv", "node_modules", "build", "dist", "__pycache__"}
        for path in self.root.rglob("*.py"):
            if any(part in ignored for part in path.relative_to(self.root).parts):
                continue
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
            except (OSError, UnicodeError, SyntaxError) as exc:
                errors.append(f"invalid Python syntax in {path.relative_to(self.root)}: {exc}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "eval":
                    errors.append(
                        f"unrestricted eval is not allowed in {path.relative_to(self.root)}:{getattr(node, 'lineno', 0)}"
                    )
        return tuple(errors)

    def run_and_summarize(self) -> str:
        return self.run().summary()

