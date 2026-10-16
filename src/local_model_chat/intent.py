from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class IntentKind(StrEnum):
    CONVERSATION = "conversation"
    RETRIEVAL = "retrieval"
    ANALYSIS = "analysis"
    TOOL = "tool"
    MUTATION = "mutation"
    EXECUTION = "execution"
    ADMINISTRATION = "administration"


@dataclass(frozen=True, slots=True)
class IntentDecision:
    kind: IntentKind
    operation: str | None
    confidence: float
    clarification: str | None = None


_EXACT_READS = {
    "project_identity": (
        r"(?:what|which) project is (?:currently )?(?:open|active|selected)",
        r"(?:what|which) project (?:are we|am i) working on",
        r"(?:what|which) project (?:we're|we are|i'm|i am) working on",
        r"what(?:'s| is) (?:the |our |my )?(?:current |selected )?project(?: name)?",
        r"what (?:the |our |my )?(?:current |selected )?project(?: name)? is",
        r"(?:show|tell me) (?:the |our |my )?(?:current |selected )?project(?: name)?",
        r"(?:current project|selected project|project name)",
        r"which project (?:is (?:selected|active)|are we using)",
    ),
    "task_status": (
        r"what(?:'s| is) (?:the |our )?current task",
        r"what task are we on",
        r"what are we working on",
        r"(?:show|tell me) (?:the |our )?current task",
        r"(?:current task|task status|current task status)",
    ),
    "git_version": (
        r"(?:what(?:'s| is) (?:the |our )?current commit)",
        r"(?:what commit are we on)",
        r"(?:what(?:'s| is) (?:the |our )?current branch)",
        r"(?:what branch are we on)",
        r"(?:show (?:the )?current (?:commit|branch))",
    ),
    "changed_files": (
        r"(?:what files (?:have changed|are changed))",
        r"(?:show (?:the )?changed files)",
        r"(?:which files are modified)",
        r"(?:git status)",
    ),
    "continuity": (
        r"where did we leave off",
        r"where did we leave off and why (?:were|are) we building it this way",
        r"what did we last finish",
    ),
}


def classify_intent(message: str) -> IntentDecision:
    normalized = " ".join(message.strip().split()).replace('’', "'").rstrip("?.! ")
    project_question = re.sub(r"^(?:(?:ok(?:ay)?|good|well)[,!]?\s+)?(?:do you know\s+|can you tell me\s+)?", "", normalized, flags=re.I)
    if any(re.fullmatch(pattern, project_question, re.I) for pattern in _EXACT_READS['project_identity']):
        return IntentDecision(IntentKind.RETRIEVAL, 'project_identity', 1.0)
    for operation, patterns in _EXACT_READS.items():
        if any(re.fullmatch(pattern, normalized, re.IGNORECASE) for pattern in patterns):
            return IntentDecision(IntentKind.RETRIEVAL, operation, 1.0)
    if re.fullmatch(r"(?:hi|hello|hey|thanks|thank you)", normalized, re.IGNORECASE):
        return IntentDecision(IntentKind.CONVERSATION, None, 1.0)
    if re.match(r"^(?:evaluate|review|analyze|explain)\b", normalized, re.IGNORECASE):
        return IntentDecision(IntentKind.ANALYSIS, None, 0.7)
    if re.match(r"^(?:run|check|search|list)\b", normalized, re.IGNORECASE):
        return IntentDecision(IntentKind.TOOL, None, 0.6)
    if re.match(r"^(?:edit|change|update|delete|remove)\b", normalized, re.IGNORECASE):
        return IntentDecision(IntentKind.MUTATION, None, 0.6)
    if re.match(r"^(?:implement|build|create|develop)\b", normalized, re.IGNORECASE):
        return IntentDecision(IntentKind.EXECUTION, None, 0.6)
    if re.match(r"^(?:install|uninstall|configure|set up)\b", normalized, re.IGNORECASE):
        return IntentDecision(IntentKind.ADMINISTRATION, None, 0.6)
    return IntentDecision(IntentKind.CONVERSATION, None, 0.3)
