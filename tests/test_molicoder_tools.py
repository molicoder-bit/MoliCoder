import json

import pytest

from local_model_chat.molicoder.tools import (
    ToolCall,
    ToolDispatcher,
    ToolParseError,
    parse_tool_calls,
)


def test_parses_structured_and_observed_bare_json_tool_calls() -> None:
    structured = {
        "tool_calls": [
            {"function": {"name": "list_dir", "arguments": {"path": "src"}}}
        ]
    }
    assert parse_tool_calls(structured) == [ToolCall("list_dir", {"path": "src"})]

    observed = {
        "content": json.dumps({"name": "list_dir", "arguments": {"path": "src"}})
    }
    assert parse_tool_calls(observed) == [ToolCall("list_dir", {"path": "src"})]


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"name":"read_file","arguments":{"path":"a.py","start":1,"end":5}}\n```',
        '<tool_call>{"name":"read_file","arguments":{"path":"a.py","start":1,"end":5}}</tool_call>',
    ],
)
def test_parses_wrapped_tool_calls(content: str) -> None:
    assert parse_tool_calls({"content": content}) == [
        ToolCall("read_file", {"path": "a.py", "start": 1, "end": 5})
    ]


def test_plain_prose_is_not_mistaken_for_tool_call() -> None:
    assert parse_tool_calls({"content": "The change is complete."}) == []


def test_parses_multiple_adjacent_json_tool_objects() -> None:
    content = (
        '{"name":"write_file","arguments":{"path":"a.py","content":"value = 1\\n"}}\n'
        '{"name":"run_validation","arguments":{}}'
    )
    assert parse_tool_calls({"content": content}) == [
        ToolCall("write_file", {"path": "a.py", "content": "value = 1\n"}),
        ToolCall("run_validation", {}),
    ]


def test_parses_explicit_json_tool_fence_inside_explanatory_prose() -> None:
    content = (
        "I will apply the change now.\n"
        "```json\n"
        '{"name":"run_validation","arguments":{}}\n'
        "```"
    )
    assert parse_tool_calls({"content": content}) == [ToolCall("run_validation", {})]


def test_parses_json_tool_call_from_python_fence_emitted_by_local_model() -> None:
    content = (
        "Let's run validation now.\n"
        "```python\n"
        '{"name": "run_validation", "arguments": {}}\n'
        "```\n"
        "If it fails I will repair it."
    )

    assert parse_tool_calls({"content": content}) == [ToolCall("run_validation", {})]


def test_parses_function_style_calls_emitted_by_local_model() -> None:
    content = (
        "I will create the files.\n"
        "```python\n"
        'write_file({"path":"alpha.py","content":"def value():\\n    return \'new-a\'\\n"})\n'
        'write_file({"path":"test_alpha.py","content":"from alpha import value\\n"})\n'
        "```\n"
        'Then validate: {"name":"run_validation","arguments":{}}'
    )

    assert parse_tool_calls({"content": content}) == [
        ToolCall("write_file", {"path": "alpha.py", "content": "def value():\n    return 'new-a'\n"}),
        ToolCall("write_file", {"path": "test_alpha.py", "content": "from alpha import value\n"}),
        ToolCall("run_validation", {}),
    ]


def test_malformed_call_is_rejected() -> None:
    with pytest.raises(ToolParseError, match="required argument"):
        parse_tool_calls({"content": '{"name":"write_file","arguments":{"path":"a.py"}}'})


def test_dispatcher_confines_paths_and_runs_fixed_validation(tmp_path) -> None:
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "sample.py").write_text("value = 1\n", encoding="utf-8")
    dispatcher = ToolDispatcher(root, validation=lambda: "pytest: passed")

    listing = dispatcher.dispatch(ToolCall("list_dir", {"path": "src"}))
    assert "sample.py" in listing

    dispatcher.dispatch(
        ToolCall(
            "replace_in_file",
            {"path": "src/sample.py", "old": "value = 1", "new": "value = 2"},
        )
    )
    assert (root / "src" / "sample.py").read_text(encoding="utf-8") == "value = 2\n"
    assert dispatcher.dispatch(ToolCall("run_validation", {})) == "pytest: passed"

    with pytest.raises(PermissionError, match="outside the worktree"):
        dispatcher.dispatch(ToolCall("read_file", {"path": "../secret.txt"}))


def test_dispatcher_denies_credential_files(tmp_path) -> None:
    (tmp_path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "credentials.json").write_text('{"token":"secret"}', encoding="utf-8")
    dispatcher = ToolDispatcher(tmp_path, validation=lambda: "validation passed")

    for name in (".env", "credentials.json"):
        with pytest.raises(PermissionError, match="sensitive"):
            dispatcher.dispatch(ToolCall("read_file", {"path": name}))
