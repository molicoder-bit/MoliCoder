# MoliCoder

**A local-first desktop chat and coding workspace for people who want to see and control where their tokens go.**

MoliCoder combines Ollama chat, project-aware answers, and a guarded Build workflow. You can keep routine work on local models, set visible context and output limits, and choose when an external coding agent is worth using. The app records what it can measure and labels unknown subscription usage as unknown.

> **Platform checks:** Windows 11, macOS Intel, and Linux x64 have automated tests and packaged startup checks. macOS/Linux builds are unsigned CI artifacts with the [limits documented here](docs/plans/platform-support.md).

## Why token control matters

Long chat histories, oversized prompts, unnecessary model calls, and automatic retries can consume context or paid allowance without improving the result. MoliCoder gives you a way to inspect those decisions before and after a run:

| Control | What it helps with | Important limit |
| --- | --- | --- |
| Context meter and reserved output tokens | See the estimated prompt size before sending; get a warning near 80% of the configured context window and a block at the 95% safety threshold. Chat history is never trimmed silently. | The pre-send count is an estimate; Ollama reports the actual count after generation. |
| Local Ollama route | Use your own hardware for chat and local Build tasks without metered API token charges. | Local work still uses compute, memory, and electricity; model quality depends on your setup. |
| Source-backed project answers | Ask about the current task, version, or changed files using Git and saved run state without another inference call. | Only questions the deterministic handler recognizes take this path. |
| Explicit external Build selection | Choose a provider and strategy, review the route, and authorize an external agent run. Call and time ceilings bound MoliCoder's CLI launches. | A subscription CLI can make internal calls MoliCoder cannot count or price. The ceiling is **not** a hard token or spend cap inside that CLI. |
| Usage and budget records | Inspect input/output tokens, route reasons, recorded requests, and actual, estimated, or unknown costs separately. Project and task reservations guard registered metered API requests. | Provider subscription balances and opaque agent costs may be unavailable. Unknown is never shown as zero. |
| Validation and recovery evidence | See task checkpoints, failed attempts, model handoffs, and validation results before relying on generated changes. | Passing tests do not prove every behavior; review changes before pushing. |

The goal is more predictable use of local hardware and paid services, with fewer needless model calls and clearer evidence about what happened. Savings depend on your workload and provider terms; MoliCoder does not promise a fixed reduction in tokens or cost. The metered API policy layer is separate from the default desktop routes and cannot cap internal usage inside a subscription CLI.

For example, a question such as “What task are we on?” can be answered from the saved run record without sending a model prompt. A small code change can run through Local offline Build. If that route cannot handle the task, you can deliberately choose an external agent, inspect the result, and see which usage figures are known. This keeps the decision to spend external allowance with you.

## What works today

- **Chat:** stream replies from local Ollama models; adjust output length, temperature, and top-p; inspect prompt/output metrics; keep conversations and export Markdown or JSON.
- **Project context:** select a local Git project, inspect authoritative run status and project knowledge, and get source-backed answers to supported status questions.
- **Build:** plan and execute tasks in isolated Git worktrees, run fixed validation commands, preserve failed work for review, and record task evidence.
- **Routes:** choose Local offline, Hybrid, or Frontier only. External CLI agents require project trust and an explicit Build authorization.
- **Visibility:** inspect provider state, environment doctor results, usage and budgets, event timelines, worktrees, and a privacy-filtered trace export.
- **Optional Kandev ownership:** explicitly assign a Build task to a configured local Kandev service and reconcile it by its saved external ID.

Local Ollama chat needs no cloud account. Optional external agents and services have their own installation, authentication, quota, and data-sharing rules.

## Run from source

Requirements: Python 3.13+, Git, and Ollama on a tested platform. The Chat presets use Ollama model names `molicoder:latest` and `local-reasoning:latest`; any other installed Ollama model also appears in Chat with conservative default generation settings. Environment readiness can inspect a public Ollama library model's official manifest, show its size and hardware estimate, and download it through Ollama after your exact approval. It does not replace model weights without approval.

```powershell
git clone https://github.com/molicoder-bit/MoliCoder.git
cd MoliCoder
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
ollama list
.\.venv\Scripts\python.exe run_app.py
```

The app connects to Ollama on `http://127.0.0.1:11434` and tries to start the local Ollama service if it is installed but stopped. `ollama list` should show the expected aliases before you start a model-backed chat or Local offline Build.

