# Local Model Chat

A private-first Windows desktop chat UI for two Ollama models:

- **Coding** — `local-coding:latest` (Qwen 2.5 Coder 7B, Q4_K_M)
- **Reasoning** — `local-reasoning:latest` (DeepSeek R1 0528 Qwen3 8B, Q6_K)

The app talks only to Ollama on `http://127.0.0.1:11434`. Conversations are stored locally in SQLite under `%LOCALAPPDATA%\LocalModelChat`; no cloud model or Codex involvement is required.

## Features

- Streaming chat with separate, collapsed reasoning output
- Persistent conversations, rename/delete, Markdown and JSON export
- Conservative context estimate, 80% warning, and 95% hard safety limit
- No silent history trimming
- Live NVIDIA VRAM/utilization, Ollama residency, token, and speed metrics
- Safe text/code attachments (UTF-8 allowlist, 1 MB each, 4 MB per message)
- Per-model generation controls and automatic model switching via a new chat
- Keyboard-first composing: Enter sends; Shift+Enter inserts a new line

## Run from source

Requirements: Windows 11, Python 3.13+, Ollama, and the two aliases above.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock
.\.venv\Scripts\python run_app.py
```

The app attempts to start Ollama if it is installed but not running. It never downloads or replaces models automatically.

## Build and install

```powershell
.\scripts\build.ps1
.\scripts\install.ps1
```

The build is written to `dist\LocalModelChat`. Installation copies it to `%LOCALAPPDATA%\Programs\LocalModelChat` and creates a desktop shortcut.

## Privacy and storage

- Chat database: `%LOCALAPPDATA%\LocalModelChat\chats.sqlite3`
- Default exports: `%LOCALAPPDATA%\LocalModelChat\exports`
- API endpoint: loopback only (`127.0.0.1`)
- Attached files are embedded as text in the prompt and saved with the message.

Deleting chats is permanent. Back up the SQLite file if the history matters.

## Tests

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python -m pytest
```
