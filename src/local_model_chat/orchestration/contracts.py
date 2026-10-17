"""Versioned wire contracts. Model proposals never own host completion state."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string", "maxLength": 12000}
STRINGS = {"type": "array", "items": TEXT, "maxItems": 24}
CHECK = obj({"id": TEXT, "kind": {"type": "string", "enum": [
    "file_equals", "file_contains", "file_exists", "directory_exists", "tool_evidence", "semantic_review"]},
    "target": TEXT, "expected": TEXT})
EVALUATION_SCHEMA = obj({
    "schema_version": {"const": "1.1"},
    "intent": {"type": "string", "enum": ["conversation", "investigation", "action", "build", "clarification"]},
    "objective": TEXT, "required_capabilities": STRINGS,
    'work_type': {'type': 'string', 'enum': ['direct_answer','extraction','analysis','planning','implementation','operation']},
    "complexity": {"type": "string", "enum": ["low", "medium", "high"]},
    "acceptance_checks": {"type": "array", "items": CHECK, "maxItems": 12},
    "missing_information": STRINGS, "reason": TEXT,
})
from .routing import ALIASES, HOST_TOOLS
EVALUATION_SCHEMA['properties']['required_capabilities'] = {'type': 'array', 'maxItems': 24,
    'items': {'type': 'string', 'enum': sorted(HOST_TOOLS | set(ALIASES) | {
        'text_generation', 'host_tools', 'tool_evidence', 'source_reasoning', 'reasoning',
        'code_generation', 'planning', 'image_understanding'})}}
ANSWER_REVIEW_SCHEMA = obj({'addresses_request': {'type': 'boolean'},
    'supported': {'type': 'boolean'}, 'reason': {'type': 'string', 'maxLength': 500}})

TOOL_ARGUMENTS = {
    "list_directory": obj({"path": TEXT}),
    "read_file": obj({"path": TEXT, "start": {"type": "integer", "minimum": 1},
                      "end": {"type": "integer", "minimum": 1}}),
    "search_code": obj({"query": TEXT}),
    "write_file": obj({"path": TEXT, "content": TEXT}),
    "replace_in_file": obj({"path": TEXT, "old": TEXT, "new": TEXT}),
    "create_directory": obj({"path": TEXT}),
    "project_status": obj({}),
    "run_check": obj({"check_id": TEXT}),
    "run_command": obj({"argv": {"type": "array", "minItems": 1, "maxItems": 32, "items": TEXT}}),
}

# Constrain the wire grammar, too: otherwise a small model can put a source-code
# expression in expected even though this check specifically requires a tool name.
EVALUATION_SCHEMA['properties']['acceptance_checks']['items'] = {'anyOf': [
    obj({**CHECK['properties'], 'kind': {'type': 'string', 'enum': [
        'file_equals', 'file_contains', 'file_exists', 'directory_exists', 'semantic_review']}}),
    obj({**CHECK['properties'], 'kind': {'const': 'tool_evidence'},
        'expected': {'type': 'string', 'enum': list(TOOL_ARGUMENTS)}}),
]}


def validate_evaluation(value):
    """Reject internally inconsistent task criteria before accepting a route's plan."""
    intent, checks = value['intent'], value['acceptance_checks']
    if 'source_reasoning' in value['required_capabilities'] and value['complexity'] == 'low':
        value['complexity'] = 'medium'
    if intent == 'conversation' and checks:
        raise ValueError('Conversation answers use acceptance_checks=[]; source/file questions use investigation')
    if intent == 'investigation' and (not checks or any(
            c['kind'] not in {'tool_evidence', 'semantic_review'} for c in checks)):
        raise ValueError('Investigation needs tool_evidence checks for actual reads/searches, not guessed file contents')
    if any(c['kind'] == 'tool_evidence' and c['expected'] not in TOOL_ARGUMENTS for c in checks):
        raise ValueError('tool_evidence.expected must name an available host tool')
    if intent == 'action' and (not checks or any(c['kind'] == 'semantic_review' for c in checks)):
        raise ValueError('Actions need concrete artifact/tool acceptance checks; semantic development belongs in Build')
    if intent == 'clarification' and not value['missing_information']:
        raise ValueError('Clarification requires a genuine unresolved user decision')
    return value


