"""Matched direct-versus-gateway latency analysis; performs no provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import mean, median


@dataclass(frozen=True, slots=True)
class LatencySample:
    case_id: str
    replicate: int
    path: str
    provider_id: str
    model_id: str
    context_tokens: int
    first_token_seconds: float
    total_seconds: float
    retries: int = 0

    def __post_init__(self) -> None:
        if (not self.case_id or not self.provider_id or not self.model_id
                or self.path not in {"direct", "gateway"} or self.replicate < 0
                or self.context_tokens <= 0 or self.retries < 0):
            raise ValueError("latency sample identity or count is invalid")
        if (not isfinite(self.first_token_seconds) or not isfinite(self.total_seconds)
                or self.first_token_seconds < 0
                or self.total_seconds < self.first_token_seconds):
            raise ValueError("latency sample times are invalid")


@dataclass(frozen=True, slots=True)
class PathLatency:
    samples: int
    median_first_token_seconds: float
    median_total_seconds: float
    mean_retries: float


@dataclass(frozen=True, slots=True)
class LatencyComparison:
    provider_id: str
    model_id: str
    context_tokens: int
    direct: PathLatency
    gateway: PathLatency
    median_paired_total_delta_seconds: float
    paired_total_delta_range_seconds: tuple[float, float]


def compare_latency(samples: tuple[LatencySample, ...]) -> LatencyComparison:
    """Compare exact matched prompts/repeats; range exposes observed uncertainty.

    Positive delta means gateway was slower. This descriptive range is not a
    confidence interval; a live report must identify its workload and sample size.
    """
    if not samples:
        raise ValueError("latency comparison needs samples")
    identities = {(item.provider_id, item.model_id, item.context_tokens) for item in samples}
    if len(identities) != 1:
        raise ValueError("provider, model, and context must match")
    paired: dict[tuple[str, int], dict[str, LatencySample]] = {}
    for sample in samples:
        routes = paired.setdefault((sample.case_id, sample.replicate), {})
        if sample.path in routes:
            raise ValueError("duplicate route sample")
        routes[sample.path] = sample
    if any(set(routes) != {"direct", "gateway"} for routes in paired.values()):
        raise ValueError("every prompt and replicate needs both routes")
    direct = [routes["direct"] for routes in paired.values()]
    gateway = [routes["gateway"] for routes in paired.values()]
    deltas = [other.total_seconds - base.total_seconds
              for base, other in zip(direct, gateway, strict=True)]

    def summarize(rows: list[LatencySample]) -> PathLatency:
        return PathLatency(
            len(rows), median(item.first_token_seconds for item in rows),
            median(item.total_seconds for item in rows), mean(item.retries for item in rows),
        )

    provider_id, model_id, context_tokens = next(iter(identities))
    return LatencyComparison(
        provider_id, model_id, context_tokens, summarize(direct), summarize(gateway),
        median(deltas), (min(deltas), max(deltas)),
    )