On macOS or Linux, replace the virtual-environment commands above with:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python run_app.py
```

For an unsigned macOS Intel or Linux x64 package, run `.venv/bin/python scripts/build_unix.py` after installing dependencies. Automated package startup and behavior checks are in [platform support](docs/plans/platform-support.md). Metered API routes are currently unavailable on macOS and Linux because MoliCoder has no verified encrypted key store there; local Ollama and supported official CLI sign-in flows remain separate.

### First steps

1. Open **Chat** for local conversation. Watch the context meter and choose an output limit before sending a large request.
2. Add an existing Git repository under **Projects**, or create a Python, Node.js, or Flutter starter project. An existing repository needs a clean, named branch for Build.
3. In **Build**, enter a small request with a checkable outcome. Choose **Local offline** to keep model work on Ollama, or choose an external agent deliberately.
4. Review **Task evidence** and **Usage and budgets** after the run. Inspect the diff before pushing it.

New Python starter projects create an isolated virtual environment and install the pinned test runner from PyPI. That setup needs network access but no model provider account.

## Build routes and safeguards

| Strategy | Execution path | Token and cost visibility |
| --- | --- | --- |
| **Local offline** | Local Ollama planning and bounded local execution. | Ollama usage is recorded when reported; no metered inference API charge. |
| **Hybrid** | A selected external CLI plans; bounded local execution can handle eligible low-risk edits, with provider takeover when needed. | Local usage is measurable; the CLI's internal subscription use remains opaque. |
| **Frontier only** | The selected external CLI plans and executes in the isolated worktree. | MoliCoder records observed calls and any usage the CLI reports; exact internal cost may be unknown. |

Build supports Codex, Claude, and Copilot CLI profiles when installed, authenticated, and eligible. An optional fallback provider must be selected and trusted separately. External agents can edit files in the isolated worktree; their own native tools may have side effects outside MoliCoder's controlled tool path. After an uncertain opaque-agent outcome, MoliCoder stops automatic retry and asks for review.

Internal Build starts from a clean named branch. It validates and checkpoints changes in an isolated worktree, then fast-forwards the checkout automatically **only when its safety checks allow it**. Otherwise, it preserves the result for review and a separate apply action. Kandev-owned tasks require local acceptance and an explicit apply. No Build route pushes commits to a remote repository.

The desktop exposes current run status, validation output, and recovery actions. Project-local `.molicoder` files hold plans and run logs; the local SQLite database holds authoritative run and task state.

### Optional Kandev owner

Build defaults to **Internal Build**. To select Kandev ownership, configure a local Kandev 0.97.0 service and set these environment variables for the app process:

```text
KANDEV_ENDPOINT=http://127.0.0.1:38429/mcp
KANDEV_VERSION=0.97.0
KANDEV_WORKSPACE_ID=...
KANDEV_WORKFLOW_ID=...
KANDEV_WORKFLOW_STEP_ID=...
KANDEV_REPOSITORY_ID=...
KANDEV_AGENT_PROFILE_ID=...
```

The endpoint must be loopback HTTP. Choose **Kandev (configured local service)** in Build and confirm the agent launch. MoliCoder records ownership before dispatch, requires exact create and launch authorizations, shows pending state, and reconciles by the saved external ID. A [live Windows readiness check](docs/plans/kandev-live-readiness.md) verified an isolated worktree, local acceptance and apply, and restart recovery with Kandev 0.97.0. Its ordinary Windows launcher failed during supervisor socket setup on that host; the check used the verified executable's direct backend entry point.

## Build a Windows app

```powershell
.\scripts\build.ps1
.\scripts\install.ps1
```

The build is written to `dist\MoliCoder`. The installer copies it to `%LOCALAPPDATA%\Programs\MoliCoder` and creates a desktop shortcut. Run from source first if you want to inspect behavior before installing.

## Privacy and local data

- Chat database: `%LOCALAPPDATA%\LocalModelChat\chats.sqlite3`
- Default exports: `%LOCALAPPDATA%\LocalModelChat\exports`
- Task worktrees: `%LOCALAPPDATA%\LocalModelChat\worktrees`
- Project run logs: `<project>\.molicoder\logs\run-<id>.jsonl`

Local Ollama requests stay on the loopback service. Selecting an external CLI or Kandev route sends task context to that service under its own terms. Attached text and run evidence may contain sensitive project content; review exports and logs before sharing them. Deleting a chat is permanent, so back up the SQLite file if the history matters.

## Open-source license

MoliCoder's source code is licensed under the [Apache License 2.0](LICENSE). You can use, modify, and distribute it, including in commercial projects, under that license's terms. It includes an express patent grant and requires preservation of applicable license and attribution notices. The [NOTICE](NOTICE) file identifies this project.

The license covers this repository's MoliCoder code and documentation. Ollama, PySide6/Qt, optional coding agents, Kandev, model weights, and other external dependencies keep their own licenses and terms; check those separately when distributing a packaged app or models.

## Tests and project status

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest -q
```

The no-cost suite uses fixtures for provider and Kandev behavior. Live Ollama and CLI tests are opt-in; an isolated Kandev readiness check is documented separately. The [platform transformation tracker](docs/plans/platform-transformation-tracker.md) shows completed work and remaining tasks, including measured routing economics and optional remote workers. Issues and feedback are welcome; include reproduction steps and avoid posting credentials or private run logs.
