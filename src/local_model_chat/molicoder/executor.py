from __future__ import annotations

import time
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .gateway import InferenceResponse
from .tools import TOOL_DEFINITIONS, ToolDispatcher, ToolCall, ToolParseError, parse_tool_calls
from .providers import redact_text
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
        read_only: bool = False,
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
        self.read_only = read_only
        self.tools = [
            tool
            for tool in TOOL_DEFINITIONS
            if allow_exact_replacements or tool["function"]["name"] != "replace_in_file"
        ]
        self.tool_names = {tool["function"]["name"] for tool in self.tools}

    def run(self, task: str) -> ExecutionResult:
        read_only = self.read_only or bool(re.search(
            r'\bdo not (?:edit|modify|change) (?:any |the )?files?\b|\bdo not create it or change any file\b', task, re.I))
        tools = [tool for tool in self.tools if not read_only or
                 tool['function']['name'] not in {'write_file', 'replace_in_file'}]
        tool_names = {tool['function']['name'] for tool in tools}
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are MoliCoder's local executor. Work only through the supplied tools. "
                    "Inspect before editing, make the smallest correct change, and run validation. "
                    "For these small tasks, use write_file with the complete updated file contents. "
                    "When finished, respond with a concise summary instead of a tool call."
                    + (' This task is read-only. Report validation failures; do not attempt repairs.' if read_only else '')
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
                status, redact_text(final), turns, tool_count, redact_text(error), tuple(trace),
                prompt_tokens, output_tokens, time.monotonic() - started,
            )
        malformed_reprompted = False
        validation_passed = False
        empty_reprompted = False
        for turn in range(1, self.max_turns + 1):
            if time.monotonic() - started > self.max_seconds:
                return result("budget_exhausted", "", turn - 1, "time budget exceeded")
            response = self.gateway.chat(
                model=self.model,
                messages=list(messages),
                tools=tools,
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
                unavailable = [call.name for call in calls if call.name not in tool_names]
                if unavailable:
                    raise ToolParseError(f"tool is not available in this task: {unavailable[0]}")
            except ActionReviewRequired as exc:
                return result("review_required", content, turn, str(exc))
            except ToolParseError as exc:
                excerpt = " ".join(content.split())[:1000] or "<empty>"
                trace.append(redact_text(f"turn {turn}: invalid tool call ({exc}); model said: {excerpt}"))
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
                    trace.append(redact_text(f"turn {turn}: no tool call; model said: {excerpt}"))
                    if not self.require_validation or validation_passed:
                        return result("completed", content.strip(), turn)
                    # Run the fixed completion gate directly instead of spending
                    # model turns asking it to invoke the same validation tool.
                    tool_count += 1
                    output = self.dispatcher.dispatch(ToolCall('run_validation', {}))
                    trace.append(redact_text(f'host run_validation -> {output[:500]}'))
                    validation_passed = output.lstrip().startswith('validation passed')
                    if validation_passed:
                        return result('completed', content.strip(), turn)
                    if read_only:
                        return result('failed', output, turn, 'Validation failed; read-only task preserved all files.')
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The task is not complete because validation has not passed. " + output + '\n' +
                                "Inspect and repair any failure, then call run_validation. "
                                "Do not provide a final answer until it reports validation passed."
                                " Include your substantive findings in the final answer, not only validation status."
                            ),
                        }
                    )
                    continue
                trace.append(f"turn {turn}: empty model response")
                if not empty_reprompted:
                    empty_reprompted = True
                    messages.append({'role': 'user', 'content':
                        'Your response was empty and the task is unfinished. Continue the requested task using the supplied tools. '
                        'Inspect the relevant tests and target files, then implement the requested behavior and validate. '
                        'Original task: ' + task})
                    continue
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
                    if call.name in {'write_file', 'replace_in_file'}:
                        validation_passed = False
                    output = self.dispatcher.dispatch(call)
                except ActionReviewRequired as exc:
                    trace.append(f"{call.name} requires review: {exc}")
                    return result("review_required", content, turn, str(exc))
                except (OSError, ValueError) as exc:
                    output = f"Tool error: {exc}"
                    failed = True
                displayed_arguments = (
                    {"path": call.arguments.get("path"), "content_bytes": len(str(call.arguments.get("content", "")).encode("utf-8"))}
                    if call.name == "write_file" else
                    {"path": call.arguments.get("path"), "replacement": "[OMITTED]"}
                    if call.name == "replace_in_file" else call.arguments
                )
                trace.append(redact_text(f"{call.name}({displayed_arguments!r}) -> {output[:500]}"))
                messages.append({"role": "tool", "name": call.name, "content": output})
                if call.name == "run_validation":
                    validation_passed = output.lstrip().startswith("validation passed")
                    if not validation_passed:
                        if read_only:
                            return result('failed', output, turn, 'Validation failed; read-only task preserved all files.')
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
