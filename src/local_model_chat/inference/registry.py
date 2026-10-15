from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from ..storage import ChatStore


@dataclass(frozen=True, slots=True)
class OfferingRecord:
    id: str
    provider_id: str
    model_id: str
    route_id: str
    account_id: str | None
    offering_class: str
    status: str
    capabilities: dict[str, bool | int | str]
    privacy_class: str
    automation_allowed: bool | None
    input_usd_per_million: float | None
    output_usd_per_million: float | None
    quota: dict
    price_checked_at: str | None
    terms_checked_at: str | None
    source_url: str
    terms_url: str
    health_state: str


class ProviderRegistry:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def add_provider(self, provider_id: str, name: str, official_url: str) -> None:
        now = self._now()
        with closing(self.store._connect()) as connection:
            connection.execute(
                """INSERT INTO inference_providers(id, name, official_url, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (provider_id, name, official_url, now, now),
            )
            connection.commit()

    def list_providers(self) -> tuple[tuple[str, str], ...]:
        with closing(self.store._connect()) as connection:
            rows = connection.execute(
                "SELECT id, name FROM inference_providers ORDER BY name, id"
            ).fetchall()
        return tuple((str(row[0]), str(row[1])) for row in rows)

    def add_model(self, model_id: str, family: str, version: str) -> None:
        now = self._now()
        with closing(self.store._connect()) as connection:
            connection.execute(
                """INSERT INTO inference_models(id, family, version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (model_id, family, version, now, now),
            )
            connection.commit()

    def add_account(
        self, account_id: str, provider_id: str, owner: str, *, secret_ref: str = "",
        auth_status: str = "unknown",
    ) -> None:
        if auth_status not in {"unknown", "login_needed", "ready", "failed"}:
            raise ValueError("unsupported authentication status")
        if secret_ref and not secret_ref.startswith((
            "os-keychain:", "wincred:", "keychain:", "secretservice:", "encrypted-local:",
        )):
            raise ValueError("account must store an opaque secret reference, not a credential value")
        now = self._now()
        with closing(self.store._connect()) as connection:
            connection.execute(
                """INSERT INTO inference_accounts(
                       id, provider_id, owner, secret_ref, auth_status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (account_id, provider_id, owner, secret_ref, auth_status, now, now),
            )
            connection.commit()

    def update_account_secret(self, account_id: str, provider_id: str, secret_ref: str) -> None:
        if not secret_ref.startswith("encrypted-local:"):
            raise ValueError("account must store an opaque encrypted-local reference")
        now = self._now()
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            provider = connection.execute(
                "SELECT 1 FROM inference_providers WHERE id = ?", (provider_id,),
            ).fetchone()
            if provider is None:
                raise ValueError("provider is not registered")
            cursor = connection.execute(
                """INSERT INTO inference_accounts(
                       id, provider_id, owner, secret_ref, auth_status, created_at, updated_at
                   ) VALUES (?, ?, 'local_user', ?, 'unknown', ?, ?)
                   ON CONFLICT(id) DO UPDATE SET secret_ref = excluded.secret_ref,
                       auth_status = 'unknown', updated_at = excluded.updated_at
                   WHERE inference_accounts.provider_id = excluded.provider_id""",
                (account_id, provider_id, secret_ref, now, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("account belongs to a different provider")
            connection.commit()

    def add_offering(
        self, offering_id: str, provider_id: str, model_id: str, route_id: str,
        offering_class: str, *, account_id: str | None = None,
    ) -> None:
        if offering_class not in {"local", "recurring_free", "trial", "metered", "subscription", "opaque_agent"}:
            raise ValueError("unsupported offering class")
        now = self._now()
        with closing(self.store._connect()) as connection:
            connection.execute(
                """INSERT INTO inference_offerings(
                       id, provider_id, model_id, route_id, account_id, class,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (offering_id, provider_id, model_id, route_id, account_id, offering_class, now, now),
            )
            connection.execute(
                """INSERT INTO inference_offering_events(
                       offering_id, kind, snapshot_json, actor, created_at
                   ) VALUES (?, 'discovered', ?, 'registry', ?)""",
                (offering_id, json.dumps({"class": offering_class, "status": "discovered"}), now),
            )
            connection.commit()

    def get_offering(self, offering_id: str) -> OfferingRecord | None:
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM inference_offerings WHERE id = ?", (offering_id,)
            ).fetchone()
        if row is None:
            return None
        return OfferingRecord(
            id=row["id"], provider_id=row["provider_id"], model_id=row["model_id"],
            route_id=row["route_id"], account_id=row["account_id"],
            offering_class=row["class"], status=row["status"],
            capabilities=json.loads(row["capabilities_json"]),
            privacy_class=row["privacy_class"],
            automation_allowed=None if row["automation_allowed"] is None else bool(row["automation_allowed"]),
            input_usd_per_million=row["input_usd_per_million"],
            output_usd_per_million=row["output_usd_per_million"],
            quota=json.loads(row["quota_json"]),
            price_checked_at=row["price_checked_at"], terms_checked_at=row["terms_checked_at"],
            source_url=row["source_url"], terms_url=row["terms_url"],
            health_state=row["health_state"],
        )

    def list_offerings(self) -> list[OfferingRecord]:
        with closing(self.store._connect()) as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT id FROM inference_offerings ORDER BY provider_id, id"
            ).fetchall()]
        return [record for offering_id in ids if (record := self.get_offering(offering_id))]

    def list_events(self, offering_id: str) -> list[dict]:
        with closing(self.store._connect()) as connection:
            rows = connection.execute(
                """SELECT id, kind, snapshot_json, source_url, actor, created_at
                   FROM inference_offering_events WHERE offering_id = ? ORDER BY id""",
                (offering_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_account_status(self, account_id: str) -> str | None:
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT auth_status FROM inference_accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return str(row[0]) if row else None

    def set_offering_state(
        self, offering_id: str, *, status: str | None = None,
        health_state: str | None = None, actor: str, reason: str,
    ) -> None:
        if status is None and health_state is None:
            raise ValueError("status or health state is required")
        if status is not None and status not in {"discovered", "eligible", "disabled", "unavailable", "deprecated"}:
            raise ValueError("unsupported offering status")
        if health_state is not None and health_state not in {"unknown", "healthy", "degraded", "unavailable"}:
            raise ValueError("unsupported health state")
        previous = self.get_offering(offering_id)
        if previous is None:
            raise ValueError("offering does not exist")
        now = self._now()
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE inference_offerings SET status = ?, health_state = ?, updated_at = ?
                   WHERE id = ?""",
                (status or previous.status, health_state or previous.health_state, now, offering_id),
            )
            connection.execute(
                """INSERT INTO inference_offering_events(
                       offering_id, kind, snapshot_json, actor, created_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    offering_id, "state_change",
                    json.dumps({
                        "previous_status": previous.status,
                        "status": status or previous.status,
                        "previous_health": previous.health_state,
                        "health": health_state or previous.health_state,
                        "reason": reason,
                    }, sort_keys=True), actor, now,
                ),
            )
            connection.commit()
