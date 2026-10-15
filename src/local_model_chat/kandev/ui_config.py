from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import urlparse

from .adapter import KandevTaskAdapter
from .contracts import KandevConnection, KandevMode
from .rest import KandevRestConfig, KandevRestTaskApi
from ..storage import ChatStore


def kandev_board_url(environ: Mapping[str, str] | None = None) -> str:
    """Return the local web UI origin; task ownership needs separate settings."""
    values = os.environ if environ is None else environ
    endpoint = values.get("KANDEV_ENDPOINT", "").strip()
    if not endpoint:
        port = values.get("KANDEV_BACKEND_PORT") or values.get("KANDEV_PORT") or "38429"
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError("Kandev board port is invalid")
        endpoint = f"http://127.0.0.1:{port}"
    parsed = urlparse(endpoint)
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("Kandev board port is invalid") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed_port is None or parsed.path.rstrip("/") not in {"", "/mcp"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Kandev board requires a loopback HTTP endpoint")
    return f"http://{parsed.netloc}/"


def configured_kandev_adapter(store: ChatStore, environ: Mapping[str, str] | None = None) -> KandevTaskAdapter:
    """Build an opt-in loopback adapter from explicit, versioned local settings."""
    values = os.environ if environ is None else environ
    names = {
        "endpoint": "KANDEV_ENDPOINT",
        "version": "KANDEV_VERSION",
        "workspace_id": "KANDEV_WORKSPACE_ID",
        "workflow_id": "KANDEV_WORKFLOW_ID",
        "workflow_step_id": "KANDEV_WORKFLOW_STEP_ID",
        "repository_id": "KANDEV_REPOSITORY_ID",
        "agent_profile_id": "KANDEV_AGENT_PROFILE_ID",
    }
    missing = [name for name in names.values() if not values.get(name, "").strip()]
    if missing:
        raise ValueError("Kandev needs explicit local settings: " + ", ".join(missing))
    config = KandevRestConfig(**{field: values[name].strip() for field, name in names.items()})
    connection = KandevConnection(
        mode=KandevMode.ATTACHED, endpoint=config.endpoint, version=config.version,
        owner="user_selected",
    )
    return KandevTaskAdapter(connection, KandevRestTaskApi(config, store=store), store=store)
