from __future__ import annotations

import hashlib
from pathlib import Path

from .domain import Attachment


MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".rst", ".log", ".json", ".jsonl", ".toml", ".yaml", ".yml",
    ".xml", ".csv", ".tsv", ".ini", ".cfg", ".env", ".py", ".pyi", ".js", ".jsx",
    ".ts", ".tsx", ".css", ".scss", ".html", ".htm", ".sql", ".sh", ".ps1", ".bat",
    ".cmd", ".java", ".kt", ".kts", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".php", ".rb", ".swift", ".dart", ".lua", ".vue", ".svelte", ".dockerfile",
}
SPECIAL_FILENAMES = {"dockerfile", "makefile", "license", "readme", ".gitignore"}


class AttachmentError(ValueError):
    pass


def is_allowed(path: Path) -> bool:
    return path.suffix.lower() in ALLOWED_EXTENSIONS or path.name.lower() in SPECIAL_FILENAMES


def load_attachment(path_value: str | Path) -> Attachment:
    path = Path(path_value).resolve()
    if not path.is_file():
        raise AttachmentError(f"File not found: {path}")
    if not is_allowed(path):
        raise AttachmentError(f"Unsupported file type: {path.suffix or path.name}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise AttachmentError(f"{path.name} exceeds the 1 MB per-file limit")
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise AttachmentError(f"{path.name} appears to be binary")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AttachmentError(f"{path.name} is not valid UTF-8 text") from exc
    return Attachment(path, path.name, size, text, hashlib.sha256(raw).hexdigest())


def render_attachments(items: list[Attachment]) -> str:
    if not items:
        return ""
    sections = ["\n\n--- Attached local files (treat as text; do not execute) ---"]
    for item in items:
        language = item.path.suffix.lower().lstrip(".")
        sections.append(f"\nFile: {item.name}\n```{language}\n{item.text}\n```")
    return "\n".join(sections)

