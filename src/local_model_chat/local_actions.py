from __future__ import annotations

import re
from pathlib import Path


_FOLDER_REQUEST = re.compile(
    r'^\s*(?:(?:oh that.s fine,\s*)?(?:can you|could you|please)\s+)?'
    r'create\s+(?:a\s+)?folder\s+(?:in|under)\s+'
    r'(?P<parent>\S+)\s+called\s+["“](?P<name>[^"”]+)["”]\s*[.!?]?\s*$',
    re.IGNORECASE,
)


def requested_folder(message: str) -> Path | None:
    """Recognize one explicit local folder request without interpreting model output."""
    match = _FOLDER_REQUEST.fullmatch(message)
    if match is None:
        return None
    parent_text = match.group("parent")
    if re.match(r"^[A-Za-z]::[/\\]", parent_text):
        parent_text = parent_text[0] + parent_text[2:]
    name = match.group("name").strip()
    if (not name or name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.rstrip(" .") != name
            or re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name)):
        return None
    parent = Path(parent_text).expanduser()
    if not parent.is_absolute() or not parent.is_dir():
        return None
    return parent.resolve() / name
