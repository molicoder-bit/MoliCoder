"""Coordinator review of a signed remote candidate; never applies a checkout change."""

from __future__ import annotations

import json
import re
import shutil
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..storage import ChatStore
from .artifacts import ArtifactFile, CandidateManifest, stage_candidate
from .leases import LeaseConflict, RemoteLeaseStore
from .messages import RemoteMessageStore
from .revisions import CandidateConflict, review_candidate_revision


@dataclass(frozen=True, slots=True)
class CandidateReview:
    task_id: int
    candidate_commit: str
    staged_path: Path
    event_id: int


def _manifest(envelope: dict) -> CandidateManifest:
    body = envelope.get("body")
    if not isinstance(body, dict) or set(body) != {"base_commit", "candidate_commit", "files"}:
        raise ValueError("candidate manifest body is invalid")
    if (not isinstance(body["base_commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", body["base_commit"])
            or not isinstance(body["candidate_commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", body["candidate_commit"])
            or not isinstance(body["files"], list)):
        raise ValueError("candidate revision or files are invalid")
    files = []
    for item in body["files"]:
        if (not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}
                or not isinstance(item["path"], str)
                or not isinstance(item["size"], int) or isinstance(item["size"], bool)
                or not isinstance(item["sha256"], str)):
            raise ValueError("candidate file entry is invalid")
        files.append(ArtifactFile(item["path"], item["size"], item["sha256"]))
    return CandidateManifest(envelope["task_id"], envelope["epoch"],
                             body["base_commit"], body["candidate_commit"], tuple(files))


class RemoteCandidateCoordinator:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    def review(self, envelope: dict, signature_b64: str, archive: Path,
               project_root: Path, staging_root: Path) -> CandidateReview:
        if envelope.get("kind") != "artifact_manifest":
            raise ValueError("signed artifact manifest is required")
        manifest = _manifest(envelope)
        RemoteMessageStore(self.store).receive(envelope, signature_b64)
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                """SELECT r.id AS run_id, r.mode, r.provider, r.status AS run_status,
                   p.path AS project_path, t.status AS task_status
                   FROM tasks t JOIN runs r ON r.id = t.run_id
                   JOIN projects p ON p.id = r.project_id WHERE t.id = ?""",
                (manifest.task_id,),
            ).fetchone()
        if (not row or row["mode"] != "REMOTE_OPT_IN" or row["provider"] != "remote"
                or row["run_status"] != "running"
                or row["task_status"] not in {"ready", "running", "verifying"}
                or Path(row["project_path"]).resolve() != project_root.resolve()):
            raise LeaseConflict("candidate is outside its selected remote project or run")
        lease = RemoteLeaseStore(self.store).get(manifest.task_id)
        if (not lease or lease.worker_id != envelope["worker_id"]
                or lease.epoch != manifest.epoch or lease.state != "active"
                or datetime.fromisoformat(lease.expires_at) <= datetime.now(timezone.utc)):
            raise LeaseConflict("candidate lease is not active")
        if RemoteLeaseStore(self.store).cancel_requested(manifest.task_id, manifest.epoch):
            raise LeaseConflict("remote cancellation blocks candidate review")
        existing = self._existing_review(manifest.task_id, envelope["message_id"])
        if existing:
            if existing.candidate_commit != manifest.candidate_commit or not existing.staged_path.is_dir():
                raise LeaseConflict("reviewed candidate is missing or differs")
            return existing
        staged = stage_candidate(lease, manifest, archive, staging_root)
        try:
            review_candidate_revision(project_root, lease, manifest)
            with closing(self.store._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """SELECT l.worker_id, l.epoch, l.state, l.last_sequence, l.expires_at,
                       w.status AS worker_status, r.status AS run_status
                       FROM remote_leases l JOIN remote_workers w ON w.id = l.worker_id
                       JOIN tasks t ON t.id = l.task_id JOIN runs r ON r.id = t.run_id
                       WHERE l.task_id = ?""", (manifest.task_id,),
                ).fetchone()
                if (not current or current["worker_id"] != envelope["worker_id"]
                        or current["epoch"] != manifest.epoch
                        or current["last_sequence"] < envelope["sequence"]
                        or current["state"] != "active" or current["worker_status"] != "paired"
                        or current["run_status"] != "running"
                        or datetime.fromisoformat(current["expires_at"]) <= datetime.now(timezone.utc)):
                    raise LeaseConflict("candidate lease changed during review")
                now = datetime.now(timezone.utc).isoformat()
                payload = {
                    "message_id": envelope["message_id"], "worker_id": lease.worker_id,
                    "epoch": lease.epoch, "base_commit": manifest.base_commit,
                    "candidate_commit": manifest.candidate_commit,
                    "files": [item.path for item in manifest.files],
                    "staged_path": str(staged), "state": "reviewed_only",
                }
                cursor = connection.execute(
                    "INSERT INTO events(run_id, task_id, kind, payload_json, created_at) "
                    "VALUES (?, ?, 'remote_candidate_reviewed', ?, ?)",
                    (row["run_id"], manifest.task_id, json.dumps(payload), now),
                )
                connection.commit()
            return CandidateReview(manifest.task_id, manifest.candidate_commit,
                                   staged, int(cursor.lastrowid))
        except CandidateConflict as exc:
            shutil.rmtree(staged)
            self.store.add_event(row["run_id"], "remote_candidate_conflict", {
                "message_id": envelope["message_id"], "worker_id": lease.worker_id,
                "epoch": lease.epoch, "base_commit": manifest.base_commit,
                "candidate_commit": manifest.candidate_commit,
                "reason": str(exc)[:500],
            }, manifest.task_id)
            raise
        except Exception:
            shutil.rmtree(staged)
            raise

    def _existing_review(self, task_id: int, message_id: str) -> CandidateReview | None:
        with closing(self.store._connect()) as connection:
            rows = connection.execute(
                "SELECT id, payload_json FROM events WHERE task_id = ? "
                "AND kind = 'remote_candidate_reviewed' ORDER BY id DESC",
                (task_id,),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("message_id") == message_id:
                return CandidateReview(task_id, payload["candidate_commit"],
                                       Path(payload["staged_path"]), row["id"])
        return None
