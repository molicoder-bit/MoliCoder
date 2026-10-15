from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "MoliCoder"
APP_SLUG = "LocalModelChat"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
SAFE_CONTEXT_FRACTION = 0.95
WARNING_CONTEXT_FRACTION = 0.80


@dataclass(frozen=True, slots=True)
class ModelPreset:
    name: str
    label: str
    role: str
    context_window: int
    max_output_tokens: int
    temperature: float
    top_p: float
    accent: str
    system_prompt: str
    show_thinking: bool = False


MODEL_PRESETS: dict[str, ModelPreset] = {
    "local-reasoning:latest": ModelPreset(
        name="local-reasoning:latest",
        label="Reasoning",
        role="DeepSeek R1 · Q4_K_M",
        context_window=8192,
        max_output_tokens=4096,
        temperature=0.6,
        top_p=0.95,
        accent="#F59E0B",
        system_prompt=(
            "Reason carefully and verify the result. Keep the reasoning focused, "
            "then provide a clearly marked final answer."
        ),
        show_thinking=True,
    ),
    "molicoder:latest": ModelPreset(
        name="molicoder:latest",
        label="MoliCoder",
        role="Qwen 2.5 Coder · Q4_K_M",
        context_window=16384,
        max_output_tokens=2048,
        temperature=0.2,
        top_p=0.9,
        accent="#22D3EE",
        system_prompt=(
            "You are a precise coding assistant. Prefer correct, minimal solutions, "
            "state assumptions, and format code in fenced blocks."
        ),
    ),
}


def data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = root / APP_SLUG
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return data_dir() / "chats.sqlite3"


def export_dir() -> Path:
    path = data_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path
