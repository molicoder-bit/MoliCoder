from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from .budget import BudgetManager
from .contracts import InferenceAdapter, InferenceEvent, InferenceRequest, InferenceResult
from .eligibility import EligibilityRequest, evaluate_eligibility
from .ledger import InferenceLedger, UsageEvent
from .registry import ProviderRegistry


@dataclass(frozen=True, slots=True)
class InferencePolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]


class InferencePolicy:
    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry
        self.authorizations = AuthorizationPolicy(registry.store)

    def evaluate(self, request: InferenceRequest) -> InferencePolicyDecision:
        offering = self.registry.get_offering(request.offering_id)
        if offering is None:
            return InferencePolicyDecision(False, ("offering is unknown",))
        reasons: list[str] = []
        if offering.model_id != request.model_id:
            reasons.append("model does not match the approved offering")
        required = set(request.required_capabilities)
        if request.stream:
            required.add("stream")
        if request.tool_scopes:
            required.add("tools")
        context_limit = offering.capabilities.get("context_window")
        output_limit = offering.capabilities.get("max_output_tokens")
        if not isinstance(context_limit, int) or request.max_input_tokens + request.max_output_tokens > context_limit:
            reasons.append("context limit is unknown or exceeded")
        if not isinstance(output_limit, int) or request.max_output_tokens > output_limit:
            reasons.append("output limit is unknown or exceeded")
        requested_window = request.options.get("context_window")
        if requested_window is not None and (
            not isinstance(requested_window, int)
            or requested_window < request.max_input_tokens + request.max_output_tokens
            or not isinstance(context_limit, int) or requested_window > context_limit
        ):
            reasons.append("requested context window is unsupported or too small")
        effort = request.options.get("reasoning_effort")
        if effort is not None:
            levels = offering.capabilities.get("reasoning_effort_levels")
            if not isinstance(effort, str) or not isinstance(levels, str) or effort not in levels.split(","):
                reasons.append("reasoning effort is unsupported by this offering")
        paid = offering.offering_class in {"metered", "trial"}
        route_approved = offering.offering_class == "local"
        if offering.offering_class != "local":
            risk = ActionRisk.FINANCIAL if paid else ActionRisk.EXTERNAL
            approval = self.authorizations.check(ActionRequest(
                request.project_id, "inference_route", request.offering_id, risk,
            ))
            route_approved = approval.allowed
            if not approval.allowed:
                reasons.append(approval.reason)
        if paid:
            if not request.reservation_id:
                reasons.append("metered request needs a budget reservation")
            else:
                reserved = BudgetManager(self.registry.store).active_amount(
                    request.reservation_id, request.request_id, request.project_id,
                )
                rate_in = offering.input_usd_per_million
                rate_out = offering.output_usd_per_million
                if rate_in is None or rate_out is None:
                    reasons.append("metered price is unknown")
                elif reserved is None or reserved + 1e-10 < (
                    request.max_input_tokens * rate_in + request.max_output_tokens * rate_out
                ) / 1_000_000:
                    reasons.append("budget reservation is invalid or insufficient")
        eligibility = evaluate_eligibility(
            offering, EligibilityRequest(
                request.sensitivity, tuple(sorted(required)),
                unattended=True, paid_allowed=paid and route_approved,
            ), self.registry,
        )
        reasons.extend(eligibility.reasons)
        return InferencePolicyDecision(not reasons, tuple(dict.fromkeys(reasons)))


