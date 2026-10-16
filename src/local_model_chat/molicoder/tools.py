from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..attachments import MAX_FILE_BYTES, AttachmentError, is_allowed, load_attachment
from ..actions.receipts import ActionReviewRequired


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


class ToolParseError(ValueError):
    pass


_ARGUMENTS: dict[str, dict[str, type]] = {
    "list_dir": {"path": str},
    "read_file": {"path": str, "start": int, "end": int},
    "grep": {"pattern": str, "glob": str},
    "write_file": {"path": str, "content": str},
    "replace_in_file": {"path": str, "old": str, "new": str},
    "run_validation": {},
}

_REQUIRED: dict[str, set[str]] = {
    "list_dir": {"path"},
    "read_file": {"path"},
    "grep": {"pattern"},
    "write_file": {"path", "content"},
    "replace_in_file": {"path", "old", "new"},
    "run_validation": set(),
}

_SENSITIVE_NAMES = {
    ".npmrc", ".pypirc", "auth.json", "credentials.json", "id_ed25519", "id_rsa",
    "secrets.json", "tokens.json",
}

_EXTERNAL_ACTION_NAMES = {
    "deploy", "publish", "payment", "charge", "refund", "delete_remote",
    "destroy", "send_email", "external_action",
}


def is_sensitive_path(path: Path) -> bool:
    lowered_parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    return (
        ".git" in lowered_parts
        or ".molicoder" in lowered_parts
        or name == ".env"
        or name.startswith(".env.")
        or name in _SENSITIVE_NAMES
        or path.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}
    )


def _tool_schema(name: str, description: str, properties: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": sorted(_REQUIRED[name]),
                "additionalProperties": False,
            },
        },
    }


