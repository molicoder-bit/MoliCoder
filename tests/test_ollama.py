from local_model_chat.ollama import _metrics_from_response


def test_metrics_conversion() -> None:
    metrics = _metrics_from_response(
        {
            "prompt_eval_count": 100,
            "prompt_eval_duration": 2_000_000_000,
            "eval_count": 40,
            "eval_duration": 1_000_000_000,
            "load_duration": 500_000_000,
            "done_reason": "stop",
        },
        4.25,
    )
    assert metrics.prompt_tokens_per_second == 50.0
    assert metrics.output_tokens_per_second == 40.0
    assert metrics.load_seconds == 0.5
    assert metrics.total_seconds == 4.25
