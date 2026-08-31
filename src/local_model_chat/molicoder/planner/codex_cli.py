from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..recon import ReconPacket
from .base import PlannerProvider
from .schema import Plan, parse_plan


class ExternalUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalCall:
    provider: str
    purpose: str
    reason: str
    input_tokens: int
    output_tokens: int
    seconds: float
    status: str


class CallBudget:
    def __init__(self, *, max_external_calls: int = 3, max_planning_calls: int = 1, max_rescue_calls: int = 1) -> None:
        self.limits = {
            "all": max_external_calls,
            "planning": max_planning_calls,
            "rescue": max_rescue_calls,
            "review": max_external_calls,
        }
        self.counts = {key: 0 for key in self.limits}
        self._lock = threading.Lock()

    def consume(self, purpose: str) -> None:
        with self._lock:
            if self.counts["all"] >= self.limits["all"]:
                raise ExternalUnavailable("external call budget exhausted")
            if purpose in {"planning", "rescue"} and self.counts[purpose] >= self.limits[purpose]:
                raise ExternalUnavailable(f"external {purpose} budget exhausted")
            self.counts["all"] += 1
            if purpose in self.counts:
                self.counts[purpose] += 1


def discover_codex_binary(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        candidate = Path(explicit).resolve()
        if candidate.is_file():
            return candidate
        raise ExternalUnavailable(f"Codex CLI not found: {candidate}")
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    bundled = local_app_data / "OpenAI" / "Codex" / "bin" / "codex.exe"
    if bundled.is_file():
        return bundled
    found = shutil.which("codex") or shutil.which("codex.exe")
    if found:
        return Path(found).resolve()
    raise ExternalUnavailable("Codex CLI is not installed")


def _usage(stdout: str) -> tuple[int, int]:
    input_tokens = output_tokens = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage") if isinstance(event, dict) else None
        if isinstance(usage, dict):
            input_tokens = max(input_tokens, int(usage.get("input_tokens", 0) or 0))
            output_tokens = max(output_tokens, int(usage.get("output_tokens", 0) or 0))
    return input_tokens, output_tokens


class CodexCliPlanner:
    provider = "codex-cli"

    def __init__(
        self,
        *,
        project_root: str | Path,
        binary: str | Path | None = None,
        budget: CallBudget | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.binary = discover_codex_binary(binary)
        self.budget = budget or CallBudget()
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.schema_path = Path(__file__).with_name("plan.schema.json").resolve()
        self._calls: list[ExternalCall] = []
        self._calls_lock = threading.Lock()

    def plan(self, packet: ReconPacket) -> Plan:
        self.budget.consume("planning")
        prompt = (
            "Act only as a read-only planning consultant. Produce a small, dependency-aware coding plan "
            "from this source-grounded evidence packet. Do not edit files.\n\n" + packet.to_prompt()
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="molicoder-codex-") as temp:
            output_path = Path(temp) / "plan.json"
            command = [
                str(self.binary), "exec", "--ignore-user-config", "--ignore-rules", "--json",
                "-C", str(self.project_root), "-s", "read-only",
                "--output-schema", str(self.schema_path), "-o", str(output_path),
                "--ephemeral", "--skip-git-repo-check", "-",
            ]
            try:
                completed = self.runner(
                    command,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                with self._calls_lock:
                    self._calls.append(ExternalCall(self.provider, "planning", "planning_complexity", 0, 0, time.monotonic() - started, "unavailable"))
                raise ExternalUnavailable(str(exc)) from exc
            input_tokens, output_tokens = _usage(completed.stdout)
            status = "completed" if completed.returncode == 0 else "unavailable"
            with self._calls_lock:
                self._calls.append(
                    ExternalCall(self.provider, "planning", "planning_complexity", input_tokens, output_tokens, time.monotonic() - started, status)
                )
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout or "Codex CLI failed").strip()
                raise ExternalUnavailable(message[-2000:])
            if not output_path.is_file():
                raise ExternalUnavailable("Codex CLI did not produce a plan")
            return parse_plan(output_path.read_text(encoding="utf-8"))

    def drain_external_calls(self) -> tuple[ExternalCall, ...]:
        with self._calls_lock:
            calls = tuple(self._calls)
            self._calls.clear()
        return calls


class CodexCliConsultant:
    provider = "codex-cli"

    def __init__(
        self,
        *,
        project_root: str | Path,
        binary: str | Path | None = None,
        budget: CallBudget | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.binary = discover_codex_binary(binary)
        self.budget = budget or CallBudget()
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self._calls: list[ExternalCall] = []
        self._calls_lock = threading.Lock()

    def _run(self, purpose: str, reason: str, prompt: str, command: list[str], cwd: Path) -> str:
        self.budget.consume(purpose)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="molicoder-consult-") as temp:
            output_path = Path(temp) / "response.txt"
            command = [*command, "-o", str(output_path), "--ephemeral", "--skip-git-repo-check", "-"]
            try:
                completed = self.runner(
                    command,
                    cwd=cwd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                with self._calls_lock:
                    self._calls.append(ExternalCall(self.provider, purpose, reason, 0, 0, time.monotonic() - started, "unavailable"))
                raise ExternalUnavailable(str(exc)) from exc
            input_tokens, output_tokens = _usage(completed.stdout)
            status = "completed" if completed.returncode == 0 else "unavailable"
            with self._calls_lock:
                self._calls.append(
                    ExternalCall(self.provider, purpose, reason, input_tokens, output_tokens, time.monotonic() - started, status)
                )
            if completed.returncode != 0:
                raise ExternalUnavailable((completed.stderr or completed.stdout or "Codex CLI failed").strip()[-2000:])
            if not output_path.is_file():
                raise ExternalUnavailable("Codex CLI did not produce consultant output")
            return output_path.read_text(encoding="utf-8").strip()

    def rescue(self, failure_packet: str) -> str:
        command = [
            str(self.binary), "exec", "--ignore-user-config", "--ignore-rules", "--json",
            "-C", str(self.project_root), "-s", "read-only",
        ]
        prompt = (
            "Act as a read-only repair consultant. Diagnose the source-grounded failure packet and return "
            "one concrete implementation strategy for the local executor. Do not edit files.\n\n" + failure_packet
        )
        return self._run("rescue", "local_repair_exhausted", prompt, command, self.project_root)

    def review(self, integration_root: str | Path, base_ref: str) -> str:
        root = Path(integration_root).resolve()
        command = [
            str(self.binary), "exec", "review", "--ignore-user-config", "--ignore-rules", "--json",
            "--base", base_ref,
        ]
        return self._run(
            "review",
            "frontier_review",
            "Review for correctness, regressions, security issues, and missing tests. Do not edit files.",
            command,
            root,
        )

    def direct_patch(self, worktree: str | Path, approved_request: str) -> str:
        """Exceptional writable path; callers must obtain explicit user approval first."""
        root = Path(worktree).resolve()
        command = [
            str(self.binary), "exec", "--ignore-user-config", "--ignore-rules", "--json",
            "-C", str(root), "-s", "workspace-write",
        ]
        prompt = (
            "Apply only this explicitly user-approved, narrowly scoped patch. Run the repository's "
            "existing safe validation if available and do not push, publish, or deploy.\n\n" + approved_request
        )
        return self._run("rescue", "human_requested_direct_patch", prompt, command, root)

    def drain_external_calls(self) -> tuple[ExternalCall, ...]:
        with self._calls_lock:
            calls = tuple(self._calls)
            self._calls.clear()
        return calls


class FallbackPlanner:
    def __init__(self, primary: PlannerProvider, fallback: PlannerProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.provider = primary.provider
        self.fallback_reason = ""

    def plan(self, packet: ReconPacket) -> Plan:
        try:
            plan = self.primary.plan(packet)
            self.provider = self.primary.provider
            return plan
        except ExternalUnavailable as exc:
            self.fallback_reason = str(exc)
            self.provider = self.fallback.provider
            return self.fallback.plan(packet)

    def drain_external_calls(self) -> tuple[ExternalCall, ...]:
        drain = getattr(self.primary, "drain_external_calls", None)
        return tuple(drain()) if drain else ()


_HIGH_RISK_TERMS = {
    "auth", "authentication", "authorization", "crypto", "encryption", "migration",
    "payment", "payments", "permission", "security", "token", "billing",
}


class RoutedPlanner:
    def __init__(self, mode: str, *, local: PlannerProvider, external: PlannerProvider | None = None) -> None:
        self.mode = mode.upper()
        self.local = local
        self.external = external
        self.provider = local.provider

    def _use_external(self, packet: ReconPacket) -> bool:
        terms = set(packet.objective.lower().replace("/", " ").split())
        return bool(terms & _HIGH_RISK_TERMS) or len(packet.relevant_files) > 12 or bool(packet.open_questions)

    def plan(self, packet: ReconPacket) -> Plan:
        if self.mode == "MANUAL_PLAN":
            self.provider = "manual"
            return parse_plan(packet.objective)
        wants_external = self.mode == "FRONTIER_PLAN" or (self.mode == "AUTO" and self._use_external(packet))
        if wants_external and self.external is not None:
            planner = FallbackPlanner(self.external, self.local)
            plan = planner.plan(packet)
            self.provider = planner.provider
            self._fallback = planner
            return plan
        self.provider = self.local.provider
        return self.local.plan(packet)

    def drain_external_calls(self) -> tuple[ExternalCall, ...]:
        fallback = getattr(self, "_fallback", None)
        if fallback:
            return fallback.drain_external_calls()
        drain = getattr(self.external, "drain_external_calls", None)
        return tuple(drain()) if drain else ()
