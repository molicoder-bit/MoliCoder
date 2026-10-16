from __future__ import annotations

from typing import Any, Protocol
import json

from ..gateway import InferenceResponse
from ..recon import ReconPacket
from .schema import Plan, PlanValidationError, parse_plan


class Gateway(Protocol):
    def chat(self, **kwargs: Any) -> InferenceResponse: ...


class LocalPlanner:
    provider = "local-qwen"

    def __init__(
        self,
        *,
        gateway: Gateway,
        model: str,
        context_window: int = 16384,
        max_output_tokens: int = 2048,
    ) -> None:
        self.gateway = gateway
        self.model = model
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens

    def _call(self, messages: list[dict[str, str]]) -> str:
        response = self.gateway.chat(
            model=self.model,
            messages=messages,
            tools=None,
            context_window=self.context_window,
            max_output_tokens=self.max_output_tokens,
            temperature=0.1,
            top_p=0.9,
        )
        return str(response.message.get("content", ""))

    def plan(self, packet: ReconPacket) -> Plan:
        example_path = next((path for path in packet.relevant_files if not path.startswith('test') and path.endswith('.py')),
                            packet.relevant_files[0] if packet.relevant_files else 'requested-file.txt')
        output_contract = (
            'Return exactly this JSON shape: {"objective":"...","tasks":['
            '{"key":"task-1","title":"...","objective":"...",'
            '"risk":"low","depends_on":[],"expected_paths":[' + json.dumps(example_path) + '],'
            '"acceptance_criteria":["Configured validation must pass"],'
            '"verification_checks":["project-default"],"artifact_checks":[]}]}. '
            "The top-level object may contain only objective and tasks. "
            "Do not copy repository evidence fields into the output."
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are MoliCoder's local planner. Convert repository evidence into a coding plan. "
                    "Each task needs key, title, objective, risk (low/medium/high), depends_on, "
                    "expected_paths, acceptance_criteria, verification_checks, and optional artifact_checks. "
                    "For an exact text-file result, use file_equals with path and exact content. "
                    "For an empty directory, use directory_empty with path. Include checked paths in expected_paths. "
                    "Tasks must be small, explicit, single-file where possible, and mechanically verifiable. "
                    "For edits, copy exact project-relative paths from the evidence. Never prepend src/ to a root-level file. "
                    "Use Configured validation must pass for the configured test gate; do not invent prose review requirements. "
                    + output_contract
                ),
            },
            {
                "role": "user",
                "content": (
                    "Create the plan requested by the objective in this evidence packet. "
                    "The packet is input, not the output schema.\n\n<EVIDENCE>\n"
                    + packet.to_prompt()
                    + "\n</EVIDENCE>"
                ),
            },
        ]
        content = self._call(messages)
        try:
            return parse_plan(content)
        except PlanValidationError as exc:
            repaired = self._call(
                messages
                + [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            f"That response was an invalid plan: {exc}. Do not echo the evidence packet. "
                            f"Create a new plan now. {output_contract} Return JSON only, without markdown."
                        ),
                    },
                ]
            )
            return parse_plan(repaired)
