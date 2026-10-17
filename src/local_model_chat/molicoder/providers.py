from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from .planner.schema import Plan, PlanValidationError, parse_plan, validate_provider_schema
from .recon import ReconPacket


ProviderEventHandler = Callable[[str, dict], None]


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider: str
    installed: bool
    authenticated: bool
    version: str = ""
    model: str = "CLI default"
    detail: str = ""
    binary: str = ""
    auth_verified: bool = False


@dataclass(frozen=True, slots=True)
class ProviderResult:
    status: str
    message: str
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    model: str = ""
    error: str = ""
    events: tuple[dict, ...] = field(default_factory=tuple)
    model_source: str = 'unknown'


@dataclass(frozen=True, slots=True)
class AgentProfile:
    provider: str
    route_class: str
    auth_guide: str
    terms_url: str
    permission_model: str
    cancellation: str
    selectable: bool
    limitation: str
    sandbox: str = ""
    capabilities: tuple[str, ...] = ()


AGENT_PROFILES = (
    AgentProfile(
        "codex", "opaque_agent", "https://developers.openai.com/codex/cli/",
        "https://openai.com/policies/service-terms/", "CLI sandbox, read-only plan and worktree execution",
        "terminate process group", True, "Internal token use and subscription balance are unknown.",
        "Codex read-only planning and workspace-write execution inside a Git worktree; external side effects are not independently intercepted.",
        ("planning", "editing", "streamed_events", "cancellation"),
    ),
    AgentProfile(
        "claude", "opaque_agent", "https://docs.anthropic.com/en/docs/claude-code/setup",
        "https://www.anthropic.com/legal/consumer-terms", "CLI plan mode and scoped edit tools",
        "terminate process group", True, "Internal token use and subscription balance are unknown.",
        "CLI tool permissions inside a Git worktree; no separate operating-system sandbox is verified.",
        ("planning", "editing", "streamed_events", "cancellation"),
    ),
    AgentProfile(
        "copilot", "opaque_agent", "https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli",
        "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service",
        "CLI read/write tool allowlist", "terminate process group", True,
        "Internal token use and subscription balance are unknown.",
        "CLI tool permissions inside a Git worktree; no separate operating-system sandbox is verified.",
        ("planning", "editing", "cancellation"),
    ),
    AgentProfile(
        "opencode", "opaque_agent", "https://opencode.ai/docs/providers/",
        "", "Deny-default local gateway profile",
        "terminate process group", True,
        "Account terms and privacy depend on the selected provider/model; free price is rechecked before use.",
        "Deny-default tool permissions; independent operating-system isolation is unverified.",
        ("planning", "editing", "local_gateway_preview"),
    ),
    AgentProfile(
        "kilo", "opaque_agent", "https://kilo.ai/docs/code-with-ai/platforms/cli",
        "", "Deny-default permissions and explicit provider/model", "terminate process group", True,
        "Auto Free can rotate underlying providers; project trust is required and CLI internal routing can remain unknown.",
        "CLI permissions; independent operating-system isolation unverified.",
        ("planning", "editing", "streamed_events", "cancellation"),
    ),
)


def agent_profile(name: str) -> AgentProfile:
    for profile in AGENT_PROFILES:
        if profile.provider == name:
            return profile
    raise ValueError(f"Unsupported agent profile: {name}")


class FrontierProvider(Protocol):
    name: str
    model: str

    def probe(self) -> ProviderStatus: ...
    def plan(self, packet: ReconPacket) -> Plan: ...
    def execute(self, worktree: str | Path, prompt: str, *, timeout_seconds: float = 1200.0) -> ProviderResult: ...
    def cancel(self) -> None: ...


class ProviderUnavailable(RuntimeError):
    pass


class ProviderExecutionError(RuntimeError):
    pass


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|secret)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_A-Za-z0-9]{10,}\b"),
)


def redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", result)
    return result


def _safe_environment() -> dict[str, str]:
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "LOCALAPPDATA", "APPDATA", "USERPROFILE", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "PROGRAMDATA", "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "TERM", "COLORTERM",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment["NO_COLOR"] = "1"
    environment["CI"] = "1"
    return environment


