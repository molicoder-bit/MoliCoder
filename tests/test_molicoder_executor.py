from collections import deque

from local_model_chat.domain import GenerationMetrics
from local_model_chat.molicoder.executor import AgentExecutor
from local_model_chat.molicoder.gateway import InferenceResponse
from local_model_chat.molicoder.tools import ToolDispatcher


class FakeGateway:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = deque(responses)
        self.requests: list[list[dict]] = []
        self.options: list[dict] = []

    def chat(self, *, messages: list[dict], **kwargs) -> InferenceResponse:
        self.requests.append(messages)
        self.options.append(kwargs)
        return InferenceResponse(self.responses.popleft(), GenerationMetrics())


def test_executor_handles_bare_json_call_then_returns_final_answer(tmp_path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    gateway = FakeGateway(
        [
            {
                "content": (
                    '{"name":"write_file","arguments":'
                    '{"path":"sample.py","content":"value = 2\\n"}}'
                )
            },
            {"content": "Updated sample.py."},
        ]
    )
    executor = AgentExecutor(
        gateway=gateway,
        dispatcher=ToolDispatcher(tmp_path, validation=lambda: "passed"),
        model="molicoder:latest",
        max_turns=4,
        require_validation=False,
    )

    result = executor.run("Change the value to 2")

    assert result.status == "completed"
    assert result.final_message == "Updated sample.py."
    assert result.tool_calls == 1
    assert source.read_text(encoding="utf-8") == "value = 2\n"
    assert gateway.requests[1][-2]["tool_calls"][0]["function"]["name"] == "write_file"
    assert gateway.requests[1][-2]["content"] == ""
    assert gateway.requests[1][-1]["role"] == "tool"
    exposed = {item["function"]["name"] for item in gateway.options[0]["tools"]}
    assert "write_file" in exposed
    assert "replace_in_file" not in exposed


def test_executor_reprompts_once_after_malformed_tool_call(tmp_path) -> None:
    gateway = FakeGateway(
        [
            {"content": '{"name":"write_file","arguments":{"path":"sample.py"}}'},
            {"content": "I cannot complete that safely."},
        ]
    )
    executor = AgentExecutor(
        gateway=gateway,
        dispatcher=ToolDispatcher(tmp_path, validation=lambda: "passed"),
        model="molicoder:latest",
        max_turns=3,
        require_validation=False,
    )

    result = executor.run("Create sample.py")

    assert result.status == "completed"
    assert len(gateway.requests) == 2
    assert "invalid" in gateway.requests[1][-1]["content"].lower()


def test_failed_edit_stops_tool_batch_and_forces_a_new_strategy(tmp_path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    validations = 0

    def validate() -> str:
        nonlocal validations
        validations += 1
        return "validation passed"

    gateway = FakeGateway(
        [
            {
                "content": (
                    '{"name":"replace_in_file","arguments":'
                    '{"path":"sample.py","old":"missing","new":"value = 2"}}\n'
                    '{"name":"run_validation","arguments":{}}'
                )
            },
            {"content": "I need to inspect the file again."},
        ]
    )
    executor = AgentExecutor(
        gateway=gateway,
        dispatcher=ToolDispatcher(tmp_path, validation=validate),
        model="molicoder:latest",
        max_turns=3,
        require_validation=False,
        allow_exact_replacements=True,
    )

    result = executor.run("Change the value")

    assert result.status == "completed"
    assert validations == 0
    assert "failed" in gateway.requests[1][-1]["content"].lower()
    assert "inspect" in gateway.requests[1][-1]["content"].lower()


def test_executor_does_not_complete_until_validation_passes(tmp_path) -> None:
    gateway = FakeGateway(
        [
            {
                "content": (
                    '{"name":"write_file","arguments":'
                    '{"path":"sample.py","content":"value = 2\\n"}}'
                )
            },
            {"content": "Done."},
            {"content": '{"name":"run_validation","arguments":{}}'},
            {"content": "Implemented and validated."},
        ]
    )
    executor = AgentExecutor(
        gateway=gateway,
        dispatcher=ToolDispatcher(tmp_path, validation=lambda: "validation passed"),
        model="molicoder:latest",
        max_turns=5,
    )

    result = executor.run("Create sample.py")

    assert result.status == "completed"
    assert result.final_message == "Implemented and validated."
    assert "not complete" in gateway.requests[2][-1]["content"].lower()


def test_failed_validation_immediately_requests_repair(tmp_path) -> None:
    outcomes = iter(["validation failed\ntest_sample.py:4: expected 2", "validation passed"])
    gateway = FakeGateway(
        [
            {"content": '{"name":"run_validation","arguments":{}}'},
            {"content": '{"name":"run_validation","arguments":{}}'},
            {"content": "Validated."},
        ]
    )
    executor = AgentExecutor(
        gateway=gateway,
        dispatcher=ToolDispatcher(tmp_path, validation=lambda: next(outcomes)),
        model="molicoder:latest",
        max_turns=4,
    )

    result = executor.run("Fix the failing test")

    assert result.status == "completed"
    correction = gateway.requests[1][-1]["content"].lower()
    assert "validation failed" in correction
    assert "read the failing test" in correction
