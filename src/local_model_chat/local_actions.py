from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_FOLDER_REQUEST = re.compile(
    r'^\s*(?:(?:oh that.s fine,\s*)?(?:can you|could you|please)\s+)?'
    r'create\s+(?:a\s+)?folder\s+(?:in|under)\s+'
    r'(?P<parent>\S+)\s+called\s+["“](?P<name>[^"”]+)["”]\s*[.!?]?\s*$',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FolderIntent:
    target: Path | None = None
    error: str = ''


def folder_intent(message: str) -> FolderIntent | None:
    prefix = r'''\s*(?:(?:let['’]s|please|can you|could you)\s+)?create\s+(?:a\s+)?(?:new\s+)?folder\b'''
    recognized = re.match(prefix, message, re.IGNORECASE)
    match = _FOLDER_REQUEST.fullmatch(message)
    if match:
        parent_text, name = match.group('parent', 'name')
    elif recognized:
        rest = message[recognized.end():].strip()
        named = re.fullmatch(r'''(?:called|named)\s+["“](?P<name>[^"”]+)["”]\s+(?:in|under)\s+(?P<parent>.+)''', rest, re.I)
        if named:
            parent_text, name = named.group('parent', 'name')
        else:
            full = re.fullmatch(r'''(?:at|in)\s+(.+)''', rest, re.I)
            if not full:
                return FolderIntent(error='Choose the full destination path for this folder. For example: create a folder at "C:/Dev/My folder".')
            target = Path(full[1].strip('"“”'))
            parent_text, name = str(target.parent), target.name
    else:
        return None
    parent_text = parent_text.strip().strip('"“”')
    if re.match(r'^[A-Za-z]::[/\\]', parent_text):
        parent_text = parent_text[0] + parent_text[2:]
    name = name.strip()
    if (not name or name in {'.', '..'} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.rstrip(' .') != name
            or re.fullmatch(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name)):
        return FolderIntent(error='Choose a valid folder name without separators or reserved characters.')
    parent = Path(parent_text).expanduser()
    if not parent.is_absolute():
        return FolderIntent(error='Choose an absolute destination path; no project folder will be assumed.')
    if not parent.is_dir():
        return FolderIntent(error=f'The parent folder does not exist: {parent}. Choose an existing parent folder.')
    return FolderIntent(parent.resolve() / name)


def requested_folder(message: str) -> Path | None:
    intent = folder_intent(message)
    return intent.target if intent else None
