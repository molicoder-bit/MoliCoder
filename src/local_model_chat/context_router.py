from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .attachments import AttachmentError, load_attachment
from .context import estimate_tokens
from .domain import KnowledgeRecord
from .molicoder.repo_index import RepositorySnapshot
from .molicoder.repo_index import build_repository_index
from .storage import ChatStore


_STOP = {"a", "an", "and", "are", "code", "evaluate", "explain", "for", "how", "implementation", "in", "is", "our", "the", "this", "to", "what"}


@dataclass(frozen=True, slots=True)
class ContextSelection:
    index_id: str
    source_commit: str | None
    files: tuple[str, ...]
    tests: tuple[str, ...]
    decision_ids: tuple[int, ...]
    reasons: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ContextItem:
    kind: str
    source_id: str
    content: str
    estimated_tokens: int
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ContextPacket:
    index_id: str
    source_commit: str | None
    items: tuple[ContextItem, ...]
    estimated_input_tokens: int
    reserved_output_tokens: int
    excluded: tuple[str, ...]


def _terms(text: str) -> set[str]:
    return {part for part in re.findall(r"[a-z0-9_]+", text.lower()) if len(part) >= 3 and part not in _STOP}


def _score(terms: set[str], searchable: str) -> int:
    words = _terms(searchable)
    return sum(1 for term in terms if term in searchable.lower() or any(
        word.startswith(term) or term.startswith(word) for word in words if len(word) >= 4
    ))


def select_context(
    snapshot: RepositorySnapshot, decisions: list[KnowledgeRecord], question: str,
    *, max_files: int = 8, max_tests: int = 4, max_decisions: int = 5,
) -> ContextSelection:
    terms = _terms(question)
    ranked_files = sorted(
        (
            (_score(terms, f"{entry.path} {' '.join(entry.symbols)}"), entry)
            for entry in snapshot.entries if not entry.is_test
        ), key=lambda row: (-row[0], row[1].path),
    )
    files = tuple(entry.path for score, entry in ranked_files if score > 0)[:max_files]
    selected_stems = {path.rsplit("/", 1)[-1].split(".")[0].removeprefix("test_") for path in files}
    ranked_tests = sorted(
        (
            (_score(terms, f"{entry.path} {' '.join(entry.symbols)}") +
             sum(2 for stem in selected_stems if stem and stem in entry.path), entry)
            for entry in snapshot.entries if entry.is_test
        ), key=lambda row: (-row[0], row[1].path),
    )
    tests = tuple(entry.path for score, entry in ranked_tests if score > 0)[:max_tests]
    ranked_decisions = sorted(
        (
            (_score(terms, f"{item.title} {item.body} {item.rationale}"), item)
            for item in decisions
        ), key=lambda row: (-row[0], -row[1].id),
    )
    decision_ids = tuple(item.id for score, item in ranked_decisions if score > 0)[:max_decisions]
    reasons = tuple(
        [(path, "question terms or related symbols") for path in files]
        + [(path, "relevant test for selected code") for path in tests]
        + [(f"decision:{item_id}", "applicable approved decision") for item_id in decision_ids]
    )
    return ContextSelection(snapshot.index_id, snapshot.commit, files, tests, decision_ids, reasons)


def _fit_text(text: str, token_cap: int) -> tuple[str, bool]:
    if estimate_tokens(text) <= token_cap:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle] + "\n[excerpt truncated]") <= token_cap:
            low = middle
        else:
            high = middle - 1
    return text[:low] + "\n[excerpt truncated]", True


def build_context_packet(
    snapshot: RepositorySnapshot, selection: ContextSelection,
    decisions: list[KnowledgeRecord], goal: str, *,
    context_window: int, reserved_output_tokens: int, max_file_tokens: int = 1200,
) -> ContextPacket:
    if selection.index_id != snapshot.index_id:
        raise ValueError("repository index changed before context assembly")
    input_cap = int(context_window * 0.95) - reserved_output_tokens
    if input_cap <= 0:
        raise ValueError("no room remains after output reservation")
    approved = {item.id: item for item in decisions if item.status == "approved"}
    mandatory = [ContextItem("goal", "user-goal", goal, estimate_tokens(goal))]
    for item_id in selection.decision_ids:
        decision = approved.get(item_id)
        if decision is None:
            raise ValueError(f"approved decision #{item_id} is unavailable")
        body = f"Decision #{decision.id} r{decision.revision}: {decision.title}\n{decision.body}\nRationale: {decision.rationale}"
        mandatory.append(ContextItem("decision", f"decision:{decision.id}:r{decision.revision}", body, estimate_tokens(body)))
    used = sum(item.estimated_tokens for item in mandatory)
    if used > input_cap:
        raise ValueError("goal and approved decisions exceed the context budget")
    items = list(mandatory)
    excluded: list[str] = []
    seen_hashes: set[str] = set()
    for kind, paths in (("code", selection.files), ("test", selection.tests)):
        for path in paths:
            entry = snapshot.entry(path)
            if entry is None:
                raise ValueError(f"selected source is missing from the index: {path}")
            if entry.sha256 in seen_hashes:
                excluded.append(f"duplicate content: {path}")
                continue
            source_path = (snapshot.root / Path(path)).resolve()
            if not source_path.is_relative_to(snapshot.root):
                raise ValueError(f"selected source escapes the project: {path}")
            try:
                attachment = load_attachment(source_path)
            except AttachmentError as exc:
                raise ValueError(f"selected source cannot be read: {path}: {exc}") from exc
            if attachment.sha256 != entry.sha256:
                raise ValueError(f"selected source changed since indexing: {path}")
            remaining = min(max_file_tokens, input_cap - used)
            prefix = f"File: {path} (sha256 {entry.sha256})\n"
            if remaining <= estimate_tokens(prefix) + 8:
                excluded.append(f"budget: {path}")
                continue
            excerpt, truncated = _fit_text(attachment.text, remaining - estimate_tokens(prefix))
            content = prefix + excerpt
            tokens = estimate_tokens(content)
            items.append(ContextItem(kind, f"file:{path}:{entry.sha256}", content, tokens, truncated))
            used += tokens
            seen_hashes.add(entry.sha256)
    return ContextPacket(
        snapshot.index_id, snapshot.commit, tuple(items), used,
        reserved_output_tokens, tuple(excluded),
    )


def verify_context_freshness(store: ChatStore, project_id: int, packet: ContextPacket) -> tuple[str, ...]:
    project = store.get_project(project_id)
    if project is None:
        return ("project no longer exists",)
    problems: list[str] = []
    try:
        current_index = build_repository_index(project.path)
    except ValueError as exc:
        return (str(exc),)
    if current_index.index_id != packet.index_id:
        problems.append("repository source revision changed since context selection")
    for item in packet.items:
        if item.kind != "decision":
            continue
        parts = item.source_id.split(":")
        try:
            item_id = int(parts[1])
            packet_revision = int(parts[2].removeprefix("r"))
        except (IndexError, ValueError):
            problems.append(f"invalid decision reference: {item.source_id}")
            continue
        current = store.get_knowledge_item(item_id, approved_revision=True)
        if current is None or current.project_id != project_id or current.revision != packet_revision or current.status != "approved":
            problems.append(f"approved decision #{item_id} changed since context selection")
    return tuple(problems)