class ControlledGateway:
    def __init__(self, policy: InferencePolicy, adapter: InferenceAdapter, ledger: InferenceLedger | None = None) -> None:
        self.policy = policy
        self.adapter = adapter
        self.ledger = ledger

    def _estimate_cost(
        self, request: InferenceRequest, input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> float | None:
        offering = self.policy.registry.get_offering(request.offering_id)
        if (offering is None or offering.input_usd_per_million is None
                or offering.output_usd_per_million is None):
            return None
        return (
            (request.max_input_tokens if input_tokens is None else input_tokens) * offering.input_usd_per_million
            + (request.max_output_tokens if output_tokens is None else output_tokens) * offering.output_usd_per_million
        ) / 1_000_000

    def _cost_accuracy(self, request: InferenceRequest) -> str:
        if request.offering_id.startswith("ollama-local:"):
            return "local_zero"
        return "estimated" if self._estimate_cost(request) is not None else "unknown"

    def _close_reservation(self, request: InferenceRequest, *, dispatched: bool, actual_usd: float | None = None) -> None:
        if not request.reservation_id:
            return
        manager = BudgetManager(self.policy.registry.store)
        if manager.active_amount(request.reservation_id, request.request_id, request.project_id) is None:
            return
        if dispatched:
            manager.settle(request.reservation_id, actual_usd)
        else:
            manager.release(request.reservation_id)

    def invoke(self, request: InferenceRequest) -> InferenceResult:
        if self.ledger:
            self.ledger.start(request)
        decision = self.policy.evaluate(request)
        if not decision.allowed:
            self._close_reservation(request, dispatched=False)
            if self.ledger:
                self.ledger.append(UsageEvent(
                    request.request_id, "denied",
                    source_event=("policy:" + "; ".join(decision.reasons))[:200],
                ))
            raise PermissionError("; ".join(decision.reasons))
        if self.ledger:
            self.ledger.append(UsageEvent(
                request.request_id, "started", estimated_cost_usd=self._estimate_cost(request),
                cost_accuracy=self._cost_accuracy(request),
                source_event="gateway",
            ))
        try:
            result = self.adapter.invoke(request)
        except Exception:
            self._close_reservation(request, dispatched=True)
            if self.ledger:
                self.ledger.append(UsageEvent(request.request_id, "failed", source_event="adapter"))
            raise
        self._close_reservation(request, dispatched=True)
        if self.ledger:
            self.ledger.append(UsageEvent(
                request.request_id, result.status, result.input_tokens, result.output_tokens,
                estimated_cost_usd=self._estimate_cost(request, result.input_tokens, result.output_tokens),
                actual_cost_usd=0.0 if request.offering_id.startswith("ollama-local:") else None,
                cost_accuracy=self._cost_accuracy(request),
                source_event="adapter", provider_request_id=result.provider_request_id or "",
                cached_tokens=result.cached_tokens, reasoning_tokens=result.reasoning_tokens,
            ))
        return result

    def stream(self, request: InferenceRequest) -> Iterator[InferenceEvent]:
        if self.ledger:
            self.ledger.start(request)
        decision = self.policy.evaluate(request)
        if not decision.allowed:
            self._close_reservation(request, dispatched=False)
            if self.ledger:
                self.ledger.append(UsageEvent(
                    request.request_id, "denied",
                    source_event=("policy:" + "; ".join(decision.reasons))[:200],
                ))
            raise PermissionError("; ".join(decision.reasons))
        if self.ledger:
            self.ledger.append(UsageEvent(
                request.request_id, "started", estimated_cost_usd=self._estimate_cost(request),
                cost_accuracy=self._cost_accuracy(request),
                source_event="gateway",
            ))
        status = "cancelled"
        terminal_cancelled = False
        try:
            for event in self.adapter.stream(request):
                if event.kind.value == "completed":
                    status = "completed"
                    self._close_reservation(request, dispatched=True)
                    if self.ledger:
                        metrics = event.payload.get("metrics")
                        self.ledger.append(UsageEvent(
                            request.request_id, status,
                            getattr(metrics, "prompt_tokens", None),
                            getattr(metrics, "output_tokens", None),
                            estimated_cost_usd=self._estimate_cost(
                                request, getattr(metrics, "prompt_tokens", None),
                                getattr(metrics, "output_tokens", None),
                            ),
                            actual_cost_usd=0.0 if request.offering_id.startswith("ollama-local:") else None,
                            cost_accuracy=self._cost_accuracy(request),
                            source_event="adapter",
                            cached_tokens=getattr(metrics, "cached_tokens", None),
                            reasoning_tokens=getattr(metrics, "reasoning_tokens", None),
                        ))
                elif event.kind.value == "failed":
                    status = "failed"
                    self._close_reservation(request, dispatched=True)
                    if self.ledger:
                        self.ledger.append(UsageEvent(request.request_id, status, source_event="adapter"))
                elif event.kind.value == "cancelled":
                    status = "cancelled"
                    terminal_cancelled = True
                yield event
            if status == "cancelled" and not terminal_cancelled:
                status = "failed"
                self._close_reservation(request, dispatched=True)
                if self.ledger:
                    self.ledger.append(UsageEvent(request.request_id, status, source_event="incomplete_stream"))
        except Exception:
            self._close_reservation(request, dispatched=True)
            status = "failed"
            if self.ledger:
                self.ledger.append(UsageEvent(request.request_id, status, source_event="adapter"))
            raise
        finally:
            if status == "cancelled":
                self._close_reservation(request, dispatched=True)
            if status == "cancelled" and self.ledger:
                self.ledger.append(UsageEvent(request.request_id, status, source_event="consumer"))
