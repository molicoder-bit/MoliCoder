"""Opt-in, bounded direct-vs-policy-gateway Ollama latency measurement."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

from local_model_chat.benchmarks.latency import LatencySample, compare_latency
from local_model_chat.inference.adapters.ollama import ensure_local_offering
from local_model_chat.inference.http_gateway import LocalPolicyHttpGateway
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore


PROMPT = "Reply with exactly one sentence describing a small Python unit test."
MODEL = "molicoder:latest"
MAX_OUTPUT_TOKENS = 16
CONTEXT_TOKENS = 4096
REPLICATES = 2


def _sample(url: str, token: str | None) -> tuple[float, float]:
    payload = json.dumps({
        "model": MODEL, "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": MAX_OUTPUT_TOKENS, "stream": True,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url + "/chat/completions", data=payload,
                                     headers=headers, method="POST")
    started = time.monotonic()
    first = None
    total_bytes = 0
    done = False
    with urllib.request.urlopen(request, timeout=60) as response:
        for line in response:
            total_bytes += len(line)
            if total_bytes > 4_194_304:
                raise ValueError("stream exceeded the 4 MiB benchmark cap")
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                done = True
                break
            event = json.loads(data)
            choices = event.get("choices", [])
            if (first is None and choices and
                    isinstance(choices[0].get("delta", {}).get("content"), str)
                    and choices[0]["delta"]["content"]):
                first = time.monotonic() - started
    total = time.monotonic() - started
    if not done or first is None:
        raise ValueError("no completed content stream")
    return first, total


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="molicoder-latency-") as directory:
        store = ChatStore(Path(directory) / "benchmark.sqlite3")
        project = store.upsert_project("Latency fixture", directory, "python")
        run_id = store.create_run(project.id, "Compare local route latency", "LOCAL_OFFLINE")
        registry = ProviderRegistry(store)
        offering_id = ensure_local_offering(
            registry, MODEL, context_window=CONTEXT_TOKENS,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        registry.set_offering_state(
            offering_id, status="eligible", health_state="healthy",
            actor="benchmark", reason="installed local model",
        )
        gateway = LocalPolicyHttpGateway(
            store, project.id, run_id, MODEL,
            context_window=CONTEXT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        gateway.start()
        samples = []
        failures = []
        try:
            # One direct warmup is outside the paired measurement.
            _sample("http://127.0.0.1:11434/v1", None)
            for replicate in range(REPLICATES):
                order = ("direct", "gateway") if replicate % 2 == 0 else ("gateway", "direct")
                for path in order:
                    url = "http://127.0.0.1:11434/v1" if path == "direct" else gateway.endpoint
                    token = None if path == "direct" else gateway.token
                    try:
                        first, total = _sample(url, token)
                    except Exception as exc:
                        failures.append({"replicate": replicate, "path": path,
                                         "error": type(exc).__name__})
                        continue
                    samples.append(LatencySample(
                        "short-chat", replicate, path, "ollama-local", MODEL,
                        CONTEXT_TOKENS, first, total, 0,
                    ))
        finally:
            gateway.stop()
    comparison = compare_latency(tuple(samples)) if not failures else None
    return {
        "workload": {"model": MODEL, "prompt": PROMPT,
                     "max_output_tokens": MAX_OUTPUT_TOKENS,
                     "context_tokens": CONTEXT_TOKENS,
                     "replicates": REPLICATES, "warmups": 1,
                     "timeout_seconds_per_call": 60},
        "samples": [asdict(item) for item in samples],
        "failures": failures,
        "comparison": asdict(comparison) if comparison else None,
        "scope": "Local Ollama direct API versus MoliCoder loopback policy gateway; no paid provider account.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Run five bounded local model requests")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this script invokes an installed model")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(run(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
