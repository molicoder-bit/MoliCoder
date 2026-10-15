from __future__ import annotations

from pathlib import Path

from ..domain import Project
from ..storage import ChatStore
from .projectstate import bootstrap_project, ensure_project_state


_SELECTED_PROJECT_SETTING = "molicoder.selected_project_id"


class ProjectRegistry:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    def add(self, path: str | Path, *, name: str | None = None) -> Project:
        root = Path(path).resolve()
        if not root.is_dir():
            raise ValueError(f"project folder does not exist: {root}")
        state = ensure_project_state(root)
        project = self.store.upsert_project(
            (name or root.name).strip() or root.name,
            str(root),
            state.kind,
        )
        self.select(project.id)
        return self.store.get_project(project.id) or project

    def create(self, parent: str | Path, name: str, kind: str) -> Project:
        state = bootstrap_project(parent, name, kind)
        return self.add(state.root, name=name)

    def list(self) -> list[Project]:
        return self.store.list_projects()

    def get(self, project_id: int) -> Project | None:
        return self.store.get_project(project_id)

    def select(self, project_id: int) -> Project:
        project = self.store.touch_project(project_id)
        if project is None:
            raise ValueError(f"project not found: {project_id}")
        self.store.set_setting(_SELECTED_PROJECT_SETTING, str(project_id))
        return project

    def selected(self) -> Project | None:
        raw = self.store.get_setting(_SELECTED_PROJECT_SETTING)
        try:
            project_id = int(raw)
        except ValueError:
            return None
        return self.store.get_project(project_id)

    def clear_selection(self) -> None:
        self.store.set_setting(_SELECTED_PROJECT_SETTING, '')

    def remove(self, project_id: int) -> None:
        selected = self.selected()
        self.store.archive_project(project_id)
        if selected and selected.id == project_id:
            self.store.set_setting(_SELECTED_PROJECT_SETTING, "")