def _usage_from_events(events: list[dict]) -> tuple[int, int, str]:
    input_tokens = output_tokens = 0
    model = ""
    native_input = native_output = 0
    seen_steps = set()
    for event in events:
        part = event.get('part', {})
        if isinstance(part, dict) and part.get('type') == 'step-finish' and part.get('id') not in seen_steps:
            seen_steps.add(part.get('id'))
            tokens = part.get('tokens', {})
            cache = tokens.get('cache', {})
            native_input += int(tokens.get('input', 0) or 0) + int(cache.get('read', 0) or 0) + int(cache.get('write', 0) or 0)
            native_output += int(tokens.get('output', 0) or 0) + int(tokens.get('reasoning', 0) or 0)
        candidates = [event]
        if isinstance(event.get("result"), dict):
            candidates.append(event["result"])
        if isinstance(event.get("message"), dict):
            candidates.append(event["message"])
        for candidate in candidates:
            usage = candidate.get("usage")
            if isinstance(usage, dict):
                input_tokens = max(input_tokens, int(usage.get("input_tokens", usage.get("inputTokens", 0)) or 0))
                output_tokens = max(output_tokens, int(usage.get("output_tokens", usage.get("outputTokens", 0)) or 0))
            if isinstance(candidate.get("model"), str):
                model = candidate["model"]
    return max(input_tokens, native_input), max(output_tokens, native_output), model


def _extract_plan_payload(text: str) -> str | dict:
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(payload, dict):
        structured = payload.get("structured_output")
        if isinstance(structured, dict):
            return structured
        result = payload.get("result")
        if isinstance(result, dict):
            structured = result.get("structured_output")
            if isinstance(structured, dict):
                return structured
        if isinstance(result, str):
            return result
        message = payload.get("message")
        if isinstance(message, str):
            return message
        if "objective" in payload and "tasks" in payload:
            return payload
    return stripped


