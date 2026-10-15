from __future__ import annotations

from dataclasses import dataclass

from .storage import ChatStore


@dataclass(frozen=True, slots=True)
class ContinuitySnapshot:
    project_id: int
    project_name: str
    objective: str
    decisions: tuple[str, ...]
    verified_work: tuple[str, ...]
    blockers: tuple[str, ...]
    open_questions: tuple[str, ...]
    source_revisions: tuple[str, ...]
    as_of: str | None

    def render(self) -> str:
        def section(title: str, values: tuple[str, ...]) -> str:
            return f"**{title}**\n" + ("\n".join(f"- {value}" for value in values) if values else "- No verified record yet.")

        parts = [f"**{self.project_name} — continuity**", f"**Objective:** {self.objective}"]
        parts.append(section("Approved decisions", self.decisions))
        parts.append(section("Verified work", self.verified_work))
        parts.append(section("Blockers", self.blockers))
        parts.append(section("Open questions", self.open_questions))
        parts.append(f"Source revisions: {', '.join(self.source_revisions) if self.source_revisions else 'none recorded'}. As of: {self.as_of or 'no run timestamp'}. Source: local project knowledge and run/task records.")
        return "\n\n".join(parts)


def build_continuity(store: ChatStore, project_id: int) -> ContinuitySnapshot | None:
    project = store.get_project(project_id)
    if project is None:
        return None
    objectives = store.list_project_knowledge(project_id, kind="objective", status="verified")
    objective = objectives[-1].body if objectives else "No verified project objective has been recorded."
    from .knowledge import ProjectKnowledge

    approved = ProjectKnowledge(store).approved_decisions(project_id)
    decisions = tuple(
        f"#{item.id} r{item.revision}: {item.title} — {item.body} ({item.rationale})"
        for item in approved[-5:]
    )
    state = store.current_project_run_status(project_id)
    assert state is not None
    run = store.get_run(state.run_id) if state.run_id is not None else None
    completed = []
    for candidate in store.list_project_runs(project_id)[:5]:
        for task in store.list_run_tasks(candidate["id"]):
            if task.status == "completed":
                completed.append(
                    f"run #{candidate['id']} task {task.key} (#{task.id}): {task.title} — completed"
                )
            if len(completed) >= 5:
                break
        if len(completed) >= 5:
            break
    verified_work = tuple(completed)
    blockers = tuple(
        f"run #{state.run_id} task {task.key} (#{task.id}): {task.title} — {task.status}"
        for task in state.tasks if task.status in {"blocked", "failed", "needs_attention"}
    )
    if run and run["status"] in {"failed", "needs_attention", "cancelled"}:
        blockers += (f"run #{state.run_id}: {run['status']} — {run['recovery_reason'] or 'reason not recorded'}",)
    open_questions: tuple[str, ...] = ()
    if not objectives:
        open_questions += ("Confirm and record the current project objective.",)
    if run and run["status"] not in {"completed", "failed", "cancelled", "needs_attention"}:
        open_questions += (f"Confirm current status of run #{state.run_id} ({run['status']}).",)
    revisions = tuple(dict.fromkeys(
        value for value in (
            *(item.source_revision for item in objectives + approved),
            run["base_commit"] if run else "",
            run["final_commit"] if run else "",
        ) if value
    ))
    return ContinuitySnapshot(
        project.id, project.name, objective, decisions, verified_work,
        blockers, open_questions, revisions, state.updated_at,
    )
