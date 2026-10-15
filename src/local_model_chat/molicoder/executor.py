from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from .gateway import InferenceResponse
from .tools import TOOL_DEFINITIONS, ToolDispatcher, ToolParseError, parse_tool_calls
from ..actions.receipts import ActionReviewRequired


class Gateway(Protocol):
    def chat(self, **kwargs: Any) -> InferenceResponse: ...


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: str
    final_message: str
    turns: int
    tool_calls: int
    error: str = ""
    trace: tuple[str, ...] = ()
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


class AgentExecutor:
    def __init__(
        self,
        *,
        gateway: Gateway,
        dispatcher: ToolDispatcher,
        model: str,
        context_window: int = 16384,
        max_output_tokens: int = 2048,
        temperature: float = 0.2,
        top_p: float = 0.9,
        max_turns: int = 8,
        max_seconds: float = 180.0,
        require_validation: bool = True,
        allow_exact_replacements: bool = False,
    ) -> None:
        if max_turns < 1 or max_seconds <= 0:
            raise ValueError("executor budgets must be positive")
        self.gateway = gateway
        self.dispatcher = dispatcher
        self.model = model
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.max_turns = max_turns
        self.max_seconds = max_seconds
        self.require_validation = require_validation
        self.tools = [
            tool
            for tool in TOOL_DEFINITIONS
            if allow_exact_replacements or tool["function"]["name"] != "replace_in_file"
        ]
        self.tool_names = {tool["function"]["name"] for tool in self.tools}

    def run(self, task: str) -> ExecutionResult:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are MoliCoder's local executor. Work only through the supplied tools. "
                    "Inspect before editing, make the smallest correct change, and run validation. "
                    "For these small tasks, use write_file with the complete updated file contents. "
                    "When finished, respond with a concise summary instead of a tool call."
                ),
            },
            {"role": "user", "content": task},
        ]
        started = time.monotonic()
        tool_count = 0
        prompt_tokens = 0
        output_tokens = 0
        trace: list[str] = []

        def result(status: str, final: str, turns: int, error: str = "") -> ExecutionResult:
            return ExecutionResult(
                status, final, turns, tool_count, error, tuple(trace),
                prompt_tokens, output_tokens, time.monotonic() - started,
            )
        malformed_reprompted = False
        validation_passed = False
        for turn in range(1, self.max_turns + 1):
            if time.monotonic() - started > self.max_seconds:
                return result("budget_exhausted", "", turn - 1, "time budget exceeded")
            response = self.gateway.chat(
                model=self.model,
                messages=list(messages),
                tools=self.tools,
                context_window=self.context_window,
                max_output_tokens=self.max_output_tokens,
                temperature=self.temperature,
                top_p=self.top_p,
            )
            message = dict(response.message)
            prompt_tokens += response.metrics.prompt_tokens
            output_tokens += response.metrics.output_tokens
            content = str(message.get("content", ""))
            try:
                calls = parse_tool_calls(message)
                unavailable = [call.name for call in calls if call.name not in self.tool_names]
                if unavailable:
                    raise ToolParseError(f"tool is not available in this task: {unavailable[0]}")
            except ActionReviewRequired as exc:
                return result("review_required", content, turn, str(exc))
            except ToolParseError as exc:
                excerpt = " ".join(content.split())[:1000] or "<empty>"
                trace.append(f"turn {turn}: invalid tool call ({exc}); model said: {excerpt}")
                if malformed_reprompted:
                    return result("failed", content, turn, str(exc))
                malformed_reprompted = True
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Your tool call was invalid: {exc}. Return exactly one valid tool call "
                            "using the supplied schema, or a plain final response if no tool is needed."
                        ),
                    }
                )
                continue
            malformed_reprompted = False
            if not calls:
                if content.strip():
                    excerpt = " ".join(content.split())[:1000]
                    trace.append(f"turn {turn}: no tool call; model said: {excerpt}")
                    if not self.require_validation or validation_passed:
                        return result("completed", content.strip(), turn)
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The task is not complete because validation has not passed. "
                                "Inspect and repair any failure, then call run_validation. "
                                "Do not provide a final answer until it reports validation passed."
                            ),
                        }
                    )
                    continue
                trace.append(f"turn {turn}: empty model response")
                return result("failed", "", turn, "model returned an empty response")
            trace.append(
                f"turn {turn}: model requested {len(calls)} tool call(s): "
                + ", ".join(call.name for call in calls)
            )
            # The installed Qwen often emits valid calls as JSON in content. Convert
            # every parsed call back to the canonical Ollama conversation shape so the
            # next model turn sees a real tool interaction, not assistant prose.
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": call.name, "arguments": call.arguments}}
                        for call in calls
                    ],
                }
            )
            for call in calls:
                tool_count += 1
                failed = False
                try:
                    output = self.dispatcher.dispatch(call)
                except (OSError, ValueError) as exc:
                    output = f"Tool error: {exc}"
                    failed = True
                trace.append(f"{call.name}({call.arguments!r}) -> {output[:500]}")
                messages.append({"role": "tool", "name": call.name, "content": output})
                if call.name == "run_validation":
                    validation_passed = output.lstrip().startswith("validation passed")
                    if not validation_passed:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Validation failed; continue working instead of explaining. "
                                    "Read the failing test and the target file named in the validation output, "
                                    "compare expected with actual, make a concrete repair, and validate again."
                                ),
                            }
                        )
                        break
                if failed:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The previous tool call failed. Stop the remaining calls from that batch. "
                                "Inspect the relevant file and test again, then use different arguments or "
                                "a different edit strategy. Do not run validation until an edit succeeds."
                            ),
                        }
                    )
                    break
        return result("budget_exhausted", "", self.max_turns, "turn budget exceeded")
