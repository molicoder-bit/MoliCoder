from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..storage import ChatStore


class AdaptiveConcurrency:
    def __init__(self, *, max_workers: int = 4) -> None:
        self.max_workers = max(1, max_workers)
        self._observations: list[tuple[float, float, bool]] = []

    def observe(self, *, validation_seconds: float, inference_seconds: float, failed: bool) -> None:
        self._observations.append((max(0.0, validation_seconds), max(0.0, inference_seconds), failed))
        self._observations = self._observations[-20:]

    def recommend(self, project_kind: str) -> int:
        if self._observations and self._observations[-1][2]:
            return 1
        base = 3 if project_kind == "flutter" else 2
        if self._observations:
            validation = sum(item[0] for item in self._observations) / len(self._observations)
            inference = sum(item[1] for item in self._observations) / len(self._observations)
            if validation > max(1.0, inference * 2):
                base += 1
        return min(self.max_workers, base)


@dataclass(frozen=True, slots=True)
class ProjectMetrics:
    total_runs: int
    completed_runs: int
    external_calls: int
    local_worker_runs: int
    local_only_success_rate: float
    external_run_success_rate: float


class RunAnalytics:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    def project_summary(self, project_id: int) -> ProjectMetrics:
        with sqlite3.connect(self.store.path) as connection:
            rows = connection.execute(
                """SELECT r.id, r.mode, r.status,
                          EXISTS(SELECT 1 FROM external_calls e WHERE e.run_id = r.id) AS used_external
                   FROM runs r WHERE r.project_id = ?""",
                (project_id,),
            ).fetchall()
            external_calls = int(connection.execute(
                """SELECT COUNT(*) FROM external_calls e
                   JOIN runs r ON r.id = e.run_id WHERE r.project_id = ?""",
                (project_id,),
            ).fetchone()[0])
            local_workers = int(connection.execute(
                """SELECT COUNT(*) FROM worker_runs w
                   JOIN tasks t ON t.id = w.task_id
                   JOIN runs r ON r.id = t.run_id WHERE r.project_id = ?""",
                (project_id,),
            ).fetchone()[0])
        local_rows = [row for row in rows if not row[3]]
        external_rows = [row for row in rows if row[3]]

        def rate(values) -> float:
            return sum(1 for row in values if row[2] == "completed") / len(values) if values else 0.0

        return ProjectMetrics(
            len(rows),
            sum(1 for row in rows if row[2] == "completed"),
            external_calls,
            local_workers,
            rate(local_rows),
            rate(external_rows),
        )
