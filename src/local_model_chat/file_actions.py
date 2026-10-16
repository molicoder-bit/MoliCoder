"""Deterministic interpretation and exclusive creation of empty text files."""
from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class EmptyFileIntent:
    target: Path | None = None
    error: str = ''


def empty_file_intent(message: str, root: Path | None) -> EmptyFileIntent | None:
    match = re.fullmatch(
        r'''\s*(?:(?:can you|could you|please)\s+)?(?:create|generate|make)\s+'''
        r'''(?:a |an |the )?(?:new )?(?:empty |simple )?(?:text )?file\s+'''
        r'''(?:(?:called|named)\s+)?["'`“]?(?P<name>[\w./\\-]+\.txt)["'`”]?\s*[.!?]?\s*''',
        message, re.I,
    )
    if not match:
        return None
    if root is None:
        return EmptyFileIntent(error='Choose a project for the new text file.')
    relative = Path(match['name'])
    if (relative.is_absolute() or '..' in relative.parts or any(p.startswith('.') for p in relative.parts)
            or re.search(r'(?i)(?:^|[/\\])(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', str(relative))):
        return EmptyFileIntent(error='Choose a regular text file path inside the active project.')
    target = root.resolve() / relative
    if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
        return EmptyFileIntent(error='The target links outside the active project.')
    if not target.parent.is_dir():
        return EmptyFileIntent(error=f'The destination folder does not exist: {target.parent}')
    return EmptyFileIntent(target)


def create_empty_file(target: Path) -> str:
    # Exclusive creation preserves existing files, including a concurrent creation.
    with target.open('xb'):
        pass
    if not target.is_file() or target.read_bytes() != b'':
        raise OSError('File creation could not be independently verified.')
    return f'Created {target}. Verified on disk: 0 bytes. Local filesystem action; no AI model used.'
