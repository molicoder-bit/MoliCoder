from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime

from ..storage import ChatStore
from .registry import ProviderRegistry


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    offering_id: str
    source_url: str
    checked_at: str
    terms_url: str
    capabilities: dict[str, bool | int | str]
    privacy_class: str
    automation_allowed: bool | None
    input_usd_per_million: float | None
    output_usd_per_million: float | None
    quota: dict


def import_catalog_record(
    store: ChatStore, record: CatalogRecord, *,
    actor: str = "official-import", manual_override: bool = False, reason: str = "",
) -> int:
    if not record.source_url.startswith("https://") or (record.terms_url and not record.terms_url.startswith("https://")):
        raise ValueError("catalog facts require HTTPS source URLs")
    try:
        checked = datetime.fromisoformat(record.checked_at)
    except ValueError as exc:
        raise ValueError("catalog checked_at must be ISO datetime") from exc
    if checked.tzinfo is None:
        raise ValueError("catalog checked_at needs a timezone")
    if record.privacy_class not in {"public", "private", "restricted", "unknown"}:
        raise ValueError("unsupported privacy class")
    if any(value is not None and value < 0 for value in (
        record.input_usd_per_million, record.output_usd_per_million,
    )):
        raise ValueError("prices cannot be negative")
    if manual_override and (not actor.strip() or not reason.strip()):
        raise ValueError("manual override requires actor and reason")
    offering = ProviderRegistry(store).get_offering(record.offering_id)
    if offering is None:
        raise ValueError("offering does not exist")
    now = datetime.now(checked.tzinfo).isoformat(timespec="microseconds")
    with closing(store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE inference_offerings SET capabilities_json = ?, privacy_class = ?,
                   automation_allowed = ?, input_usd_per_million = ?, output_usd_per_million = ?,
                   quota_json = ?, price_checked_at = ?, terms_checked_at = ?,
                   source_url = ?, terms_url = ?, updated_at = ? WHERE id = ?""",
            (
                json.dumps(record.capabilities, sort_keys=True), record.privacy_class,
                None if record.automation_allowed is None else int(record.automation_allowed),
                record.input_usd_per_million, record.output_usd_per_million,
                json.dumps(record.quota, sort_keys=True),
                record.checked_at if record.input_usd_per_million is not None and record.output_usd_per_million is not None else None,
                record.checked_at if record.terms_url else None,
                record.source_url, record.terms_url, now, record.offering_id,
            ),
        )
        cursor = connection.execute(
            """INSERT INTO inference_offering_events(
                   offering_id, kind, snapshot_json, source_url, actor, created_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                record.offering_id, "manual_override" if manual_override else "catalog_refresh",
                json.dumps({"previous": asdict(offering), "current": asdict(record), "reason": reason}, sort_keys=True),
                record.source_url, actor, now,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
