import json
from pathlib import Path

import pytest

from local_model_chat.domain import GenerationMetrics
from local_model_chat.ollama import ChatResponse
from local_model_chat.orchestration.coordinator import RequestCoordinator
from local_model_chat.orchestration.contracts import EVALUATION_SCHEMA, decode
from local_model_chat.storage import ChatStore


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def tags(self):
        return [{"name": "molicoder:latest", "digest": "a"}, {"name": "local-coding:latest", "digest": "b"}]

    def chat(self, model, messages, **kwargs):
        self.calls.append((model, messages))
        if 'addresses_request' in kwargs.get('response_format', {}).get('properties', {}):
            return ChatResponse({'content': json.dumps({'addresses_request': True, 'supported': True,
                'reason': 'Scripted review; real semantic qualification is a separate integration test'})}, GenerationMetrics())
        value = self.responses.pop(0)
        if callable(value):
            value = value(messages)
        if isinstance(value, Exception):
            raise value
        return ChatResponse({"content": json.dumps(value)}, GenerationMetrics(prompt_tokens=10, output_tokens=5))


def request_payload(messages):
    system = next(m['content'] for m in messages if m['role'] == 'system')
    return json.loads(system.split('APPLICATION CONTEXT (literal data):\n', 1)[1])


def evaluation(intent="action", checks=None):
    return {"schema_version": "1.0", "intent": intent, "objective": "Create Iworked.txt containing yeay",
        "required_capabilities": ["file_write"], "complexity": "low", "acceptance_checks": checks if checks is not None else [
            {"id": "content", "kind": "file_equals", "target": "Iworked.txt", "expected": "yeay"}],
        "missing_information": [], "reason": "Bounded local operation"}


def turn(status, calls=None, refs=None, **kwargs):
    return {"schema_version": "1.0", "status": status, "message": "Done", "tool_calls": calls or [],
        "evidence_refs": refs or [], "unmet_criteria": [], "failure": None, **kwargs}


def write_call():
    return {"call_id": "write-1", "name": "write_file", "arguments": {"path": "Iworked.txt", "content": "yeay"}}


def final_with_evidence(messages):
    outputs = [json.loads(m["content"].split(": ", 1)[1]) for m in messages if m["content"].startswith("HOST TOOL RESULT")]
    return turn("work_ready", refs=[outputs[-1]["evidence_id"]])


def service(tmp_path, responses, **kwargs):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    store = ChatStore(tmp_path / "profile.sqlite3")
    project = store.upsert_project("project", str(root), "generic")
    client = Client(responses)
    store.set_setting("orchestration.qualification.v1", json.dumps({"local-coding:latest": {
        "enabled_for_fallback": True, "source": "isolated test fixture", "passed": 3}}))
    return RequestCoordinator(store, project, client, allow_external=False, **kwargs), client, root


def test_requested_file_contents_are_written_and_checked_on_disk(tmp_path):
    reviews = []
    coordinator, client, root = service(tmp_path,
        [evaluation(), turn("needs_tools", [write_call()]), final_with_evidence],
        review=lambda p: reviews.append(p) or True)
    result = coordinator.run('Create a file called Iworked.txt with the text "yeay" in it')
    assert result.receipt["outcome"] == "completed"
    assert (root / "Iworked.txt").read_bytes() == b"yeay"
    assert result.receipt["checks"][0]["status"] == "passed"
    assert len(reviews) == 1
    with coordinator.store._connect() as db:
        assert db.execute("SELECT state FROM orchestration_requests").fetchone()[0] == "completed"


def test_advice_instead_of_execution_does_not_complete(tmp_path):
    coordinator, client, root = service(tmp_path, [evaluation(), turn("answer_ready", message='Use echo "yeay" > Iworked.txt'),
        turn("work_ready", refs=["made-up-evidence"])])
    result = coordinator.run("Create Iworked.txt with yeay")
    assert result.receipt["outcome"] == "failed"
    assert not (root / "Iworked.txt").exists()
    assert len(result.receipt["attempts"]) == 2


def test_declined_write_stops_without_fallback(tmp_path):
    coordinator, client, root = service(tmp_path, [evaluation(), turn("needs_tools", [write_call()])])
    result = coordinator.run("Create Iworked.txt with yeay")
    assert result.receipt["outcome"] == "blocked"
    assert len(result.receipt["attempts"]) == 1
    assert not (root / "Iworked.txt").exists()


