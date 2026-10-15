import pytest

from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest, InferenceResult


def test_request_requires_route_bounds_and_identity() -> None:
    request = InferenceRequest(
        request_id="r1", project_id=1, offering_id="ollama-local-coder",
        model_id="coder-v1", purpose=InferencePurpose.CHAT,
        messages=({"role": "user", "content": "Hi"},), sensitivity="private",
        required_capabilities=("stream",), max_input_tokens=100,
        max_output_tokens=20, timeout_seconds=30,
    )
    assert request.offering_id == "ollama-local-coder"
    assert request.reservation_id is None
    with pytest.raises(ValueError, match="offering"):
        InferenceRequest(
            "r1", 1, "", "coder-v1", InferencePurpose.CHAT,
            ({"role": "user", "content": "Hi"},), "private", (), 100, 20, 30,
        )
    with pytest.raises(ValueError, match="positive"):
        InferenceRequest(
            "r2", 1, "route", "coder-v1", InferencePurpose.CHAT,
            ({"role": "user", "content": "Hi"},), "private", (), 100, 0, 30,
        )


def test_missing_provider_usage_remains_unknown() -> None:
    result = InferenceResult("r1", "completed", "local", "coder-v1", "Hello")
    assert result.input_tokens is None
    assert result.output_tokens is None
