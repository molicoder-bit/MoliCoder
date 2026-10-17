from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os


class RequestJournal:
    """Durable host-owned transitions and evidence, using the application's profile."""
    TERMINAL = {"completed", "failed", "cancelled", "blocked", "awaiting_approval", "awaiting_input", "interrupted", "resumed", "delegated"}
    TRANSITIONS = {
        'received': {'evaluating', 'failed', 'cancelled', 'blocked'},
        'evaluating': {'running', 'completed', 'failed', 'cancelled', 'blocked', 'awaiting_approval', 'awaiting_input', 'delegated'},
        'running': {'running', 'verifying', 'failed', 'cancelled', 'blocked', 'awaiting_input'},
        'verifying': {'running', 'completed', 'failed', 'cancelled', 'blocked', 'awaiting_input'},
    }

    def __init__(self, store, request_id):
        self.store, self.request_id = store, request_id

    def start(self, text, project, history, *, conversation_id=None, resumed_from=None, build_run_id=None):
        from ..process_owner import process_birth
        self.project_id = project.id if project else None
        with closing(self.store._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            if resumed_from:
                previous = db.execute('SELECT state FROM orchestration_requests WHERE id=?', (resumed_from,)).fetchone()
                if previous is None or previous['state'] != 'interrupted':
                    raise ValueError('Only an interrupted request can be resumed, once')
                db.execute("UPDATE orchestration_requests SET state='resumed',updated_at=? WHERE id=?", (self.now(), resumed_from))
            db.execute("INSERT INTO orchestration_requests(id, project_id, state, spec_json, updated_at) VALUES(?,?,?,?,?)",
                (self.request_id, project.id if project else None, "received",
                 json.dumps({"text": text, "project_root": project.path if project else None, "history": history,
                    "conversation_id": conversation_id, "resumed_from": resumed_from,
                    "build_run_id": build_run_id,
                    "owner": {"pid": os.getpid(), "birth": process_birth(os.getpid())}}), self.now()))
            db.commit()

    @classmethod
    def recover_interrupted(cls, store):
        """Observe dead owners only. Another running app's requests remain untouched."""
        from ..process_owner import process_birth
        recovered = []
        with closing(store._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT id,state,spec_json FROM orchestration_requests').fetchall():
                if row['state'] in cls.TERMINAL:
                    continue
                spec = json.loads(row['spec_json'])
                owner = spec.get('owner', {})
                birth = process_birth(owner['pid']) if owner.get('pid') else ''
                if birth is None or birth and (not owner.get('birth') or birth == owner['birth']):
                    continue
                db.execute("UPDATE orchestration_requests SET state='interrupted',updated_at=? WHERE id=?", (cls.now(), row['id']))
                db.execute('INSERT INTO orchestration_events(request_id,kind,payload_json,created_at) VALUES(?,?,?,?)',
                    (row['id'], 'interrupted', json.dumps({'previous_state': row['state']}), cls.now()))
                recovered.append(row['id'])
            db.commit()
        return recovered

    @classmethod
    def load(cls, store, request_id):
        with closing(store._connect()) as db:
            row = db.execute('SELECT * FROM orchestration_requests WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise ValueError('Request does not exist in this profile')
            result = dict(row)
            result['spec'] = json.loads(result.pop('spec_json'))
            result['events'] = [dict(id='E' + str(e['id']), kind=e['kind'], payload=json.loads(e['payload_json']))
                for e in db.execute('SELECT * FROM orchestration_events WHERE request_id=? ORDER BY id', (request_id,))]
            return result

    @classmethod
    def pending(cls, store, conversation_id=None):
        with closing(store._connect()) as db:
            rows = db.execute("SELECT id,spec_json FROM orchestration_requests WHERE state='interrupted' ORDER BY updated_at DESC").fetchall()
        return [(r['id'], json.loads(r['spec_json'])) for r in rows
            if conversation_id is None or json.loads(r['spec_json']).get('conversation_id') == conversation_id]

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def event(self, kind, payload):
        with closing(self.store._connect()) as db:
            cursor = db.execute("INSERT INTO orchestration_events(request_id, kind, payload_json, created_at) VALUES(?,?,?,?)",
                (self.request_id, kind, json.dumps(payload, ensure_ascii=False), self.now()))
            db.commit()
            return f"E{cursor.lastrowid}"

    def transition(self, state, payload=None):
        with closing(self.store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM orchestration_requests WHERE id=?", (self.request_id,)).fetchone()
            if row is None or row[0] in self.TERMINAL:
                raise ValueError("Request is absent or already terminal")
            if state not in self.TRANSITIONS.get(row[0], set()):
                raise ValueError(f'Illegal request transition: {row[0]} -> {state}')
            db.execute("UPDATE orchestration_requests SET state=?, updated_at=? WHERE id=?", (state, self.now(), self.request_id))
            db.execute("INSERT INTO orchestration_events(request_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                (self.request_id, "state", json.dumps({"from": row[0], "to": state, **(payload or {})}), self.now()))
            db.commit()
