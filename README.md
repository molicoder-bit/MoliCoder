# MoliCoder

A private-first Windows desktop chat UI and local coding-executor foundation for two Ollama models:

- **MoliCoder** — `molicoder:latest` (Qwen 2.5 Coder 7B, Q4_K_M)
- **Reasoning** — `local-reasoning:latest` (DeepSeek R1 0528 Qwen3 8B, Q4_K_M)

The app talks only to Ollama on `http://127.0.0.1:11434`. Conversations are stored locally in SQLite under `%LOCALAPPDATA%\LocalModelChat`; no cloud model or Codex involvement is required.

## Features

- Streaming chat with separate, collapsed reasoning output
- Clean final-answer callouts for DeepSeek's boxed LaTeX prose
- Persistent conversations, rename/delete, Markdown and JSON export
- Conservative context estimate, 80% warning, and 95% hard safety limit
- No silent history trimming
- Live NVIDIA VRAM/utilization, Ollama residency, token, and speed metrics
- Safe text/code attachments (UTF-8 allowlist, 1 MB each, 4 MB per message)
- Per-model generation controls and automatic model switching via a new chat
- Keyboard-first composing: Enter sends; Shift+Enter inserts a new line
- Bounded local executor with a serialized inference lane and mandatory validation gate
- Tolerant Ollama tool parsing, including Qwen's bare and fenced JSON variants
- Worktree-confined file tools, sensitive-path denial, and fixed validation commands

## Phase 1 executor proof

The orchestration foundation is intentionally available as a guarded command before it is
connected to the desktop Build view. It currently supports Python/pytest projects. Run it
only from a clean, dedicated `molicoder/<task>` branch:

```powershell
git switch -c molicoder/example-task
.\.venv\Scripts\molicoder-exec.exe --project . --yes "A small, explicit, testable task"
```

The command refuses `main`, `master`, unrelated branch names, dirty worktrees, and projects
without fixed pytest validation. It never lets the model choose shell commands.

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

The build is written to `dist\MoliCoder`. Installation copies it to `%LOCALAPPDATA%\Programs\MoliCoder` and creates a desktop shortcut.

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

The real Ollama executor test is opt-in and operates only in a temporary project:

```powershell
$env:MOLICODER_REAL_MODEL = "1"
.\.venv\Scripts\python -m pytest tests\test_molicoder_real_model.py
```
