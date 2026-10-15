from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..ollama import OllamaClient
from ..storage import ChatStore
from .discovery import EnvironmentReport


def official_model_size(model: str, *, opener=None) -> tuple[int, str]:
    """Read a bounded official Ollama registry manifest before asking to pull."""
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,95})(?::([A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?", model)
    if not match:
        raise ValueError("enter a public Ollama library model and optional tag")
    name, tag = match[1], match[2] or "latest"
    url = f"https://registry.ollama.ai/v2/library/{name}/manifests/{tag}"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            raise ValueError("model manifest redirected")

    open_request = opener or urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({})).open
    with open_request(urllib.request.Request(url, headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"}), timeout=10) as response:
        raw = response.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("model manifest exceeds size limit")
    manifest = json.loads(raw)
    layers = manifest.get("layers") if isinstance(manifest, dict) else None
    if not isinstance(layers, list) or not layers:
        raise ValueError("official model manifest has no layers")
    total = 0
    for layer in layers:
        if (not isinstance(layer, dict) or not isinstance(layer.get("size"), int)
                or isinstance(layer["size"], bool) or layer["size"] <= 0
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(layer.get("digest", "")))):
            raise ValueError("official model manifest contains an invalid layer")
        total += layer["size"]
    return total, url


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
