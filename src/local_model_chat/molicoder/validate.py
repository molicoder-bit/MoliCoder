from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ValidationCommand:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 300.0


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
            lines.append(f"{result.command.name}: exit {result.exit_code}; log={result.log_path}")
            lines.extend(result.errors[:20])
        return "\n".join(lines)


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

    def __init__(self, root: str | Path, commands: list[ValidationCommand]) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"project root does not exist: {self.root}")
        if not commands:
            raise ValueError("at least one validation command is required")
        self.commands = tuple(commands)
        self.log_dir = self.root / ".molicoder" / "logs"

    def run(self) -> ValidationReport:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        results: list[ValidationResult] = []
        for index, command in enumerate(self.commands, start=1):
            try:
                completed = subprocess.run(
                    command.argv,
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=command.timeout_seconds,
                    shell=False,
                    check=False,
                )
                output = completed.stdout + completed.stderr
                exit_code = completed.returncode
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                output = stdout + stderr + f"\nERROR validation timed out after {command.timeout_seconds}s\n"
                exit_code = 124
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", command.name).strip("-") or "validation"
            log_path = self.log_dir / f"{timestamp}-{index}-{safe_name}.log"
            log_path.write_text(output, encoding="utf-8")
            result = ValidationResult(command, exit_code, output, extract_errors(output), log_path)
            results.append(result)
            if exit_code != 0:
                break
        return ValidationReport(all(result.exit_code == 0 for result in results), tuple(results))

    def run_and_summarize(self) -> str:
        return self.run().summary()