def turn_schema(evidence_ids=(), *, require_evidence=False, source_evidence_ids=(), allow_completion=True, allow_tools=True):
    fields = {
        "schema_version": {"const": "1.0"},
        "status": {"type": "string", "enum": ["needs_tools", "answer_ready", "work_ready", "cannot_complete", "needs_clarification"]},
        "message": TEXT,
        "tool_calls": {"type": "array", "maxItems": 3, "items": {"anyOf": [
            obj({"call_id": TEXT, "name": {"const": name}, "arguments": schema})
            for name, schema in TOOL_ARGUMENTS.items()]}},
        "evidence_refs": STRINGS, "unmet_criteria": STRINGS,
        "failure": {"type": ["string", "null"], "maxLength": 2000},
    }
    tool_fields = dict(fields, status={"type": "string", "const": "needs_tools"},
        tool_calls={**fields["tool_calls"], "minItems": 1})
    final_fields = dict(fields, status={"type": "string", "enum": ["answer_ready", "work_ready"]},
        tool_calls={"type": "array", "maxItems": 0, "items": obj({})})
    if require_evidence:
        final_fields['evidence_refs'] = {'type': 'array', 'minItems': 1, 'maxItems': 24,
            'items': {'type': 'string', 'enum': list(evidence_ids)}}
    unable_fields = dict(final_fields, status={'type': 'string', 'enum': ['cannot_complete', 'needs_clarification']},
        evidence_refs=STRINGS)
    if source_evidence_ids:
        final_fields['citations'] = {'type': 'array', 'minItems': 1, 'maxItems': 8,
            'items': obj({'evidence_id': {'type': 'string', 'enum': list(source_evidence_ids)},
                'path': TEXT, 'line': {'type': 'integer', 'minimum': 1}, 'quote': TEXT})}
    # Constrain the grammar itself: small models otherwise mix status and tool-call states.
    variants = [obj(tool_fields)] if allow_tools else []
    if allow_completion:
        variants.append(obj(final_fields))
    variants.append(obj(unable_fields))
    return {"anyOf": variants}


def validate(value: Any, schema: dict, path="$" ):
    """Strict subset used by these contracts, independent of provider SDKs."""
    if "anyOf" in schema:
        errors = []
        for candidate in schema["anyOf"]:
            try:
                validate(value, candidate, path)
                return value
            except ValueError as exc:
                status_schema = candidate.get('properties', {}).get('status', {})
                if isinstance(value, dict) and (value.get('status') == status_schema.get('const') or value.get('status') in status_schema.get('enum', [])):
                    errors.append(str(exc))
        if errors:
            raise ValueError(errors[0])
        raise ValueError(f"{path}: no supported variant")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path}: incorrect constant")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unsupported value")
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if kinds and not any(type(value) is types[k] for k in kinds):
        raise ValueError(f"{path}: wrong type")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if set(schema.get("required", ())) - value.keys():
            raise ValueError(f"{path}: missing required fields")
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            raise ValueError(f"{path}: unknown fields")
        for key, item in value.items():
            if key in props:
                validate(item, props[key], f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{path}: too few items")
        if len(value) > schema.get("maxItems", 100000):
            raise ValueError(f"{path}: too many items")
        for item in value:
            validate(item, schema.get("items", {}), path + "[]")
    if isinstance(value, str) and len(value) > schema.get("maxLength", 100000):
        raise ValueError(f"{path}: text too long")
    if type(value) is int and value < schema.get("minimum", value):
        raise ValueError(f"{path}: below minimum")
    return value


def decode(text, schema):
    # Reject prose, fenced instructions, extra documents, and duplicate keys.
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    return validate(json.loads(text, object_pairs_hook=unique), schema)


@dataclass
class RequestResult:
    content: str
    receipt: dict
    metrics: Any