def _bounded_payload(value, *, string_limit: int = 12000):
    if isinstance(value, str):
        return value if len(value) <= string_limit else value[:string_limit] + "… [truncated]"
    if isinstance(value, dict):
        return {str(key): _bounded_payload(item, string_limit=string_limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_bounded_payload(item, string_limit=string_limit) for item in value[:100]]
    return value


class CliFrontierProvider:
    name = "cli"
    display_name = "CLI"

    def __init__(
        self,
        *,
        binary: str | Path | None = None,
        model: str = "",
        on_event: ProviderEventHandler | None = None,
        search_path: str | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        self._explicit_binary = Path(binary).resolve() if binary else None
        self.model = model.strip()
        self.on_event = on_event
        self.search_path = search_path
        self.project_root = Path(project_root).resolve() if project_root else None
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = threading.Event()
        self._lock = threading.Lock()

    @property
    def provider(self) -> str:
        return self.name

    def discover_binary(self) -> Path | None:
        if self._explicit_binary:
            return self._explicit_binary if self._explicit_binary.is_file() else None
        found = shutil.which(self.name, path=self.search_path)
        if found:
            return Path(found).resolve()
        if self.name == "opencode" and self.search_path is None:
            from ..environment.managed_tools import managed_executable

            return managed_executable("opencode")
        return None

    def version_command(self, binary: Path) -> list[str]:
        return [str(binary), "--version"]

    def auth_command(self, binary: Path) -> list[str] | None:
        return None

    def invocation_environment(self) -> dict[str, str]:
        return _safe_environment()

    def probe(self) -> ProviderStatus:
        binary = self.discover_binary()
        if binary is None:
            return ProviderStatus(
                self.name, False, False, model=self.model or "CLI default",
                detail=f"{self.display_name} is not installed.",
            )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        auth_verified = False
        try:
            version_result = subprocess.run(
                self.version_command(binary), capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10, check=False, creationflags=creationflags,
                env=_safe_environment(),
            )
            version = (version_result.stdout or version_result.stderr).strip().splitlines()[0][:200]
            authenticated = version_result.returncode == 0
            detail = "Installed; authentication will be verified when a build starts."
            auth_command = self.auth_command(binary)
            if auth_command:
                auth = subprocess.run(
                    auth_command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=15, check=False, creationflags=creationflags, env=_safe_environment(),
                )
                authenticated = auth.returncode == 0
                auth_verified = True
                detail = "Ready." if authenticated else redact_text((auth.stderr or auth.stdout).strip()[-500:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProviderStatus(
                self.name, True, False, model=self.model or "CLI default", detail=redact_text(str(exc)),
                binary=str(binary),
            )
        return ProviderStatus(
            self.name, True, authenticated, version, self.model or "CLI default", detail, str(binary),
            auth_verified,
        )

    def plan_command(self, root: Path) -> tuple[list[str], Path | None]:
        raise NotImplementedError

    def execute_command(self, worktree: Path) -> list[str]:
        raise NotImplementedError

    def _emit(self, kind: str, payload: dict) -> None:
        if self.on_event:
            self.on_event(kind, payload)

    def _invoke(
        self,
        command: list[str],
        prompt: str,
        *,
        cwd: Path,
        purpose: str,
        timeout_seconds: float,
    ) -> ProviderResult:
        # Cancellation belongs to this provider instance. A late probe must not
        # clear it and launch a new process after the user pressed Stop.
        if self._cancelled.is_set():
            return ProviderResult('cancelled', '', error='Cancelled before process launch')
        started = time.monotonic()
        events: list[dict] = []
        output: list[str] = []
        errors: list[str] = []
        binary = Path(command[0])
        if not binary.is_file() and shutil.which(command[0], path=self.search_path) is None:
            raise ProviderUnavailable(f"{self.display_name} is not installed")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs = {
            "cwd": cwd,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": self.invocation_environment(),
            "creationflags": creationflags,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
            kwargs.pop("creationflags")
        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            raise ProviderUnavailable(redact_text(str(exc))) from exc
        with self._lock:
            self._process = process
        self._emit("provider_started", {"provider": self.name, "purpose": purpose, "pid": process.pid})
        assert process.stdin and process.stdout and process.stderr
        def write_prompt():
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except (OSError, ValueError):
                pass
        lines: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def read_stream(label: str, stream) -> None:
            for line in iter(stream.readline, ""):
                lines.put((label, line.rstrip("\r\n")))
            lines.put((label, None))

        readers = [
            threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        writer = threading.Thread(target=write_prompt, daemon=True)
        writer.start()
        closed: set[str] = set()
        timed_out = False
        last_activity = started
        while len(closed) < 2 or process.poll() is None:
            if self._cancelled.is_set():
                self._terminate_process(process)
                break
            elapsed = time.monotonic() - started
            if elapsed > timeout_seconds:
                self._terminate_process(process)
                timed_out = True
                break
            try:
                label, line = lines.get(timeout=0.25)
            except queue.Empty:
                if time.monotonic() - last_activity >= 1.0:
                    self._emit("provider_heartbeat", {"provider": self.name, "purpose": purpose, "elapsed_seconds": round(elapsed, 1)})
                    last_activity = time.monotonic()
                continue
            if line is None:
                closed.add(label)
                continue
            last_activity = time.monotonic()
            clean = redact_text(line)
            stored_line = clean if len(clean) <= 200000 else clean[:200000] + "… [truncated]"
            (output if label == "stdout" else errors).append(stored_line)
            try:
                event = json.loads(clean)
            except json.JSONDecodeError:
                event = {"type": "text", "stream": label, "message": clean[:12000] + ("… [truncated]" if len(clean) > 12000 else "")}
            if isinstance(event, dict):
                event = _bounded_payload(event)
                events.append(event)
                self._emit("provider_output", {"provider": self.name, "purpose": purpose, "event": event})
        return_code = process.wait(timeout=5)
        with self._lock:
            self._process = None
        seconds = time.monotonic() - started
        input_tokens, output_tokens, reported_model = _usage_from_events(events)
        message = "\n".join(output).strip()
        error = "\n".join(errors).strip()
        if timed_out:
            error = f"{purpose} timed out after {timeout_seconds:.0f}s\n" + error
        if return_code != 0 and self.name == 'codex':
            from .planner.codex_cli import provider_failure
            error = provider_failure(message, error)
        status = "cancelled" if self._cancelled.is_set() else "completed" if return_code == 0 and not timed_out else "needs_attention"
        if status != "completed" and not error:
            error = message[-4000:] or f"{self.display_name} exited with code {return_code}"
        result = ProviderResult(
            status, message, input_tokens, output_tokens, seconds,
            reported_model or self.model, error[-4000:] if status != "completed" else "", tuple(events),
            model_source='reported' if reported_model else 'configured' if self.model else 'unknown',
        )
        self._emit("provider_finished", {
            "provider": self.name, "purpose": purpose, "status": status,
            "seconds": round(seconds, 3), "input_tokens": input_tokens or None,
            "output_tokens": output_tokens or None, "model": result.model or None,
            "model_source": "reported" if reported_model else "configured" if self.model else "unknown",
            "error": result.error,
        })
        return result

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._terminate_process(process)

    def _planning_prompt(self, packet: ReconPacket) -> str:
        return (
            "Create a source-grounded implementation plan as JSON matching the supplied schema. "
            "Use as many coherent, independently verifiable tasks as needed, up to thirty. "
            "Each task should be suitable for its own review/merge and include focused tests. "
            "Atomic does not mean single-file or microscopic. Declare dependencies. Do not edit files, invent paths "
            "as existing, or include dependency installation unless the evidence explicitly requires it. "
            "MoliCoder owns dependency manifests: do not plan manifest edits for ordinary application work. "
            "For an unspecified Python desktop UI use standard-library Tkinter; for an unspecified Node UI "
            "use dependency-free HTML, CSS, and JavaScript. Every task must make a concrete repository change; "
            "do not create validation-only, review-only, or manual smoke-test tasks because MoliCoder performs "
            "validation after implementation. For exact text files include artifact_checks with kind "
            "file_equals, project-relative path, and exact content. If a named text file has no requested "
            "contents, explicitly propose an empty file for approval and use empty content in the check. "
            "Use an empty artifact_checks array for tasks without exact artifact checks. "
            "Acceptance criteria are executable contracts: for a simple exact artifact task use only "
            "the literal criteria 'Artifact checks must pass' and 'Configured validation must pass'. "
            "Put the human-readable description in the task objective and the exact requirements in "
            "artifact_checks. Prose acceptance criteria require manual review. Use 'project-default' "
            "in verification_checks unless the evidence supplies a configured check identifier.\n\n"
            + packet.to_prompt()
        )

    def plan(self, packet: ReconPacket) -> Plan:
        binary = self.discover_binary()
        if binary is None:
            raise ProviderUnavailable(f"{self.display_name} is not installed")
        root = self.project_root or Path.cwd()
        prompt = self._planning_prompt(packet)
        last_error = ""
        previous = ""
        for attempt in range(1, 3):
            command, output_path = self.plan_command(root)
            result = self._invoke(
                command, prompt, cwd=root, purpose="planning", timeout_seconds=300.0,
            )
            if result.status != "completed":
                raise ProviderUnavailable(result.error or f"{self.display_name} planning failed")
            text = output_path.read_text(encoding="utf-8") if output_path and output_path.is_file() else result.message
            if output_path:
                output_path.unlink(missing_ok=True)
            try:
                return parse_plan(_extract_plan_payload(text))
            except PlanValidationError as exc:
                last_error = str(exc)
                previous = text[-8000:]
                if attempt == 1:
                    self._emit("plan_repair_started", {"provider": self.name, "reason": last_error})
                    prompt = (
                        self._planning_prompt(packet)
                        + f"\n\nYour previous response was invalid: {last_error}. Return a corrected JSON plan only."
                        + f"\n\nPrevious response:\n{previous}"
                    )
        raise ProviderExecutionError(last_error or "provider did not produce a valid plan")

    def analyze(self, root: Path, prompt: str, *, timeout_seconds: float = 90.0) -> ProviderResult:
        """Read-only answer invocation, independent of implementation/plan schemas."""
        output = None
        if self.name == 'codex':
            output = Path(tempfile.gettempdir()) / f'molicoder-analysis-{os.getpid()}-{time.time_ns()}.json'
            command = self._base(root, 'read-only') + ['-o', str(output), '--ephemeral', '--skip-git-repo-check', '-']
        elif self.name == 'claude':
            command = [str(self.discover_binary() or 'claude'), '-p', '--output-format', 'json',
                       '--permission-mode', 'plan', '--no-session-persistence', '--tools', 'Read,Glob,Grep',
                       '--allowedTools', 'Read,Glob,Grep']
            if self.model:
                command += ['--model', self.model]
        elif self.name == 'copilot':
            command, _ = self.plan_command(root)
        else:
            raise ProviderUnavailable('Read-only analysis is unavailable for this provider')
        try:
            result = self._invoke(command,
                'Answer this repository question without editing files, running commands or accessing credentials. '
                'Use only the supplied source evidence. Return the requested answer JSON, not a plan.\n\n' + prompt,
                cwd=root, purpose='repository_analysis', timeout_seconds=timeout_seconds)
            text = result.message
            if output and output.is_file():
                text = output.read_text(encoding='utf-8')
            else:
                for event in result.events:
                    if isinstance(event.get('result'), str):
                        text = event['result']
                    item = event.get('item', {})
                    if isinstance(item, dict) and item.get('type') == 'agent_message':
                        text = item.get('text', text)
            return replace(result, message=text)
        finally:
            if output:
                output.unlink(missing_ok=True)

    def execute(self, worktree: str | Path, prompt: str, *, timeout_seconds: float = 1200.0) -> ProviderResult:
        root = Path(worktree).resolve()
        command = self.execute_command(root)
        guardrails = (
            "You are implementing one approved task inside an isolated Git worktree. Inspect current files, "
            "make concrete edits, and stop when implementation is complete. Do not push, publish, deploy, "
            "access paths outside the worktree, change credentials, install dependencies, or edit dependency "
            "manifests. Use Tkinter for Python desktop UI and dependency-free browser code for Node UI unless "
            "the approved task explicitly names an existing framework. MoliCoder runs "
            "the authoritative validation after you exit.\n\n"
        )
        return self._invoke(command, guardrails + prompt, cwd=root, purpose="implementation", timeout_seconds=timeout_seconds)


class OpenCodeProvider(CliFrontierProvider):
    """Documented headless contract; activation waits for a reviewed route profile."""

    name = "opencode"
    display_name = "OpenCode"
    enforcement_class = 'scoped_tools'

    def set_task_scope(self, paths):
        if not hasattr(self, '_request_context'):
            self._request_context = threading.local()
        if not paths or any(Path(path).is_absolute() or '..' in Path(path).parts for path in paths):
            raise ValueError('Runtime editing requires explicit project-relative task paths')
        self._request_context.write_paths = tuple(paths)

    def _check_profile(self):
        if self.model in {'opencode/big-pickle', 'kilo/kilo-auto/free'}:
            status = self.probe()
            if not status.authenticated:
                raise ProviderUnavailable(status.detail)

    def plan(self, packet):
        self._check_profile()
        return super().plan(packet)

    def execute(self, worktree, prompt, *, timeout_seconds=1200):
        self._check_profile()
        return super().execute(worktree, prompt, timeout_seconds=timeout_seconds)

    def tool_permissions(self, purpose):
        reads = {'*': 'allow', '.env*': 'deny', '**/.env*': 'deny', '**/auth.json': 'deny',
                 '**/credentials.json': 'deny', '**/id_rsa': 'deny', '**/id_ed25519': 'deny',
                 '**/*.pem': 'deny', '**/.git/**': 'deny', '**/.molicoder/**': 'deny'}
        writes = {'*': 'deny'}
        if purpose == 'implementation':
            paths = getattr(getattr(self, '_request_context', None), 'write_paths', ('*',))
            for path in paths:
                writes[path] = 'allow'
                if path != '*':
                    writes[path.rstrip('/') + '/**'] = 'allow'
            for path in reads:
                if path != '*':
                    writes[path] = 'deny'
        return {'*': 'deny', 'read': reads, 'glob': 'allow', 'grep': 'allow', 'edit': writes,
                'external_directory': 'deny', 'webfetch': 'deny', 'websearch': 'deny', 'bash': 'deny'}

    def discover_binary(self):
        binary = super().discover_binary()
        if binary and not self._explicit_binary and os.name == 'nt' and self.name == 'opencode':
            native = binary.parent / 'node_modules' / 'opencode-ai' / 'bin' / 'opencode.exe'
            if native.is_file():
                return native.resolve()
        return binary

    def configure_local_gateway(self, endpoint: str, model_id: str, access_token: str) -> None:
        parsed = urlparse(endpoint)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or not parsed.port or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1"):
            raise ValueError("OpenCode gateway endpoint must be a loopback HTTP /v1 URL")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", model_id):
            raise ValueError("invalid local gateway model ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", access_token):
            raise ValueError("local gateway requires a bounded access token")
        self._gateway_endpoint = endpoint.rstrip("/")
        self._gateway_model_id = model_id
        self._gateway_access_token = access_token
        self.model = f"molicoder-local/{model_id}"

    def invocation_environment(self) -> dict[str, str]:
        environment = _safe_environment()
        environment["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        environment["OPENCODE_DISABLE_LSP_DOWNLOAD"] = "1"
        purpose = getattr(getattr(self, "_request_context", None), "purpose", "planning")
        environment["OPENCODE_PERMISSION"] = json.dumps(self.tool_permissions(purpose))
        if self.model:
            environment['OPENCODE_CONFIG_CONTENT'] = json.dumps({
                'model': self.model, 'small_model': self.model,
                'enabled_providers': [self.model.split('/', 1)[0]],
                'permission': json.loads(environment['OPENCODE_PERMISSION']),
                'agent': {name: {'model': self.model} for name in ('plan', 'build', 'code')},
            })
        if hasattr(self, "_gateway_endpoint"):
            request_id = getattr(getattr(self, "_request_context", None), "request_id", "")
            environment["OPENCODE_CONFIG_CONTENT"] = json.dumps({
                "enabled_providers": ["molicoder-local"],
                "model": self.model,
                "small_model": self.model,
                "provider": {"molicoder-local": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "MoliCoder local gateway",
                    "options": {
                        "baseURL": self._gateway_endpoint,
                        "apiKey": self._gateway_access_token,
                        "headers": {"X-MoliCoder-Request-ID": request_id} if request_id else {},
                    },
                    "models": {self._gateway_model_id: {"name": self._gateway_model_id}},
                }},
            }, sort_keys=True)
        return environment

    def probe(self) -> ProviderStatus:
        binary = self.discover_binary()
        if binary is None:
            return ProviderStatus(self.name, False, False, detail="OpenCode is not installed.")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            version = subprocess.run(
                [str(binary), "--version"], capture_output=True, text=True,
                timeout=10, check=False, creationflags=flags, env=self.invocation_environment(),
            )
            if version.returncode != 0:
                return ProviderStatus(self.name, True, False, detail="OpenCode version probe failed.", binary=str(binary))
            if hasattr(self, "_gateway_endpoint"):
                return ProviderStatus(
                    self.name, True, False, version.stdout.strip()[:100], self.model,
                    "Local gateway profile configured; endpoint health has not been verified.", str(binary),
                )
            free_defaults = {'opencode': 'opencode/big-pickle', 'kilo': 'kilo/kilo-auto/free'}
            if self.model == free_defaults.get(self.name):
                from ..orchestration.catalog import CapabilityCatalogService
                try:
                    facts = next((m for m in CapabilityCatalogService.runtime_models(self.name, binary, refresh=True)
                                  if m['model'] == self.model), None)
                except Exception:
                    facts = None
                free = bool(facts and all(type(facts['cost'].get(k)) in (int, float) and facts['cost'][k] == 0 for k in ('input', 'output')))
                return ProviderStatus(self.name, True, free, version.stdout.strip()[:100], self.model,
                    'Pinned free model advertised; inference remains authoritative for availability.' if free else
                    'The selected free model is unavailable or its zero price could not be confirmed.', str(binary), free)
            model_provider = self.model.split("/", 1)[0] if "/" in self.model else ""
            if not model_provider:
                return ProviderStatus(
                    self.name, True, False, version.stdout.strip()[:100],
                    detail="Installed; select and verify a provider/model profile before Build.", binary=str(binary),
                )
            accounts = subprocess.run(
                [str(binary), "auth", "list"], capture_output=True, text=True,
                timeout=10, check=False, creationflags=flags, env=self.invocation_environment(),
            )
            found = accounts.returncode == 0 and bool(re.search(
                rf"(?<![\w-]){re.escape(model_provider)}(?![\w-])", accounts.stdout, re.IGNORECASE,
            ))
            return ProviderStatus(
                self.name, True, found, version.stdout.strip()[:100], self.model,
                "Provider credential listed; entitlement unverified." if found else "Provider login not detected.",
                str(binary),
            )
        except (OSError, subprocess.TimeoutExpired):
            return ProviderStatus(self.name, True, False, detail="OpenCode probe failed.", binary=str(binary))

    def _base(self, root: Path, agent: str) -> list[str]:
        binary = self.discover_binary()
        command = [str(binary) if binary else "opencode", "--pure", "run", "--format", "json",
                   "--agent", agent, "--dir", str(root.resolve())]
        if self.model:
            command.extend(["--model", self.model])
        if getattr(self, 'effort', None):
            command.extend(['--variant', self.effort])
        return command

    def plan_command(self, root: Path) -> tuple[list[str], Path | None]:
        return self._base(root, "plan"), None

    def execute_command(self, worktree: Path) -> list[str]:
        return self._base(worktree, "build")

    def analyze(self, root, prompt, *, timeout_seconds=90):
        self._check_profile()
        return self._invoke(self._base(Path(root), 'plan'), prompt, cwd=Path(root),
                            purpose='repository_analysis', timeout_seconds=timeout_seconds)

    def _invoke(self, command: list[str], prompt: str, *, cwd: Path, purpose: str,
                timeout_seconds: float) -> ProviderResult:
        request_id = str(uuid4())
        if not hasattr(self, "_request_context"):
            self._request_context = threading.local()
        self._request_context.request_id = request_id
        self._request_context.purpose = purpose
        self._emit("provider_request", {"provider": self.name, "request_id": request_id, "purpose": purpose})
        try:
            result = super()._invoke(
                [*command, "--title", f"MoliCoder {request_id}"], prompt, cwd=cwd,
                purpose=purpose, timeout_seconds=timeout_seconds,
            )
        finally:
            self._request_context.request_id = ""
            self._request_context.purpose = "planning"
        text_parts = []
        for event in result.events:
            part = event.get("part") if isinstance(event.get("part"), dict) else event
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
        message = ''.join(text_parts) or result.message
        # Some hosted reasoning profiles leak their transport delimiter into a
        # text event instead of separating the final answer from reasoning.
        # Only the final segment is an answer; the caller still validates its
        # complete JSON schema before any proposed tool can execute.
        if '</think>' in message:
            final_segment = message.rsplit('</think>', 1)[1].strip()
            if final_segment:
                message = final_segment
        fence = re.fullmatch(r'\s*```(?:json)?\s*(.*?)\s*```\s*', message, flags=re.DOTALL)
        if fence:
            try:
                json.loads(fence[1])
                message = fence[1]
            except ValueError:
                pass
        return replace(result, message=message)


class KiloProvider(OpenCodeProvider):
    """Kilo 7 CLI contract: explicit model, JSON events, scoped permission policy."""
    name = "kilo"
    display_name = "Kilo Code"

    def discover_binary(self):
        binary = super().discover_binary()
        if binary and not self._explicit_binary and os.name == 'nt':
            native = binary.parent / 'node_modules' / '@kilocode' / 'cli' / 'node_modules' / '@kilocode' / 'cli-windows-x64' / 'bin' / 'kilo.exe'
            if native.is_file():
                return native.resolve()
        return binary

    def invocation_environment(self):
        environment = super().invocation_environment()
        purpose = getattr(getattr(self, '_request_context', None), 'purpose', 'planning')
        permission = self.tool_permissions(purpose)
        config = json.loads(environment.pop('OPENCODE_CONFIG_CONTENT', '{}'))
        config['permission'] = permission
        environment['KILO_CONFIG_CONTENT'] = json.dumps(config)
        environment['KILO_DISABLE_PROJECT_CONFIG'] = '1'
        return environment

    def _base(self, root, agent):
        binary = self.discover_binary()
        command = [str(binary) if binary else 'kilo', '--pure', 'run', '--format', 'json',
                   '--agent', 'code' if agent == 'build' else 'plan', '--dir', str(root.resolve())]
        if self.model:
            command.extend(['--model', self.model])
        effort = getattr(self, 'effort', None)
        if effort:
            command.extend(['--variant', effort])
        return command

    def analyze(self, root, prompt, *, timeout_seconds=90):
        self._check_profile()
        return self._invoke(self._base(Path(root), 'plan'), prompt, cwd=Path(root),
                            purpose='repository_analysis', timeout_seconds=timeout_seconds)


class CodexProvider(CliFrontierProvider):
    name = "codex"
    display_name = "Codex CLI"

    def discover_binary(self) -> Path | None:
        if self._explicit_binary:
            return super().discover_binary()
        bin_root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        candidates = [path for path in bin_root.glob("**/codex.exe") if path.is_file()]
        if candidates:
            # Codex Desktop keeps a compatibility binary at bin/codex.exe and
            # its current runtime under a versioned child directory. Prefer
            # the newest runtime so CLI/schema capabilities match the app.
            return max(candidates, key=lambda path: path.stat().st_mtime_ns).resolve()
        return super().discover_binary()

    def auth_command(self, binary: Path) -> list[str] | None:
        # `codex login status` loads the user's full config and can fail on an
        # unrelated stale preference even though `codex exec --ignore-user-config`
        # is authenticated and usable. Builds perform the authoritative check.
        return None

    def _base(self, root: Path, sandbox: str, *, writable: bool = False) -> list[str]:
        binary = self.discover_binary()
        if binary is None:
            return ["codex"]
        command = [str(binary)]
        if writable:
            # This option itself selects workspace-write and automatic review;
            # Codex rejects combining it with an explicit --sandbox flag.
            command.append("--approve-for-me")
        else:
            command.extend(["-a", "never", "-s", sandbox])
        if getattr(self, 'effort', None):
            command.extend(['-c', 'model_reasoning_effort=' + json.dumps(self.effort)])
        command.extend([
            "exec", "--ignore-user-config", "--ignore-rules", "--json",
            "-C", str(root.resolve()),
        ])
        if self.model:
            command.extend(["-m", self.model])
        return command

    def plan_command(self, root: Path) -> tuple[list[str], Path | None]:
        output = Path(tempfile.gettempdir()) / f"molicoder-codex-plan-{os.getpid()}-{time.time_ns()}.json"
        schema = Path(__file__).parent / "planner" / "plan.schema.json"
        validate_provider_schema(json.loads(schema.read_text(encoding="utf-8")))
        return (
            self._base(root, "read-only")
            + ["--output-schema", str(schema), "-o", str(output), "--ephemeral", "--skip-git-repo-check", "-"],
            output,
        )

    def execute_command(self, worktree: Path) -> list[str]:
        return self._base(worktree, "workspace-write", writable=True) + ["--ephemeral", "--skip-git-repo-check", "-"]


class ClaudeProvider(CliFrontierProvider):
    name = "claude"
    display_name = "Claude Code"

    def auth_command(self, binary: Path) -> list[str] | None:
        return [str(binary), "auth", "status"]

    def plan_command(self, root: Path) -> tuple[list[str], Path | None]:
        binary = self.discover_binary()
        schema = json.loads((Path(__file__).parent / "planner" / "plan.schema.json").read_text(encoding="utf-8"))
        # Claude Code's bundled validator supports the schema keywords used by
        # the plan but rejects the draft URI metadata itself.
        schema.pop("$schema", None)
        command = [
            str(binary or "claude"), "-p", "--output-format", "json", "--permission-mode", "plan",
            "--no-session-persistence", "--json-schema",
            json.dumps(schema, separators=(",", ":")),
        ]
        if self.model:
            command.extend(["--model", self.model])
        return command, None

    def execute_command(self, worktree: Path) -> list[str]:
        binary = self.discover_binary()
        command = [
            str(binary or "claude"), "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits", "--no-session-persistence",
            "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(git status*),Bash(git diff*),Bash(pytest*),Bash(python -m pytest*),Bash(npm test*),Bash(npm run build*),Bash(flutter analyze*),Bash(flutter test*)",
        ]
        if self.model:
            command.extend(["--model", self.model])
        return command


class CopilotProvider(CliFrontierProvider):
    name = "copilot"
    display_name = "GitHub Copilot CLI"

    def plan_command(self, root: Path) -> tuple[list[str], Path | None]:
        binary = self.discover_binary()
        command = [str(binary or "copilot"), "-p", "-", "--no-ask-user", "--deny-tool", "shell", "--deny-tool", "write"]
        if self.model:
            command.extend(["--model", self.model])
        return command, None

    def execute_command(self, worktree: Path) -> list[str]:
        binary = self.discover_binary()
        command = [
            str(binary or "copilot"), "-p", "-", "--no-ask-user",
            "--allow-tool", "read", "--allow-tool", "write",
            "--allow-tool", "shell(git status*)", "--allow-tool", "shell(git diff*)",
            "--allow-tool", "shell(pytest*)", "--allow-tool", "shell(python -m pytest*)",
            "--allow-tool", "shell(npm test*)", "--allow-tool", "shell(npm run build*)",
            "--allow-tool", "shell(flutter analyze*)", "--allow-tool", "shell(flutter test*)",
            "--deny-tool", "url", "--deny-tool", "memory",
        ]
        if self.model:
            command.extend(["--model", self.model])
        return command


def create_provider(
    name: str,
    *,
    model: str = "",
    on_event: ProviderEventHandler | None = None,
    binary: str | Path | None = None,
    project_root: str | Path | None = None,
) -> CliFrontierProvider:
    providers = {"codex": CodexProvider, "claude": ClaudeProvider, "copilot": CopilotProvider,
                 "opencode": OpenCodeProvider, "kilo": KiloProvider}
    try:
        provider_type = providers[name.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unsupported frontier provider: {name}") from exc
    if not model and name in {'opencode', 'kilo'}:
        model = {'opencode': 'opencode/big-pickle', 'kilo': 'kilo/kilo-auto/free'}[name]
    return provider_type(binary=binary, model=model, on_event=on_event, project_root=project_root)


def probe_all_providers() -> tuple[ProviderStatus, ...]:
    return (*tuple(create_provider(name).probe() for name in ("codex", "claude", "copilot")),
            OpenCodeProvider().probe(), KiloProvider().probe())
