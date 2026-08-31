from local_model_chat.config import MODEL_PRESETS


def test_reasoning_preset_matches_installed_quantization() -> None:
    preset = MODEL_PRESETS["local-reasoning:latest"]
    assert "Q4_K_M" in preset.role
    assert preset.context_window == 8192
    assert preset.max_output_tokens == 4096


def test_executor_preset_uses_measured_full_gpu_context() -> None:
    preset = MODEL_PRESETS["molicoder:latest"]
    assert preset.context_window == 16384
    assert preset.max_output_tokens == 2048