TOOL_DEFINITIONS = [
    _tool_schema("list_dir", "List files and directories inside the worktree.", {"path": {"type": "string"}}),
    _tool_schema(
        "read_file",
        "Read a UTF-8 text file, optionally by inclusive line range.",
        {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
    ),
    _tool_schema(
        "grep",
        "Search text files with a regular expression.",
        {"pattern": {"type": "string"}, "glob": {"type": "string"}},
    ),
    _tool_schema(
        "write_file",
        "Write the complete UTF-8 contents of a text file.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
    ),
    _tool_schema(
        "replace_in_file",
        "Replace one exact, unique string in a UTF-8 text file.",
        {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
    ),
    _tool_schema("run_validation", "Run the project's fixed validation commands.", {}),
]


def _validate_call(value: Any) -> ToolCall:
    if not isinstance(value, dict):
        raise ToolParseError("tool call must be an object")
    function = value.get("function", value)
    if not isinstance(function, dict):
        raise ToolParseError("tool call function must be an object")
    name = function.get("name")
    if isinstance(name, str) and name in _EXTERNAL_ACTION_NAMES:
        raise ActionReviewRequired("unsupported external action requires user review")
    if not isinstance(name, str) or name not in _ARGUMENTS:
        raise ToolParseError(f"unknown tool: {name!r}")
    arguments = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ToolParseError("tool arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise ToolParseError("tool arguments must be an object")
    missing = _REQUIRED[name] - arguments.keys()
    if missing:
        raise ToolParseError(f"missing required argument: {sorted(missing)[0]}")
    unknown = arguments.keys() - _ARGUMENTS[name].keys()
    if unknown:
        raise ToolParseError(f"unknown argument for {name}: {sorted(unknown)[0]}")
    for argument, expected_type in _ARGUMENTS[name].items():
        if argument in arguments and type(arguments[argument]) is not expected_type:
            raise ToolParseError(f"argument {argument} must be {expected_type.__name__}")
    if name == "read_file":
        start = arguments.get("start", 1)
        end = arguments.get("end")
        if start < 1 or (end is not None and end < start):
            raise ToolParseError("read_file line range is invalid")
    return ToolCall(name, arguments)


def _content_json(content: str) -> Any | None:
    candidate = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    elif embedded_fence := re.search(r"```json\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE):
        candidate = embedded_fence.group(1).strip()
    wrapped = re.fullmatch(r"<tool_call>\s*(.*?)\s*</tool_call>", candidate, flags=re.DOTALL | re.IGNORECASE)
    if wrapped:
        candidate = wrapped.group(1).strip()
    elif embedded_wrapper := re.search(
        r"<tool_call>\s*(.*?)\s*</tool_call>", candidate, flags=re.DOTALL | re.IGNORECASE
    ):
        candidate = embedded_wrapper.group(1).strip()
    if not candidate.startswith(("{", "[")):
        return None
    decoder = json.JSONDecoder()
    values: list[Any] = []
    offset = 0
    try:
        while offset < len(candidate):
            while offset < len(candidate) and candidate[offset].isspace():
                offset += 1
            if offset == len(candidate):
                break
            value, offset = decoder.raw_decode(candidate, offset)
            values.append(value)
    except json.JSONDecodeError as exc:
        raise ToolParseError("tool call content is not valid JSON") from exc
    if len(values) == 1:
        return values[0]
    return values


def _embedded_tool_values(content: str) -> list[Any]:
    """Recover strict JSON calls from the common prose/code style of small local models."""
    decoder = json.JSONDecoder()
    located: list[tuple[int, int, Any]] = []
    function_names = "|".join(re.escape(name) for name in _ARGUMENTS)
    for match in re.finditer(rf"\b({function_names})\s*\(\s*", content):
        try:
            arguments, end = decoder.raw_decode(content, match.end())
        except json.JSONDecodeError:
            continue
        if isinstance(arguments, dict):
            located.append(
                (match.start(), end, {"name": match.group(1), "arguments": arguments})
            )

    function_spans = [(start, end) for start, end, _value in located]
    for start, character in enumerate(content):
        if character != "{" or any(left <= start < right for left, right in function_spans):
            continue
        try:
            value, end = decoder.raw_decode(content, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and ("name" in value or "function" in value):
            located.append((start, end, value))

    located.sort(key=lambda item: item[0])
    return [value for _start, _end, value in located]


def parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    structured = message.get("tool_calls")
    if structured is not None:
        if not isinstance(structured, list):
            raise ToolParseError("message.tool_calls must be a list")
        return [_validate_call(item) for item in structured]
    content = str(message.get("content", ""))
    decoded = _content_json(content)
    if decoded is None:
        values = _embedded_tool_values(content)
        return [_validate_call(item) for item in values]
    values = decoded if isinstance(decoded, list) else [decoded]
    return [_validate_call(item) for item in values]


class ToolDispatcher:
    def __init__(self, root: str | Path, *, validation: Callable[[], str]) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"worktree does not exist: {self.root}")
        self.validation = validation

    def _resolve(self, value: str) -> Path:
        path = (self.root / value).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError(f"path is outside the worktree: {value}") from exc
        if is_sensitive_path(path.relative_to(self.root)):
            raise PermissionError(f"access to sensitive path is denied: {value}")
        return path

    def dispatch(self, call: ToolCall) -> str:
        # Validate calls created programmatically as well as calls parsed from model output.
        call = _validate_call({"name": call.name, "arguments": call.arguments})
        handler = getattr(self, f"_do_{call.name}")
        return str(handler(**call.arguments))

    def _do_list_dir(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_dir():
            raise FileNotFoundError(f"directory not found: {path}")
        entries = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        return "\n".join(f"{'dir' if item.is_dir() else 'file'}\t{item.relative_to(self.root)}" for item in entries[:500])

    def _do_read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        target = self._resolve(path)
        attachment = load_attachment(target)
        lines = attachment.text.splitlines(keepends=True)
        return "".join(lines[start - 1:end])

    def _do_grep(self, pattern: str, glob: str = "*") -> str:
        try:
            expression = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        matches: list[str] = []
        for path in self.root.rglob(glob):
            try:
                safe_path = self._resolve(str(path.relative_to(self.root)))
            except (PermissionError, ValueError):
                continue
            if not safe_path.is_file() or not is_allowed(safe_path):
                continue
            try:
                text = load_attachment(safe_path).text
            except AttachmentError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if expression.search(line):
                    matches.append(f"{path.relative_to(self.root)}:{number}:{line}")
                    if len(matches) >= 200:
                        return "\n".join(matches)
        return "\n".join(matches)

    def _do_write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        if not is_allowed(target):
            raise ValueError(f"unsupported text file type: {target.name}")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("file exceeds the 1 MB write limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        return f"wrote {len(content.encode('utf-8'))} bytes to {target.relative_to(self.root)}"

    def _do_replace_in_file(self, path: str, old: str, new: str) -> str:
        if not old:
            raise ValueError("old text must not be empty")
        target = self._resolve(path)
        attachment = load_attachment(target)
        occurrences = attachment.text.count(old)
        if occurrences != 1:
            raise ValueError(f"expected one exact match in {path}, found {occurrences}")
        content = attachment.text.replace(old, new, 1)
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("replacement would exceed the 1 MB write limit")
        # load_attachment preserves the file's original newline bytes. Disable Python's
        # platform translation so an existing CRLF is not written back as CRCRLF.
        target.write_text(content, encoding="utf-8", newline="")
        return f"replaced one exact match in {target.relative_to(self.root)}"

    def _do_run_validation(self) -> str:
        return self.validation()
