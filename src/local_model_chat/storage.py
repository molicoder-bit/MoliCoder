from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import database_path
from .domain import (
    Conversation, EventRecord, GenerationMetrics, Message, Project, ProjectPreferences,
    ProjectRunStatus, KnowledgeRecord, TaskCompletionRecord, TaskRecord, TaskStatus,
)


class ChatStore:
    SCHEMA_VERSION = 11
    TASK_STATES = frozenset({
        "pending", "proposed", "ready", "running", "verifying", "completed",
        "blocked", "failed", "retrying", "waiting_quota", "waiting_approval",
        "waiting_auth", "cancelled", "review_required", "needs_attention",
    })

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_before_schema_upgrade()
        self._initialize()

    def _backup_before_schema_upgrade(self) -> None:
        """Capture a consistent copy of an existing database before new schema writes."""
        if not self.path.is_file():
            return
        with closing(sqlite3.connect(self.path)) as source:
            version = int(source.execute("PRAGMA user_version").fetchone()[0])
            has_data = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
            ).fetchone() is not None
            if version >= self.SCHEMA_VERSION or not has_data:
                return
            integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"database integrity check failed: {integrity}")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = self.path.with_name(f"{self.path.name}.pre-v{self.SCHEMA_VERSION}-{stamp}.bak")
            with closing(sqlite3.connect(backup_path)) as target:
                source.backup(target)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('system', 'user', 'assistant')),
                    content TEXT NOT NULL,
                    thinking TEXT NOT NULL DEFAULT '',
                    prompt_tokens INTEGER,
                    output_tokens INTEGER,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, id);
                CREATE INDEX IF NOT EXISTS idx_conversations_updated
                    ON conversations(updated_at DESC);
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_opened_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    request TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    base_commit TEXT NOT NULL DEFAULT '',
                    final_commit TEXT NOT NULL DEFAULT '',
                    worktree TEXT NOT NULL DEFAULT '',
                    recovery_reason TEXT NOT NULL DEFAULT '',
                    log_version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    worktree TEXT,
                    branch TEXT,
                    base_commit TEXT,
                    changed_paths TEXT NOT NULL DEFAULT '[]',
                    acceptance_criteria_json TEXT NOT NULL DEFAULT '["Configured validation must pass"]',
                    verification_checks_json TEXT NOT NULL DEFAULT '["project-default"]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, key)
                );
                CREATE TABLE IF NOT EXISTS task_deps (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    depends_on INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    PRIMARY KEY(task_id, depends_on)
                );
                CREATE TABLE IF NOT EXISTS worker_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    seconds REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS validations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    command TEXT NOT NULL,
                    exit_code INTEGER NOT NULL,
                    log_path TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL,
                    cluster_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                    provider TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    est_cost REAL,
                    seconds REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'completed',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_preferences (
                    project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL DEFAULT 'codex',
                    strategy TEXT NOT NULL DEFAULT 'HYBRID',
                    trusted_providers_json TEXT NOT NULL DEFAULT '[]',
                    model_overrides_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_authorizations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK(decision IN ('approved', 'denied', 'revoked')),
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_action_authorizations_lookup
                    ON action_authorizations(project_id, action, scope, id DESC);
                CREATE TABLE IF NOT EXISTS knowledge_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                    kind TEXT NOT NULL CHECK(kind IN ('fact', 'requirement', 'decision', 'objective', 'finding')),
                    status TEXT NOT NULL CHECK(status IN ('proposed', 'verified', 'approved', 'superseded', 'invalidated', 'archived')),
                    current_revision INTEGER NOT NULL DEFAULT 1,
                    author TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_revisions (
                    item_id INTEGER NOT NULL REFERENCES knowledge_items(id) ON DELETE RESTRICT,
                    revision INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    rationale TEXT NOT NULL DEFAULT '',
                    confidence TEXT NOT NULL DEFAULT 'unverified',
                    source_ref TEXT NOT NULL DEFAULT '',
                    source_revision TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(item_id, revision)
                );
                CREATE TABLE IF NOT EXISTS knowledge_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    source_locator TEXT NOT NULL,
                    source_revision TEXT NOT NULL DEFAULT '',
                    verified_at TEXT,
                    FOREIGN KEY(item_id, revision) REFERENCES knowledge_revisions(item_id, revision)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS decision_records (
                    item_id INTEGER PRIMARY KEY REFERENCES knowledge_items(id) ON DELETE RESTRICT,
                    approved_revision INTEGER,
                    approved_at TEXT,
                    approved_by TEXT,
                    supersedes_item_id INTEGER REFERENCES knowledge_items(id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS conversation_context (
                    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                    linked_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES knowledge_items(id) ON DELETE RESTRICT,
                    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
                    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
                    relationship TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    CHECK((conversation_id IS NOT NULL) != (run_id IS NOT NULL)),
                    UNIQUE(item_id, conversation_id, run_id, relationship)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_links_conversation
                    ON knowledge_links(conversation_id, id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_links_run
                    ON knowledge_links(run_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_conversation_link
                    ON knowledge_links(item_id, conversation_id, relationship)
                    WHERE conversation_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_run_link
                    ON knowledge_links(item_id, run_id, relationship)
                    WHERE run_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS inference_providers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    official_url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inference_models (
                    id TEXT PRIMARY KEY,
                    family TEXT NOT NULL,
                    version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inference_accounts (
                    id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL REFERENCES inference_providers(id) ON DELETE RESTRICT,
                    owner TEXT NOT NULL,
                    secret_ref TEXT NOT NULL DEFAULT '',
                    auth_status TEXT NOT NULL DEFAULT 'unknown',
                    auth_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inference_offerings (
                    id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL REFERENCES inference_providers(id) ON DELETE RESTRICT,
                    model_id TEXT NOT NULL REFERENCES inference_models(id) ON DELETE RESTRICT,
                    route_id TEXT NOT NULL,
                    account_id TEXT REFERENCES inference_accounts(id) ON DELETE RESTRICT,
                    class TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    privacy_class TEXT NOT NULL DEFAULT 'unknown',
                    automation_allowed INTEGER,
                    input_usd_per_million REAL,
                    output_usd_per_million REAL,
                    quota_json TEXT NOT NULL DEFAULT '{}',
                    price_checked_at TEXT,
                    terms_checked_at TEXT,
                    source_url TEXT NOT NULL DEFAULT '',
                    terms_url TEXT NOT NULL DEFAULT '',
                    health_state TEXT NOT NULL DEFAULT 'unknown',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider_id, route_id, account_id)
                );
                CREATE TABLE IF NOT EXISTS inference_offering_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offering_id TEXT NOT NULL REFERENCES inference_offerings(id) ON DELETE RESTRICT,
                    kind TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inference_requests (
                    request_id TEXT PRIMARY KEY,
                    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
                    offering_id TEXT NOT NULL REFERENCES inference_offerings(id) ON DELETE RESTRICT,
                    model_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    sensitivity TEXT NOT NULL,
                    context_packet_id TEXT,
                    reservation_id TEXT,
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inference_usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL REFERENCES inference_requests(request_id) ON DELETE RESTRICT,
                    status TEXT NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cached_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    estimated_cost_usd REAL,
                    actual_cost_usd REAL,
                    cost_accuracy TEXT NOT NULL DEFAULT 'unknown',
                    source_event TEXT NOT NULL DEFAULT '',
                    provider_request_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inference_usage_request
                    ON inference_usage_events(request_id, id);
                CREATE TABLE IF NOT EXISTS project_budget_limits (
                    project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                    limit_usd REAL NOT NULL CHECK(limit_usd >= 0),
                    premium_reserve_usd REAL NOT NULL DEFAULT 0 CHECK(premium_reserve_usd >= 0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_scope_limits (
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('task', 'month')),
                    scope TEXT NOT NULL,
                    limit_usd REAL NOT NULL CHECK(limit_usd >= 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, kind, scope)
                );
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    task_scope TEXT NOT NULL DEFAULT '',
                    tier TEXT NOT NULL DEFAULT 'standard',
                    amount_usd REAL NOT NULL CHECK(amount_usd >= 0),
                    actual_usd REAL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'settled', 'released')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_budget_reservations_project
                    ON budget_reservations(project_id, status);
                CREATE TABLE IF NOT EXISTS installation_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_installation_jobs_status
                    ON installation_jobs(status, id);
                CREATE INDEX IF NOT EXISTS idx_inference_offerings_provider_status
                    ON inference_offerings(provider_id, status, id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_project_status
                    ON knowledge_items(project_id, status, id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_source
                    ON knowledge_revisions(source_ref, source_revision);
                CREATE INDEX IF NOT EXISTS idx_projects_last_opened
                    ON projects(last_opened_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_project
                    ON runs(project_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_run
                    ON tasks(run_id, id);
                CREATE INDEX IF NOT EXISTS idx_events_run
                    ON events(run_id, id);
                """
            )
            external_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(external_calls)").fetchall()
            }
            if "seconds" not in external_columns:
                connection.execute(
                    "ALTER TABLE external_calls ADD COLUMN seconds REAL NOT NULL DEFAULT 0"
                )
            if "status" not in external_columns:
                connection.execute(
                    "ALTER TABLE external_calls ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
                )
            project_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(projects)").fetchall()
            }
            if "archived_at" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN archived_at TEXT")
            run_columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
            run_migrations = {
                "provider": "TEXT NOT NULL DEFAULT ''",
                "model": "TEXT NOT NULL DEFAULT ''",
                "base_commit": "TEXT NOT NULL DEFAULT ''",
                "final_commit": "TEXT NOT NULL DEFAULT ''",
                "worktree": "TEXT NOT NULL DEFAULT ''",
                "recovery_reason": "TEXT NOT NULL DEFAULT ''",
                "log_version": "INTEGER NOT NULL DEFAULT 1",
            }
            for column, definition in run_migrations.items():
                if column not in run_columns:
                    connection.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")
            budget_columns = {row[1] for row in connection.execute("PRAGMA table_info(project_budget_limits)")}
            if "premium_reserve_usd" not in budget_columns:
                connection.execute("ALTER TABLE project_budget_limits ADD COLUMN premium_reserve_usd REAL NOT NULL DEFAULT 0")
            reservation_columns = {row[1] for row in connection.execute("PRAGMA table_info(budget_reservations)")}
            if "task_scope" not in reservation_columns:
                connection.execute("ALTER TABLE budget_reservations ADD COLUMN task_scope TEXT NOT NULL DEFAULT ''")
            if "tier" not in reservation_columns:
                connection.execute("ALTER TABLE budget_reservations ADD COLUMN tier TEXT NOT NULL DEFAULT 'standard'")
            usage_columns = {row[1] for row in connection.execute("PRAGMA table_info(inference_usage_events)")}
            if "cached_tokens" not in usage_columns:
                connection.execute("ALTER TABLE inference_usage_events ADD COLUMN cached_tokens INTEGER")
            if "reasoning_tokens" not in usage_columns:
                connection.execute("ALTER TABLE inference_usage_events ADD COLUMN reasoning_tokens INTEGER")
            request_columns = {row[1] for row in connection.execute("PRAGMA table_info(inference_requests)")}
            if "run_id" not in request_columns:
                connection.execute("ALTER TABLE inference_requests ADD COLUMN run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL")
            task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "acceptance_criteria_json" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN acceptance_criteria_json TEXT NOT NULL "
                    "DEFAULT '[\"Configured validation must pass\"]'"
                )
            if "verification_checks_json" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN verification_checks_json TEXT NOT NULL "
                    "DEFAULT '[\"project-default\"]'"
                )
            connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            connection.commit()

    def create_conversation(self, model: str, title: str = "New chat") -> Conversation:
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO conversations(title, model, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (title, model, now, now),
            )
            connection.commit()
            return Conversation(int(cursor.lastrowid), title, model, now, now)

    def list_conversations(self) -> list[Conversation]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, title, model, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
        return [Conversation(**dict(row)) for row in rows]

    def get_conversation(self, conversation_id: int) -> Conversation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id, title, model, created_at, updated_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return Conversation(**dict(row)) if row else None

    def rename_conversation(self, conversation_id: int, title: str) -> None:
        title = title.strip()[:120] or "Untitled chat"
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, datetime.now().isoformat(timespec="seconds"), conversation_id),
            )
            connection.commit()

    def delete_conversation(self, conversation_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            connection.commit()

    def clear_all(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM conversations")
            connection.commit()

    def list_messages(self, conversation_id: int) -> list[Message]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, conversation_id, role, content, thinking,
                          prompt_tokens, output_tokens, created_at
                   FROM messages WHERE conversation_id = ? ORDER BY id""",
                (conversation_id,),
            ).fetchall()
        return [Message(**dict(row)) for row in rows]

    def add_message(self, message: Message, metrics: GenerationMetrics | None = None) -> int:
        metrics_json = json.dumps(asdict(metrics) if metrics else {}, ensure_ascii=False)
        now = message.created_at or datetime.now().isoformat(timespec="seconds")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO messages(
                       conversation_id, role, content, thinking, prompt_tokens,
                       output_tokens, metrics_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.conversation_id, message.role, message.content, message.thinking,
                    message.prompt_tokens, message.output_tokens, metrics_json, now,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, message.conversation_id)
            )
            connection.commit()
            return int(cursor.lastrowid)

    def delete_last_exchange(self, conversation_id: int) -> None:
        with closing(self._connect()) as connection:
            ids = connection.execute(
                "SELECT id FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 2",
                (conversation_id,),
            ).fetchall()
            if ids:
                connection.executemany("DELETE FROM messages WHERE id = ?", [(row[0],) for row in ids])
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (datetime.now().isoformat(timespec="seconds"), conversation_id),
                )
                connection.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            connection.commit()

    def upsert_project(self, name: str, path: str, kind: str) -> Project:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO projects(name, path, kind, created_at, last_opened_at, archived_at)
                   VALUES (?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(path) DO UPDATE SET
                       name = excluded.name,
                       kind = excluded.kind,
                       last_opened_at = excluded.last_opened_at,
                       archived_at = NULL""",
                (name, path, kind, now, now),
            )
            row = connection.execute(
                "SELECT id, name, path, kind, created_at, last_opened_at FROM projects WHERE path = ?",
                (path,),
            ).fetchone()
            connection.commit()
        return Project(**dict(row))

    def list_projects(self) -> list[Project]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, name, path, kind, created_at, last_opened_at
                   FROM projects WHERE archived_at IS NULL ORDER BY id"""
            ).fetchall()
        return [Project(**dict(row)) for row in rows]

    def get_project(self, project_id: int) -> Project | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT id, name, path, kind, created_at, last_opened_at
                   FROM projects WHERE id = ? AND archived_at IS NULL""",
                (project_id,),
            ).fetchone()
        return Project(**dict(row)) if row else None

    def touch_project(self, project_id: int) -> Project | None:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE projects SET last_opened_at = ? WHERE id = ?", (now, project_id)
            )
            connection.commit()
        return self.get_project(project_id)

    def delete_project(self, project_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            connection.commit()

    def archive_project(self, project_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE projects SET archived_at = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="microseconds"), project_id),
            )
            connection.commit()

    def has_active_run(self, project_id: int) -> bool:
        active = ("preflight", "planning", "running", "validating", "repairing", "applying")
        placeholders = ",".join("?" for _ in active)
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT 1 FROM runs WHERE project_id = ? AND status IN ({placeholders}) LIMIT 1",
                (project_id, *active),
            ).fetchone()
        return row is not None

    def clear_project_history(self, project_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM runs WHERE project_id = ?", (project_id,))
            connection.commit()

    def get_project_preferences(self, project_id: int) -> ProjectPreferences:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT project_id, provider, strategy, trusted_providers_json, model_overrides_json
                   FROM project_preferences WHERE project_id = ?""",
                (project_id,),
            ).fetchone()
        if row is None:
            return ProjectPreferences(project_id)
        trusted = json.loads(row["trusted_providers_json"] or "[]")
        overrides = json.loads(row["model_overrides_json"] or "{}")
        return ProjectPreferences(
            int(row["project_id"]), str(row["provider"]), str(row["strategy"]),
            tuple(str(item) for item in trusted),
            {str(key): str(value) for key, value in overrides.items()},
        )

    def record_action_authorization(
        self, *, project_id: int | None, action: str, scope: str, risk: str,
        decision: str, actor: str, expires_at: str, reason: str = "",
    ) -> int:
        if decision not in {"approved", "denied", "revoked"}:
            raise ValueError("invalid authorization decision")
        if not action or not scope or not actor or not expires_at:
            raise ValueError("authorization requires action, scope, actor, and expiry")
        now = datetime.now().astimezone().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO action_authorizations(
                       project_id, action, scope, risk, decision, actor, reason, created_at, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, action, scope, risk, decision, actor, reason, now, expires_at),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def latest_action_authorization(
        self, *, project_id: int | None, action: str, scope: str,
    ) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT id, project_id, action, scope, risk, decision, actor,
                          reason, created_at, expires_at
                   FROM action_authorizations
                   WHERE project_id IS ? AND action = ? AND scope = ?
                   ORDER BY id DESC LIMIT 1""",
                (project_id, action, scope),
            ).fetchone()
        return dict(row) if row else None

    def create_knowledge_item(
        self, *, project_id: int, kind: str, title: str, body: str,
        rationale: str, confidence: str, source_ref: str, source_revision: str,
        author: str, supersedes_item_id: int | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT INTO knowledge_items(
                       project_id, kind, status, current_revision, author, created_at, updated_at
                   ) VALUES (?, ?, 'proposed', 1, ?, ?, ?)""",
                (project_id, kind, author, now, now),
            )
            item_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO knowledge_revisions(
                       item_id, revision, title, body, rationale, confidence,
                       source_ref, source_revision, author, created_at
                   ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, title, body, rationale, confidence, source_ref, source_revision, author, now),
            )
            if kind == "decision":
                connection.execute(
                    "INSERT INTO decision_records(item_id, supersedes_item_id) VALUES (?, ?)",
                    (item_id, supersedes_item_id),
                )
            if source_ref:
                connection.execute(
                    """INSERT INTO knowledge_evidence(
                           item_id, revision, source_type, source_locator, source_revision
                       ) VALUES (?, 1, 'reference', ?, ?)""",
                    (item_id, source_ref, source_revision),
                )
            connection.commit()
        return item_id

    def get_knowledge_item(self, item_id: int, *, approved_revision: bool = False) -> KnowledgeRecord | None:
        revision_join = (
            "JOIN decision_records d ON d.item_id = i.id AND d.approved_revision IS NOT NULL "
            "JOIN knowledge_revisions r ON r.item_id = i.id AND r.revision = d.approved_revision"
            if approved_revision else
            "JOIN knowledge_revisions r ON r.item_id = i.id AND r.revision = i.current_revision"
        )
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT i.id, i.project_id, i.kind, i.status, r.revision,
                          r.title, r.body, r.rationale, r.confidence,
                          r.source_ref, r.source_revision, r.author, i.updated_at
                   FROM knowledge_items i """ + revision_join + " WHERE i.id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        if approved_revision and payload["status"] == "proposed":
            payload["status"] = "approved"
        return KnowledgeRecord(**payload)

    def list_project_knowledge(
        self, project_id: int, *, kind: str | None = None, status: str | None = None,
        approved_revision: bool = False,
    ) -> list[KnowledgeRecord]:
        clauses = ["project_id = ?"]
        args: list[object] = [project_id]
        if kind is not None:
            clauses.append("kind = ?")
            args.append(kind)
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        with closing(self._connect()) as connection:
            ids = [int(row[0]) for row in connection.execute(
                f"SELECT id FROM knowledge_items WHERE {' AND '.join(clauses)} ORDER BY id", args,
            ).fetchall()]
        return [item for item_id in ids if (item := self.get_knowledge_item(item_id, approved_revision=approved_revision))]

    def revise_knowledge_item(
        self, item_id: int, *, title: str, body: str, rationale: str,
        confidence: str, source_ref: str, source_revision: str, author: str,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT current_revision, status FROM knowledge_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None or row["status"] in {"superseded", "invalidated", "archived"}:
                raise ValueError("knowledge item cannot be revised")
            revision = int(row["current_revision"]) + 1
            connection.execute(
                """INSERT INTO knowledge_revisions(
                       item_id, revision, title, body, rationale, confidence,
                       source_ref, source_revision, author, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, revision, title, body, rationale, confidence, source_ref, source_revision, author, now),
            )
            if source_ref:
                connection.execute(
                    """INSERT INTO knowledge_evidence(
                           item_id, revision, source_type, source_locator, source_revision
                       ) VALUES (?, ?, 'reference', ?, ?)""",
                    (item_id, revision, source_ref, source_revision),
                )
            connection.execute(
                "UPDATE knowledge_items SET current_revision = ?, status = 'proposed', updated_at = ? WHERE id = ?",
                (revision, now, item_id),
            )
            connection.commit()
        return revision

    def transition_knowledge_item(self, item_id: int, *, status: str, actor: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind, status, current_revision FROM knowledge_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise ValueError("knowledge item does not exist")
            if row["status"] in {"superseded", "invalidated", "archived"}:
                raise ValueError("terminal knowledge item cannot transition")
            if status == "verified" and row["status"] != "proposed":
                raise ValueError("only proposed knowledge can be verified")
            if status == "approved":
                if row["kind"] != "decision":
                    raise ValueError("only decisions use approval")
                connection.execute(
                    """UPDATE decision_records SET approved_revision = ?, approved_at = ?, approved_by = ?
                       WHERE item_id = ?""",
                    (row["current_revision"], now, actor, item_id),
                )
                predecessor = connection.execute(
                    "SELECT supersedes_item_id FROM decision_records WHERE item_id = ?", (item_id,)
                ).fetchone()
                if predecessor and predecessor[0] is not None:
                    connection.execute(
                        "UPDATE knowledge_items SET status = 'superseded', updated_at = ? WHERE id = ?",
                        (now, predecessor[0]),
                    )
            elif status not in {"verified", "invalidated", "archived"}:
                raise ValueError("unsupported knowledge transition")
            connection.execute(
                "UPDATE knowledge_items SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, item_id),
            )
            connection.commit()

    def link_conversation_project(self, conversation_id: int, project_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            current = connection.execute(
                "SELECT project_id FROM conversation_context WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if current is not None and current[0] != project_id:
                has_history = connection.execute(
                    "SELECT 1 FROM messages WHERE conversation_id = ? LIMIT 1", (conversation_id,)
                ).fetchone()
                has_links = connection.execute(
                    "SELECT 1 FROM knowledge_links WHERE conversation_id = ? LIMIT 1", (conversation_id,)
                ).fetchone()
                if has_history is not None or has_links is not None:
                    raise ValueError("conversation with history cannot move to another project")
            connection.execute(
                """INSERT INTO conversation_context(conversation_id, project_id, linked_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                       project_id = excluded.project_id, updated_at = excluded.updated_at""",
                (conversation_id, project_id, now, now),
            )
            connection.commit()

    def get_conversation_project_id(self, conversation_id: int) -> int | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT project_id FROM conversation_context WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row[0]) if row else None

    def link_knowledge(
        self, item_id: int, *, relationship: str,
        conversation_id: int | None = None, run_id: int | None = None,
    ) -> int:
        if (conversation_id is None) == (run_id is None):
            raise ValueError("link exactly one conversation or run")
        if not relationship.strip():
            raise ValueError("knowledge relationship is required")
        with closing(self._connect()) as connection:
            item = connection.execute(
                "SELECT project_id FROM knowledge_items WHERE id = ?", (item_id,)
            ).fetchone()
            if item is None:
                raise ValueError("knowledge item does not exist")
            if conversation_id is not None:
                target = connection.execute(
                    "SELECT project_id FROM conversation_context WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
            else:
                target = connection.execute(
                    "SELECT project_id FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
            if target is None or target[0] != item[0]:
                raise ValueError("knowledge link must stay within its project")
            now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            connection.execute(
                """INSERT OR IGNORE INTO knowledge_links(
                       item_id, conversation_id, run_id, relationship, created_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (item_id, conversation_id, run_id, relationship.strip(), now),
            )
            row = connection.execute(
                """SELECT id FROM knowledge_links WHERE item_id = ? AND
                   conversation_id IS ? AND run_id IS ? AND relationship = ?""",
                (item_id, conversation_id, run_id, relationship.strip()),
            ).fetchone()
            connection.commit()
            return int(row[0])

    def list_knowledge_links(
        self, *, conversation_id: int | None = None, run_id: int | None = None,
    ) -> list[dict]:
        if (conversation_id is None) == (run_id is None):
            raise ValueError("query exactly one conversation or run")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, item_id, conversation_id, run_id, relationship, created_at
                   FROM knowledge_links WHERE conversation_id IS ? AND run_id IS ? ORDER BY id""",
                (conversation_id, run_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_project_preferences(
        self,
        project_id: int,
        *,
        provider: str,
        strategy: str,
        trusted_providers: tuple[str, ...] = (),
        model_overrides: dict[str, str] | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO project_preferences(
                       project_id, provider, strategy, trusted_providers_json, model_overrides_json, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE SET
                       provider = excluded.provider,
                       strategy = excluded.strategy,
                       trusted_providers_json = excluded.trusted_providers_json,
                       model_overrides_json = excluded.model_overrides_json,
                       updated_at = excluded.updated_at""",
                (
                    project_id, provider, strategy,
                    json.dumps(sorted(set(trusted_providers))),
                    json.dumps(model_overrides or {}, sort_keys=True), now,
                ),
            )
            connection.commit()

    def create_run(
        self,
        project_id: int,
        request: str,
        mode: str,
        *,
        provider: str = "",
        model: str = "",
        base_commit: str = "",
        worktree: str = "",
        log_version: int = 2,
    ) -> int:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO runs(
                       project_id, request, mode, status, started_at, provider, model,
                       base_commit, worktree, log_version
                   ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)""",
                (project_id, request, mode, now, provider, model, base_commit, worktree, log_version),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str) -> None:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE runs SET status = ?, ended_at = ? WHERE id = ?", (status, now, run_id)
            )
            connection.commit()

    def update_run_state(
        self,
        run_id: int,
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        base_commit: str | None = None,
        final_commit: str | None = None,
        worktree: str | None = None,
        recovery_reason: str | None = None,
    ) -> None:
        values = {
            "provider": provider,
            "model": model,
            "base_commit": base_commit,
            "final_commit": final_commit,
            "worktree": worktree,
            "recovery_reason": recovery_reason,
        }
        assignments = ["status = ?"]
        parameters: list[object] = [status]
        for column, value in values.items():
            if value is not None:
                assignments.append(f"{column} = ?")
                parameters.append(value)
        parameters.append(run_id)
        with closing(self._connect()) as connection:
            connection.execute(f"UPDATE runs SET {', '.join(assignments)} WHERE id = ?", parameters)
            connection.commit()

    def recover_interrupted_runs(self) -> tuple[int, ...]:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            active = ("running", "preflight", "planning", "validating", "repairing", "applying")
            placeholders = ",".join("?" for _ in active)
            rows = connection.execute(
                f"SELECT id FROM runs WHERE status IN ({placeholders}) ORDER BY id", active
            ).fetchall()
            ids = tuple(int(row[0]) for row in rows)
            if ids:
                connection.execute(
                    f"""UPDATE runs SET status = 'needs_attention', ended_at = ?,
                           recovery_reason = CASE WHEN recovery_reason = '' THEN 'application closed during run' ELSE recovery_reason END
                       WHERE status IN ({placeholders})""",
                    (now, *active),
                )
                task_rows = connection.execute(
                    f"""SELECT id, run_id, status, attempts FROM tasks
                          WHERE run_id IN ({','.join('?' for _ in ids)})
                            AND status IN ('running', 'verifying', 'retrying')""",
                    ids,
                ).fetchall()
                for task in task_rows:
                    revision = connection.execute(
                        "SELECT COUNT(*) FROM events WHERE task_id = ? AND kind = 'task_state_changed'",
                        (task["id"],),
                    ).fetchone()[0] + 1
                    connection.execute(
                        "UPDATE tasks SET status = 'review_required', updated_at = ? WHERE id = ?",
                        (now, task["id"]),
                    )
                    connection.execute(
                        "INSERT INTO events(run_id, task_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                        (task["run_id"], task["id"], "task_state_changed", json.dumps({
                            "from": task["status"], "to": "review_required",
                            "attempts": task["attempts"], "reason": "application closed during active task",
                            "actor": "recovery", "revision": revision,
                        }), now),
                    )
                connection.commit()
        return ids

    def get_run(self, run_id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT id, project_id, request, mode, status, started_at, ended_at,
                          provider, model, base_commit, final_commit, worktree,
                          recovery_reason, log_version
                   FROM runs WHERE id = ?""",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_project_runs(self, project_id: int) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, project_id, request, mode, status, started_at, ended_at,
                          provider, model, base_commit, final_commit, worktree,
                          recovery_reason, log_version
                   FROM runs WHERE project_id = ? ORDER BY id DESC""",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def current_project_run_status(self, project_id: int) -> ProjectRunStatus | None:
        """Read the latest recorded run and tasks for one non-archived project."""
        with closing(self._connect()) as connection:
            project = connection.execute(
                "SELECT id, name FROM projects WHERE id = ? AND archived_at IS NULL",
                (project_id,),
            ).fetchone()
            if project is None:
                return None
            run = connection.execute(
                """SELECT id, status, started_at, ended_at FROM runs
                   WHERE project_id = ? ORDER BY id DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
            if run is None:
                return ProjectRunStatus(int(project["id"]), str(project["name"]), None, None, None)
            rows = connection.execute(
                """SELECT id, key, title, status, updated_at FROM tasks
                   WHERE run_id = ? ORDER BY id""",
                (run["id"],),
            ).fetchall()
            event = connection.execute(
                "SELECT MAX(created_at) FROM events WHERE run_id = ?", (run["id"],)
            ).fetchone()
        tasks = tuple(TaskStatus(**dict(row)) for row in rows)
        updated_at = max(
            (value for value in (
                run["started_at"], run["ended_at"], event[0],
                *(task.updated_at for task in tasks),
            ) if value),
            default=None,
        )
        return ProjectRunStatus(
            int(project["id"]), str(project["name"]), int(run["id"]),
            str(run["status"]), updated_at, tasks,
        )

    def latest_recoverable_run(self, project_id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT id, project_id, request, mode, status, started_at, ended_at,
                          provider, model, base_commit, final_commit, worktree,
                          recovery_reason, log_version
                   FROM runs
                   WHERE project_id = ? AND status IN ('needs_attention', 'ready_to_apply', 'cancelled', 'failed')
                         AND worktree != ''
                   ORDER BY id DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_run_plan(self, run_id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT json FROM plans WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def reopen_run(self, run_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE runs SET status = 'running', ended_at = NULL, recovery_reason = '' WHERE id = ?",
                (run_id,),
            )
            connection.commit()

    def save_plan(self, run_id: int, source: str, provider: str, payload: dict) -> int:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO plans(run_id, source, provider, json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, source, provider, json.dumps(payload, ensure_ascii=False), now),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def create_run_task(
        self, run_id: int, key: str, title: str, objective: str, risk: str,
        *, acceptance_criteria: tuple[str, ...] = ("Configured validation must pass",),
        verification_checks: tuple[str, ...] = ("project-default",),
    ) -> int:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO tasks(
                       run_id, key, title, objective, status, risk, attempts,
                       acceptance_criteria_json, verification_checks_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'proposed', ?, 0, ?, ?, ?, ?)""",
                (run_id, key, title, objective, risk,
                 json.dumps(acceptance_criteria), json.dumps(verification_checks), now, now),
            )
            task_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO events(run_id, task_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, task_id, "task_state_changed", json.dumps({
                    "from": None, "to": "proposed", "attempts": 0, "reason": "task created",
                    "actor": "system", "revision": 1,
                }), now),
            )
            connection.commit()
            return task_id

    def add_task_dependency(self, task_id: int, depends_on: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO task_deps(task_id, depends_on) VALUES (?, ?)",
                (task_id, depends_on),
            )
            connection.commit()

    def update_run_task(
        self, task_id: int, status: str, attempts: int, *,
        reason: str = "", actor: str = "system",
    ) -> None:
        self.transition_task_state(
            task_id, status, attempts=attempts,
            reason=reason or f"task entered {status}", actor=actor,
        )

    def transition_task_state(
        self, task_id: int, status: str, *, attempts: int,
        reason: str, actor: str,
    ) -> None:
        if status not in self.TASK_STATES:
            raise ValueError("unsupported task lifecycle state")
        if attempts < 0 or not reason.strip() or not actor.strip():
            raise ValueError("task transition requires attempts, reason and actor")
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            current = connection.execute(
                "SELECT run_id, status FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if current is None:
                raise ValueError("task does not exist")
            revision = connection.execute(
                "SELECT COUNT(*) FROM events WHERE task_id = ? AND kind = 'task_state_changed'",
                (task_id,),
            ).fetchone()[0] + 1
            connection.execute(
                "UPDATE tasks SET status = ?, attempts = ?, updated_at = ? WHERE id = ?",
                (status, attempts, now, task_id),
            )
            connection.execute(
                "INSERT INTO events(run_id, task_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (current["run_id"], task_id, "task_state_changed", json.dumps({
                    "from": current["status"], "to": status, "attempts": attempts,
                    "reason": reason.strip(), "actor": actor.strip(), "revision": revision,
                }), now),
            )
            connection.commit()

    def list_task_state_history(self, task_id: int) -> tuple[dict, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT payload_json, created_at FROM events
                   WHERE task_id = ? AND kind = 'task_state_changed' ORDER BY id""",
                (task_id,),
            ).fetchall()
        return tuple(json.loads(row["payload_json"]) | {"created_at": row["created_at"]} for row in rows)

    def update_task_workspace(
        self,
        task_id: int,
        *,
        worktree: str,
        branch: str,
        base_commit: str,
        changed_paths: tuple[str, ...] = (),
    ) -> None:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE tasks SET worktree = ?, branch = ?, base_commit = ?,
                       changed_paths = ?, updated_at = ? WHERE id = ?""",
                (worktree, branch, base_commit, json.dumps(changed_paths), now, task_id),
            )
            connection.commit()

    def list_run_tasks(self, run_id: int) -> list[TaskRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, run_id, key, title, objective, status, risk, attempts,
                          worktree, branch, base_commit, changed_paths,
                          acceptance_criteria_json, verification_checks_json
                   FROM tasks WHERE run_id = ? ORDER BY id""",
                (run_id,),
            ).fetchall()
        records: list[TaskRecord] = []
        for row in rows:
            payload = dict(row)
            payload["changed_paths"] = tuple(json.loads(payload["changed_paths"] or "[]"))
            payload["acceptance_criteria"] = tuple(json.loads(payload.pop("acceptance_criteria_json")))
            payload["verification_checks"] = tuple(json.loads(payload.pop("verification_checks_json")))
            records.append(TaskRecord(**payload))
        return records

    def add_event(
        self, run_id: int, kind: str, payload: dict, task_id: int | None = None
    ) -> int:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO events(run_id, task_id, kind, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, task_id, kind, json.dumps(payload, ensure_ascii=False), now),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def record_task_completion(
        self, run_id: int, task_id: int, *, revision: str,
        checks: tuple[str, ...], validation_summary: str,
        output_artifacts: tuple[str, ...], unmet_criteria: tuple[str, ...],
        human_review_status: str,
    ) -> TaskCompletionRecord:
        if not revision or not validation_summary.lstrip().startswith("validation passed"):
            raise ValueError("completion requires a verified revision and passing validation")
        if human_review_status not in {"not_required", "pending", "approved"}:
            raise ValueError("invalid human review status")
        if unmet_criteria and human_review_status == "not_required":
            raise ValueError("unmet criteria require review")
        record = TaskCompletionRecord(
            run_id, task_id, revision, tuple(checks), validation_summary[-8000:],
            tuple(output_artifacts), tuple(unmet_criteria), human_review_status,
            datetime.now().isoformat(timespec="microseconds"),
        )
        self.add_event(run_id, "task_completion_record", asdict(record), task_id)
        return record

    def get_task_completion(self, task_id: int) -> TaskCompletionRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT payload_json FROM events
                   WHERE task_id = ? AND kind = 'task_completion_record' ORDER BY id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        for field_name in ("checks", "output_artifacts", "unmet_criteria"):
            payload[field_name] = tuple(payload[field_name])
        return TaskCompletionRecord(**payload)

    def list_events(self, run_id: int) -> list[EventRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, run_id, task_id, kind, payload_json, created_at
                   FROM events WHERE run_id = ? ORDER BY id""",
                (run_id,),
            ).fetchall()
        return [EventRecord(**dict(row)) for row in rows]

    def run_inference_usage(self, run_id: int) -> dict:
        with closing(self._connect()) as connection:
            local = connection.execute(
                """SELECT COUNT(*), COALESCE(SUM(e.input_tokens), 0),
                          COALESCE(SUM(e.output_tokens), 0),
                          SUM(e.estimated_cost_usd), SUM(e.actual_cost_usd)
                   FROM inference_requests r
                   LEFT JOIN inference_usage_events e ON e.id = (
                       SELECT MAX(id) FROM inference_usage_events
                       WHERE request_id = r.request_id AND status IN ('completed', 'failed', 'cancelled', 'denied')
                   )
                   WHERE r.run_id = ?""",
                (run_id,),
            ).fetchone()
            external = connection.execute(
                """SELECT COUNT(*), COALESCE(SUM(input_tokens), 0),
                          COALESCE(SUM(output_tokens), 0)
                   FROM external_calls WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
        return {
            "local_requests": int(local[0]),
            "local_input_tokens": int(local[1]),
            "local_output_tokens": int(local[2]),
            "local_estimated_cost_usd": local[3],
            "local_actual_cost_usd": local[4],
            "opaque_cli_calls": int(external[0]),
            "opaque_cli_reported_input_tokens": int(external[1]),
            "opaque_cli_reported_output_tokens": int(external[2]),
            "opaque_cli_cost_accuracy": "unknown",
        }

    def add_external_call(
        self,
        run_id: int,
        provider: str,
        purpose: str,
        reason: str,
        input_tokens: int,
        output_tokens: int,
        seconds: float,
        status: str,
        task_id: int | None = None,
    ) -> int:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO external_calls(
                       run_id, task_id, provider, purpose, reason, input_tokens,
                       output_tokens, est_cost, seconds, status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                (run_id, task_id, provider, purpose, reason, input_tokens, output_tokens, seconds, status, now),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def add_worker_run(
        self,
        task_id: int,
        role: str,
        model: str,
        provider: str,
        prompt_tokens: int,
        output_tokens: int,
        seconds: float,
        status: str,
    ) -> int:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO worker_runs(
                       task_id, role, model, provider, prompt_tokens, output_tokens,
                       seconds, status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, role, model, provider, prompt_tokens, output_tokens, seconds, status, now),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def add_failure(self, task_id: int, fingerprint: str, cluster: dict) -> int:
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO failures(task_id, fingerprint, cluster_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, fingerprint, json.dumps(cluster, ensure_ascii=False), now),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def list_external_calls(self, run_id: int) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, run_id, task_id, provider, purpose, reason, input_tokens,
                          output_tokens, est_cost, seconds, status, created_at
                   FROM external_calls WHERE run_id = ? ORDER BY id""",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_markdown(self, conversation_id: int, destination: Path) -> None:
        conversation = self.get_conversation(conversation_id)
        if not conversation:
            raise ValueError("Conversation not found")
        lines = [f"# {conversation.title}", "", f"Model: `{conversation.model}`", ""]
        for message in self.list_messages(conversation_id):
            lines.extend([f"## {message.role.title()}", ""])
            if message.thinking:
                lines.extend(["<details><summary>Reasoning</summary>", "", message.thinking, "", "</details>", ""])
            lines.extend([message.content, ""])
        destination.write_text("\n".join(lines), encoding="utf-8")

    def export_json(self, conversation_id: int, destination: Path) -> None:
        conversation = self.get_conversation(conversation_id)
        if not conversation:
            raise ValueError("Conversation not found")
        payload = {
            "conversation": asdict(conversation),
            "messages": [asdict(message) for message in self.list_messages(conversation_id)],
        }
        destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
