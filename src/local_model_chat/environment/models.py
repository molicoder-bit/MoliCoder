from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..ollama import OllamaClient
from ..storage import ChatStore
from .discovery import EnvironmentReport


@dataclass(frozen=True, slots=True)
class LocalModelPlan:
    model: str
    size_bytes: int
    ram_needed_bytes: int
    vram_preferred_bytes: int
    disk_needed_bytes: int
    suitable: bool
    reason: str
    source: str
    scope: str


def plan_local_model(
    environment: EnvironmentReport, model: str, size_bytes: int,
    *, source: str,
) -> LocalModelPlan:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", model):
        raise ValueError("model name is invalid")
    if size_bytes <= 0 or not source.startswith("https://"):
        raise ValueError("model needs a verified positive size and HTTPS source")
    ram = size_bytes * 2
    vram = size_bytes
    disk = size_bytes * 2
    reasons = []
    if environment.memory_bytes is None or environment.memory_bytes < ram:
        reasons.append("RAM is unknown or below the conservative requirement")
    if environment.disk_free_bytes is None or environment.disk_free_bytes < disk:
        reasons.append("free disk is unknown or below the conservative requirement")
    digest = hashlib.sha256(f"{model}:{size_bytes}:{source}".encode("utf-8")).hexdigest()
    return LocalModelPlan(
        model, size_bytes, ram, vram, disk, not reasons,
        "; ".join(reasons) if reasons else "RAM and disk meet conservative estimates; GPU fit is advisory",
        source, f"model:{model}:size:{size_bytes}:source:{digest}",
    )


def pull_approved_local_model(
    store: ChatStore, client: OllamaClient, plan: LocalModelPlan,
) -> None:
    if not plan.suitable:
        raise PermissionError(plan.reason)
    AuthorizationPolicy(store).require(ActionRequest(
        None, "pull_local_model", plan.scope, ActionRisk.MUTATION,
    ))
    client.pull_model(plan.model)
