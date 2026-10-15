from __future__ import annotations

import os
from typing import Mapping

from .adapter import KandevTaskAdapter
from .contracts import KandevConnection, KandevMode
from .rest import KandevRestConfig, KandevRestTaskApi
from ..storage import ChatStore


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
