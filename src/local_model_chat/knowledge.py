from __future__ import annotations

from .domain import KnowledgeRecord
from .storage import ChatStore


class ProjectKnowledge:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    def propose_item(
        self, project_id: int, *, kind: str, title: str, body: str,
        author: str, rationale: str = "", source_ref: str = "",
        source_revision: str = "",
    ) -> KnowledgeRecord:
        if kind not in {"fact", "requirement", "objective", "finding"}:
            raise ValueError("use propose_decision for decisions")
        if not title.strip() or not body.strip() or not author.strip():
            raise ValueError("knowledge title, body, and author are required")
        item_id = self.store.create_knowledge_item(
            project_id=project_id, kind=kind, title=title.strip(), body=body.strip(),
            rationale=rationale.strip(), confidence="unverified", source_ref=source_ref,
            source_revision=source_revision, author=author,
        )
        return self._get(item_id)

    def verify_item(self, item_id: int, *, actor: str) -> KnowledgeRecord:
        current = self._get(item_id)
        if actor == "model" or not actor.strip():
            raise PermissionError("verification requires a non-model actor")
        if not current.source_ref:
            raise ValueError("verification requires a source reference")
        self.store.transition_knowledge_item(item_id, status="verified", actor=actor)
        return self._get(item_id)

    def propose_decision(
        self, project_id: int, *, title: str, body: str, rationale: str,
        author: str, source_ref: str = "", source_revision: str = "",
        supersedes_item_id: int | None = None,
    ) -> KnowledgeRecord:
        if not title.strip() or not body.strip() or not rationale.strip():
            raise ValueError("decision title, body, and rationale are required")
        if supersedes_item_id is not None:
            old = self.store.get_knowledge_item(supersedes_item_id)
            if old is None or old.project_id != project_id or old.kind != "decision" or old.status != "approved":
                raise ValueError("superseded decision must be approved in the same project")
        item_id = self.store.create_knowledge_item(
            project_id=project_id, kind="decision", title=title.strip(), body=body.strip(),
            rationale=rationale.strip(), confidence="unverified", source_ref=source_ref,
            source_revision=source_revision, author=author, supersedes_item_id=supersedes_item_id,
        )
        return self._get(item_id)

    def correct_decision(
        self, item_id: int, *, title: str, body: str, rationale: str,
        author: str, source_ref: str = "", source_revision: str = "",
    ) -> KnowledgeRecord:
        current = self._get(item_id)
        if author == "model" or current.kind != "decision":
            raise PermissionError("only a human may correct a decision")
        if not title.strip() or not body.strip() or not rationale.strip():
            raise ValueError("decision title, body, and rationale are required")
        self.store.revise_knowledge_item(
            item_id, title=title.strip(), body=body.strip(), rationale=rationale.strip(),
            confidence="unverified", source_ref=source_ref, source_revision=source_revision,
            author=author,
        )
        return self._get(item_id)

    def approve_decision(self, item_id: int, *, actor: str) -> KnowledgeRecord:
        if actor == "model" or not actor.strip():
            raise PermissionError("decision approval requires a human actor")
        current = self._get(item_id)
        if current.kind != "decision":
            raise ValueError("item is not a decision")
        self.store.transition_knowledge_item(item_id, status="approved", actor=actor)
        return self._get(item_id)

    def invalidate(self, item_id: int, *, actor: str) -> None:
        self._human_transition(item_id, actor=actor, status="invalidated")

    def archive(self, item_id: int, *, actor: str) -> None:
        self._human_transition(item_id, actor=actor, status="archived")

    def _human_transition(self, item_id: int, *, actor: str, status: str) -> None:
        if actor == "model" or not actor.strip():
            raise PermissionError("knowledge lifecycle change requires a human actor")
        self.store.transition_knowledge_item(item_id, status=status, actor=actor)

    def approved_decisions(self, project_id: int) -> list[KnowledgeRecord]:
        return [
            item for item in self.store.list_project_knowledge(
                project_id, kind="decision", approved_revision=True,
            ) if item.status == "approved"
        ]

    def approved_revision(self, item_id: int) -> KnowledgeRecord | None:
        return self.store.get_knowledge_item(item_id, approved_revision=True)

    def _get(self, item_id: int) -> KnowledgeRecord:
        record = self.store.get_knowledge_item(item_id)
        if record is None:
            raise ValueError("knowledge item does not exist")
        return record
