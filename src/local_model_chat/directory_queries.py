"""Explicit, read-only folder listing: no shell, recursion or file-content reads."""
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import time

from .domain import Project


@dataclass(frozen=True)
class DirectoryQuestion:
    target: str


@dataclass(frozen=True)
class DirectoryListing:
    content: str
    target: str
    outcome: str
    entries: int = 0
    truncated: bool = False
    checked_at: str = ''


def directory_question(message: str) -> DirectoryQuestion | None:
    text = message.strip()
    acknowledgment = (r"^(?:ok(?:ay)?|all right|alright|good|great|thanks|thank you|well)"
                      r"(?:\s+(?:that['’]s good|sounds good|great|thanks))?"
                      r"(?:[,!.]\s*|\s+(?=(?:can|could|would|please|tell|show|list|what|which)\b))")
    for _ in range(3):
        text = re.sub(acknowledgment, '', text, count=1, flags=re.I)
    text = re.sub(r'^(?:(?:can|could|would) you\s+(?:please\s+)?|please\s+)', '', text, flags=re.I)
    text = re.sub(r'\s+please[?.!]?$', '', text, flags=re.I).rstrip('?').strip()
    noun = r'(?:contents?|files(?: and folders)?|folders(?: and files)?|items|directories|entries)'
    query = r'(?:show(?: me)?|list|display|tell me|give me|get me)(?: (?:a|the))?(?: (?:list|listing) of)?(?: (?:all|the))*'
    patterns = (
        rf"(?:(?:tell|show) me )?(?:what|which)\s+(?:(?:are|is)\s+)?(?:the\s+)?{noun}\s+(?:(?:are|is)\s+)?(?:in|inside|under|of)\s+(.+)",
        r"(?:(?:tell|show) me )?what(?:['’]s| is)\s+(?:in|inside|under)\s+(.+)",
        rf"{query}\s+{noun}\s+(?:of|in|inside|under)\s+(.+)",
        r"(?:show(?: me)?|list|dir|ls)\s+(.+)",
    )
    for index, pattern in enumerate(patterns):
        match = re.fullmatch(pattern, text, re.I)
        if not match:
            continue
        target = match[1].strip()
        target = re.sub(r'^(?:(?:the|a) )?(?:folder|directory|path)\s+', '', target, flags=re.I)
        quoted = target[:1] in {'"', "'", '“'}
        if quoted:
            closing = '”' if target.startswith('“') else target[0]
            if not target.endswith(closing) or len(target) < 3:
                return None
            target = target[1:-1]
        project_reference = re.fullmatch(r'(?:the )?(?:(?:my|our|this|current|selected) )?(?:project(?: folder)?|folder)', target, re.I)
        if project_reference:
            target = '.'
        # A generic "list ideas" remains ordinary conversation.
        if index in {1, 3} and not project_reference and not quoted and not re.match(r'^(?:[A-Za-z]:[/\\]|[/\\~.]|[^\s:/\\]+[/\\])', target):
            continue
        return DirectoryQuestion(target)
    return None


def directory_target(question: DirectoryQuestion, project: Project | None) -> Path:
    target = question.target
    if '\x00' in target or target.startswith(('\\\\', '//')):
        raise ValueError('Choose a local folder path; network and device paths are not supported by this listing action.')
    if re.match(r'^[A-Za-z]:', target) and os.name != 'nt':
        raise ValueError('That Windows drive path is unavailable on this platform. Choose a local folder path.')
    try:
        path = Path(target).expanduser()
    except RuntimeError as exc:
        raise ValueError('The home-folder path could not be resolved. Provide a full local folder path.') from exc
    if not path.is_absolute():
        if re.match(r'^[A-Za-z]:', target):
            raise ValueError('Use a full drive path such as C:/Dev, rather than a drive-relative path.')
        if project is None:
            raise ValueError('Provide an absolute folder path, or select a project for a project-relative listing.')
        path = Path(project.path) / path
    return path


def _literal(text: str) -> str:
    text = json.dumps(text, ensure_ascii=False)
    fence = '`' * (max((len(run) for run in re.findall(r'`+', text)), default=0) + 1)
    return f'{fence} {text} {fence}'


def list_directory(path: Path, *, cancelled=lambda: False, max_entries=100, max_chars=16000, max_seconds=2.0) -> DirectoryListing:
    checked = datetime.now().astimezone().isoformat(timespec='seconds')
    target = str(path)
    if cancelled():
        return DirectoryListing('Folder listing cancelled.', target, 'cancelled', checked_at=checked)
    rows = []
    truncated = False
    started = time.monotonic()
    size = 0
    try:
        # Keep source paths absolute without following child links.
        target = str(path.absolute())
        with os.scandir(path) as entries:
            for entry in entries:
                if cancelled():
                    return DirectoryListing('Folder listing cancelled.', target, 'cancelled', checked_at=checked)
                if len(rows) >= max_entries or time.monotonic() - started >= max_seconds:
                    truncated = True
                    break
                kind = 'Link' if entry.is_symlink() else 'Folder' if entry.is_dir(follow_symlinks=False) else 'File'
                row = f'- {kind}: {_literal(entry.name)}'
                if size + len(row) > max_chars:
                    truncated = True
                    break
                rows.append((kind, entry.name.casefold(), row))
                size += len(row)
    except OSError as exc:
        reason = ('Folder not found' if isinstance(exc, FileNotFoundError) else
                  'This path is a file, not a folder' if isinstance(exc, NotADirectoryError) else
                  'Access denied' if isinstance(exc, PermissionError) else str(exc)[:400])
        return DirectoryListing(f'Could not list {_literal(target)}: {reason}.', target, 'failed', checked_at=checked)
    rows.sort(key=lambda row: (row[0] != 'Folder', row[1]))
    detail = '\n'.join(row[2] for row in rows) or ('No entries displayed before the listing limit was reached.' if truncated else 'This folder is empty.')
    if truncated:
        detail += f'\n\nListing limited; showing {len(rows)} entries. More entries may exist.'
    content = (f'**Contents of {_literal(target)}**\n\n{detail}\n\n'
               f'Checked: {checked}. Source: read-only local folder listing. Immediate entries only; no file contents read.')
    return DirectoryListing(content, target, 'completed', len(rows), truncated, checked)


def directory_followup(message: str) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:can you|could you|please)\s+(?:do (?:it|that)(?: now)?|try (?:it |that )?again)[?.!]*\s*",
        message, re.I))