def test_fallback_preserves_completed_write_and_does_not_replay_it(tmp_path):
    def resumed(messages):
        payload = request_payload(messages)
        assert payload["completed_tools"]
        return turn("work_ready", refs=list(payload["completed_tools"]))
    checks = evaluation()["acceptance_checks"] + [{"id": "readback", "kind": "tool_evidence", "target": ".", "expected": "read_file"}]
    def readback(messages):
        payload = request_payload(messages)
        assert payload["completed_tools"]
        return turn("needs_tools", [{"call_id": "read", "name": "read_file", "arguments": {
            "path": "Iworked.txt", "start": 1, "end": 10}}])
    coordinator, client, root = service(tmp_path, [evaluation(checks=checks), turn("needs_tools", [write_call()]),
        RuntimeError("connection timed out after write"), readback], review=lambda p: True)
    result = coordinator.run("Create Iworked.txt with yeay")
    assert result.receipt["outcome"] == "completed"
    assert (root / "Iworked.txt").read_bytes() == b"yeay"
    assert sum(e['tool'] == 'write_file' for e in result.receipt['evidence'].values()) == 1
    assert sum(e['tool'] == 'read_file' for e in result.receipt['evidence'].values()) == 1
    assert client.calls[-1][0] != client.calls[0][0]


def test_wrong_bytes_cannot_pass_host_checks(tmp_path):
    call = write_call()
    call["arguments"]["content"] = "wrong"
    coordinator, client, root = service(tmp_path, [evaluation(), turn("needs_tools", [call]), final_with_evidence,
        turn("work_ready", refs=[])], review=lambda p: True)
    result = coordinator.run("Create Iworked.txt with yeay")
    assert result.receipt["outcome"] == "failed"
    assert not (root / "Iworked.txt").exists()
    with coordinator.store._connect() as db:
        assert any('differ from the evaluated acceptance' in r[0] for r in db.execute(
            "SELECT payload_json FROM orchestration_events WHERE kind='tool_error'"))


def test_path_escape_is_blocked_before_review_or_mutation(tmp_path):
    call = write_call()
    call["arguments"]["path"] = "../escape.txt"
    coordinator, client, root = service(tmp_path, [evaluation(), turn("needs_tools", [call])],
        review=lambda p: pytest.fail("Must reject the path before review"))
    assert coordinator.run("Create a file").receipt["outcome"] == "blocked"
    assert not (root.parent / "escape.txt").exists()


def test_no_project_is_a_truthful_blocker(tmp_path):
    store = ChatStore(tmp_path / "profile.sqlite3")
    result = RequestCoordinator(store, None, Client([evaluation()]), allow_external=False).run("Create a file")
    assert result.receipt["outcome"] == "blocked"
    assert "Select a project" in result.content


def test_schema_rejects_unknown_and_duplicate_fields():
    payload = evaluation()
    payload["verified"] = True
    with pytest.raises(ValueError):
        decode(json.dumps(payload), EVALUATION_SCHEMA)
    with pytest.raises(ValueError, match="Duplicate"):
        decode('{"intent":"action","intent":"conversation"}', EVALUATION_SCHEMA)


def test_action_followup_uses_history_instead_of_directory_clarification(tmp_path):
    coordinator, client, root = service(tmp_path, [evaluation(), turn('needs_tools', [write_call()])], review=lambda p: True)
    result = coordinator.run('Can you do it now?', [
        {'role': 'user', 'content': 'Create Iworked.txt with the text yeay'},
        {'role': 'assistant', 'content': 'You can use echo in a terminal.'}])
    assert result.receipt['outcome'] == 'completed'
    assert (root / 'Iworked.txt').read_bytes() == b'yeay'
    assert 'Create Iworked.txt with the text yeay' in json.dumps(client.calls[0][1])


def test_catalog_preserves_disabled_and_offline_facts(tmp_path):
    from local_model_chat.orchestration.catalog import CapabilityCatalogService
    coordinator, client, root = service(tmp_path, [])
    catalog = CapabilityCatalogService(coordinator.store, client)
    facts = catalog.refresh(discover_runtimes=False)
    offering = catalog.registry.list_offerings()[0]
    catalog.registry.set_offering_state(offering.id, status="disabled", actor="test", reason="user choice")
    catalog.refresh(discover_runtimes=False)
    assert catalog.registry.get_offering(offering.id).status == "disabled"
    client.tags = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    offline = catalog.refresh(discover_runtimes=False)
    assert offline["stale"]
    assert offline["models"] == facts["models"]
