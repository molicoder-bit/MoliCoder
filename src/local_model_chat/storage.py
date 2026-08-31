from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import database_path
from .domain import Conversation, GenerationMetrics, Message, Project


class ChatStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

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
                    last_opened_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    request TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
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
                """INSERT INTO projects(name, path, kind, created_at, last_opened_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       name = excluded.name,
                       kind = excluded.kind,
                       last_opened_at = excluded.last_opened_at""",
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
                   FROM projects ORDER BY id"""
            ).fetchall()
        return [Project(**dict(row)) for row in rows]

    def get_project(self, project_id: int) -> Project | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT id, name, path, kind, created_at, last_opened_at
                   FROM projects WHERE id = ?""",
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
