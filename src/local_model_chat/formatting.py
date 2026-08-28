from __future__ import annotations

import re


_BOXED_TEXT_PATTERN = re.compile(
    r"(?:\\\[\s*|\[\s*)?"
    r"\\boxed\{\s*\\text\{(?P<text>.*?)\}\s*\}"
    r"(?:\s*\\\]|\s*\])?",
    re.DOTALL,
)


def _clean_latex_text(text: str) -> str:
    replacements = {
        r"\\": "\n",
        r"\&": "&",
        r"\%": "%",
        r"\$": "$",
        r"\#": "#",
        r"\_": "_",
        r"\{": "{",
        r"\}": "}",
    }
    cleaned = text.strip()
    for source, destination in replacements.items():
        cleaned = cleaned.replace(source, destination)
    return cleaned


def _final_answer_callout(match: re.Match[str]) -> str:
    text = _clean_latex_text(match.group("text"))
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
    return f"\n\n> **Final answer**\n>\n{quoted}\n\n"


def normalize_model_markdown(content: str) -> str:
    """Convert common model-only LaTeX prose wrappers into readable Markdown."""
    if not content or r"\boxed" not in content:
        return content
    return _BOXED_TEXT_PATTERN.sub(_final_answer_callout, content)
