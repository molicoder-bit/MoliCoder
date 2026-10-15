import json

import pytest

from local_model_chat.environment.manifests import load_manifests


def test_official_pinned_manifests_have_platform_trust_and_rollback() -> None:
    manifests = load_manifests()
    assert len(manifests) == 4
    assert {(item.tool_id, item.system) for item in manifests} == {
        ("ollama", "Windows"), ("ollama", "Linux"),
        ("kandev", "Windows"), ("kandev", "Linux"),
    }
    assert all(item.sha256 and item.license_url and item.rollback for item in manifests)
    assert all(item.signature_requirement for item in manifests)


@pytest.mark.parametrize("field,value,expected", [
    ("sha256", "unknown", "SHA-256"),
    ("source_url", "https://mirror.example/release", "official release"),
    ("version", "99.0", "version must match"),
    ("expected_executable", "../escape", "filename"),
])
def test_invalid_manifest_is_rejected(tmp_path, field, value, expected) -> None:
    source = load_manifests()[0]
    record = {key: getattr(source, key) for key in source.__dataclass_fields__}
    record[field] = value
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([record]), encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        load_manifests(path)
