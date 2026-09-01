# MoliCoder

A local-first Windows desktop coding orchestrator and chat UI for two Ollama models:

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
- Persistent local-project registry with Python, Node, and Flutter detection
- Per-project `.molicoder` metadata, evidence, plans, task records, and run history
- Functional local-only Build workspace with guarded branches and a live event timeline
- Local Qwen multi-task planning with schema repair and dependency ordering
- Sequential validated execution with fresh-context repair and failure fingerprinting
- Isolated Git task worktrees, dependency scheduling, integration merging, and conflict repair
- Optional read-only Codex CLI planning, rescue, and integration review with local fallback
- Adaptive CPU/GPU overlap, reusable project skills, and run-effectiveness analytics
- Pause/resume, cooperative cancellation, diff inspection, and explicit validated-branch merge

## Local Build mode

For a new application, click **New** beside **Projects**, choose a parent folder, enter a
project name, and select Python, Node.js, or Flutter. MoliCoder creates the folder, language
marker, local metadata, Git repository, and initial commit, then opens a build-ready project.
Python projects also receive an isolated `.venv` with the pinned test runner; this one-time setup
downloads `pytest` from PyPI but requires no account or API key.

To work on an existing repository, click **Add** and select it. Existing projects must start from
a clean, named Git branch.
Enter a small, mechanically verifiable request and MoliCoder will
collect evidence, produce a validated plan, execute dependency-ready tasks with safe overlap, run the
project's fixed validation commands, and retry bounded failures with fresh context. Task edits
occur in isolated `molicoder/run-*/...` branches; the current checkout is changed only when you
explicitly confirm **Merge integration branch**.

Build modes:

- `LOCAL_ONLY`: local Qwen planning and execution; no subscription usage.
- `AUTO`: local by default, Codex planning for security/auth/payment/migration ambiguity, with local fallback.
- `FRONTIER_PLAN`: Codex plans and reviews; local Qwen implements and repairs.
- `MANUAL_PLAN`: paste a plan JSON object matching `.molicoder`'s plan schema.

External modes ask for confirmation before spending subscription quota. Codex runs read-only,
has hard per-run call ceilings, and never edits the repository.

Reusable project guidance can be stored in `.molicoder/skills/<name>.md`. The first line is
`<!-- keywords: pytest, testing -->`; matching skills are injected only into relevant tasks.

The guarded executor command remains available for a single hand-written task:

```powershell
git switch -c molicoder/example-task
.\.venv\Scripts\molicoder-exec.exe --project . --yes "A small, explicit, testable task"
```

The single-task CLI still requires a dedicated `molicoder/<task>` branch. The desktop Build
workspace accepts `main` because all autonomous edits occur in isolated worktrees. Both refuse
dirty or detached checkouts and projects without a fixed validation allowlist. The model never
chooses shell commands.

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
Launch the installed app from the **MoliCoder** desktop shortcut or run:

```powershell
& "$env:LOCALAPPDATA\Programs\MoliCoder\MoliCoder.exe"
```

The build performs a packaged-import health check before it can succeed. Its PyInstaller
configuration also excludes unrelated ICU DLLs inherited from developer-tool `PATH` entries,
which would otherwise conflict with Qt's Windows system interface.

## Privacy and storage

- Chat database: `%LOCALAPPDATA%\LocalModelChat\chats.sqlite3`
- Default exports: `%LOCALAPPDATA%\LocalModelChat\exports`
- API endpoint: loopback only (`127.0.0.1`)
- Task worktrees: `%LOCALAPPDATA%\LocalModelChat\worktrees`
- Attached files are embedded as text in the prompt and saved with the message.

Deleting chats is permanent. Back up the SQLite file if the history matters.

## Tests

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python -m pytest
```

Real Ollama tests are opt-in and operate only in temporary repositories:

```powershell
$env:MOLICODER_REAL_MODEL = "1"
.\.venv\Scripts\python -m pytest tests\test_molicoder_real_model.py
```

The full frontier workflow additionally requires explicit quota authorization:

```powershell
$env:MOLICODER_REAL_MODEL = "1"
$env:MOLICODER_REAL_CODEX = "1"
.\.venv\Scripts\python -m pytest tests\test_molicoder_real_model.py
```
