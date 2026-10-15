from __future__ import annotations

import time
import math
from dataclasses import dataclass
from typing import Callable

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..context import estimate_tokens
from ..storage import ChatStore
from .fixtures import BenchmarkCase


@dataclass(frozen=True, slots=True)
class BenchmarkCaps:
    provider_id: str
    offering_class: str
    max_calls: int
    max_total_tokens: int
    max_output_tokens_per_call: int
    max_seconds: float
    permitted_privacy: str
    dry_run: bool = True
    max_input_tokens_per_call: int = 2048
    max_spend_usd: float | None = None
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None

    def scope(self) -> str:
        return (
            f"benchmark:{self.provider_id}:{self.offering_class}:calls={self.max_calls}:"
            f"tokens={self.max_total_tokens}:output={self.max_output_tokens_per_call}:"
            f"seconds={self.max_seconds:g}:privacy={self.permitted_privacy}:"
            f"input={self.max_input_tokens_per_call}:spend={self.max_spend_usd}:"
            f"rates={self.input_usd_per_million},{self.output_usd_per_million}"
        )


@dataclass(frozen=True, slots=True)
class BenchmarkResponse:
    text: str
    input_tokens: int
    output_tokens: int
    seconds: float
    first_token_seconds: float | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class BenchmarkRecord:
    case_id: str
    provider_id: str
    status: str
    accepted: bool | None
    input_tokens: int
    output_tokens: int
    seconds: float
    first_token_seconds: float | None
    failure: str


class BenchmarkRunner:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    def run(
        self, cases: tuple[BenchmarkCase, ...], caps: BenchmarkCaps,
        invoke: Callable[[BenchmarkCase, int], BenchmarkResponse],
    ) -> tuple[BenchmarkRecord, ...]:
        if (not caps.provider_id or caps.offering_class not in {
            "local", "recurring_free", "trial", "subscription", "metered",
        } or caps.max_calls < 1 or caps.max_total_tokens < 1
                or caps.max_output_tokens_per_call < 1 or caps.max_seconds <= 0
                or caps.max_input_tokens_per_call < 1
                or caps.permitted_privacy not in {"public", "private", "restricted"}):
            raise ValueError("benchmark needs explicit positive provider, token, call, time and privacy caps")
        paid = caps.offering_class in {"trial", "metered"}
        if paid and not caps.dry_run:
            prices = (caps.input_usd_per_million, caps.output_usd_per_million)
            if (caps.max_spend_usd is None or not math.isfinite(caps.max_spend_usd)
                    or caps.max_spend_usd <= 0 or any(
                        price is None or not math.isfinite(price) or price < 0 for price in prices
                    )):
                raise ValueError("paid benchmark requires a positive spend cap and finite quoted rates")
        if not caps.dry_run:
            risk = ActionRisk.FINANCIAL if caps.offering_class in {"trial", "metered"} else ActionRisk.EXTERNAL
            AuthorizationPolicy(self.store).require(ActionRequest(None, "benchmark", caps.scope(), risk))
        privacy_rank = {"public": 0, "private": 1, "restricted": 2}
        records: list[BenchmarkRecord] = []
        calls = tokens = 0
        reserved_spend = 0.0
        started = time.monotonic()
        for case in cases:
            if privacy_rank[case.privacy] > privacy_rank[caps.permitted_privacy]:
                records.append(BenchmarkRecord(case.id, caps.provider_id, "excluded_privacy", None, 0, 0, 0, None, "privacy cap"))
                continue
            estimated_input = estimate_tokens(case.prompt)
            worst_tokens = caps.max_input_tokens_per_call + caps.max_output_tokens_per_call
            worst_cost = (
                (caps.max_input_tokens_per_call * caps.input_usd_per_million
                 + caps.max_output_tokens_per_call * caps.output_usd_per_million) / 1_000_000
                if paid and caps.input_usd_per_million is not None and caps.output_usd_per_million is not None else 0.0
            )
            if (calls >= caps.max_calls or estimated_input > caps.max_input_tokens_per_call
                    or tokens + worst_tokens > caps.max_total_tokens
                    or (paid and not caps.dry_run and reserved_spend + worst_cost > caps.max_spend_usd)
                    or time.monotonic() - started >= caps.max_seconds):
                records.append(BenchmarkRecord(case.id, caps.provider_id, "capped", None, 0, 0, 0, None, "benchmark cap reached"))
                continue
            if caps.dry_run:
                records.append(BenchmarkRecord(case.id, caps.provider_id, "planned", None, estimated_input, 0, 0, None, ""))
                calls += 1
                tokens += worst_tokens
                continue
            calls += 1
            tokens += worst_tokens
            reserved_spend += worst_cost
            call_started = time.monotonic()
            try:
                response = invoke(case, caps.max_output_tokens_per_call)
            except Exception as exc:
                records.append(BenchmarkRecord(case.id, caps.provider_id, "failed", False, 0, 0,
                                               time.monotonic() - call_started, None, str(exc)[:500]))
                continue
            if response.input_tokens < 0 or response.output_tokens < 0 or response.seconds < 0:
                raise ValueError("benchmark provider returned invalid usage")
            if (response.input_tokens > caps.max_input_tokens_per_call
                    or response.output_tokens > caps.max_output_tokens_per_call):
                raise ValueError("benchmark provider exceeded the authorized token bounds")
            accepted = case.accepts(response.text) if not response.error else False
            records.append(BenchmarkRecord(
                case.id, caps.provider_id, "completed" if not response.error else "failed", accepted,
                response.input_tokens, response.output_tokens, response.seconds,
                response.first_token_seconds, response.error[:500],
            ))
        return tuple(records)
