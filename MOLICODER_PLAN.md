# MoliCoder — Implementation Plan

Converting the existing `LocalModelChat` PySide6 application into a local-first
software-development orchestration system.

**Status:** Phases 0–5 are implemented as of 2026-08-31. MoliCoder 1.0 includes local
chat, isolated local execution, optional Codex CLI consulting, and resource analytics.
Originally written against commit `c2957e5`, verified against the live Ollama install and GPU on this machine.

### Implemented milestone — Phase 2 planning and repair

- Added deterministic repository reconnaissance with source-referenced evidence packets,
  secret exclusion, relevant-file ranking, tests, constraints, and Git risks.
- Added the local Qwen planner abstraction, strict multi-task plan schema, dependency
  ordering, and one bounded schema-repair attempt.
- Added persisted sequential orchestration across runs, plans, tasks, dependencies, and
  event history, with matching JSON artifacts under `.molicoder/`.
- Added fresh-context repair attempts, local diagnosis, normalized failure fingerprints,
  and stuck/oscillating failure detection.
- Activated the desktop Build workspace with branch and cleanliness guards, Enter-to-run,
  fixed project validation, and a live event timeline.
- Verified the real installed Qwen model through both the edit/validation loop and valid
  multi-task planning; deterministic repair tests cover progress and genuine validation
  failure handling without placing live inference in the default suite.

### Implemented milestone — Phase 3 orchestration

- Added dependency-aware scheduling with expected-path conflict exclusion.
- Runs independent ready tasks in isolated Git branches and worktrees while the serialized
  Ollama gateway preserves the machine's single inference lane.
- Commits task results independently, merges them into a separate integration worktree,
  validates the integrated result, and keeps the user's checkout untouched.
- Detects real Git conflicts by path and gives the local repair loop one bounded resolution
  path before integration validation.
- Added pause, resume, cooperative cancel, integrated diff inspection, and an explicit,
  user-confirmed merge control in the Build view.

### Implemented milestone — Phase 4 external intelligence

- Added the Codex CLI planner with binary discovery, stdin packets, read-only sandboxing,
  schema-constrained output, JSONL usage capture, ephemeral sessions, and timeouts.
- Isolates the adapter from incompatible user configuration while preserving OAuth login;
  missing binaries, invalid login, quota exhaustion, and nonzero exits fall back locally.
- Added `LOCAL_ONLY`, `AUTO`, `FRONTIER_PLAN`, and `MANUAL_PLAN` routing with readable
  high-risk AUTO heuristics and hard planning/rescue/total external-call ceilings.
- Added one bounded read-only frontier rescue strategy after genuine local stagnation and
  optional frontier review of the validated integration branch.
- Records every external call by purpose, reason, tokens, duration, and status; OAuth calls
  intentionally have no fabricated dollar estimate.

### Implemented milestone — Phase 5 resource optimization

- Added adaptive deterministic worker selection by project type and observed validation vs.
  inference cost; CPU validation can overlap another task's serialized GPU inference.
- Added reusable, keyword-matched project skills under `.molicoder/skills/`.
- Records local worker tokens, duration, status, repair fingerprints, and interruption state.
- Added project analytics comparing local-only and externally assisted run outcomes, surfaced
  in the Build workspace after each run.
- Expanded opt-in integration coverage to real local multi-task orchestration and a full
  frontier-plan/local-execute/frontier-review workflow.

### Implemented milestone — Phase 1 executor proof

- Added a serialized local inference gateway and non-streaming Ollama tool calls.
- Added the six confined tools, while the Phase 1 small-file policy exposes full-file
  writes and withholds exact replacement from the active model menu by default.
- Tolerates structured calls, bare JSON, adjacent JSON objects, JSON fences, embedded
  JSON fences, and `<tool_call>` wrappers observed from the installed Qwen model.
- Normalizes fallback calls back into Ollama's structured conversation format.
- Added fixed-command validation with saved logs, error extraction, timeouts, and
  fail-fast behavior.
- Completion is gated deterministically on passing validation; model prose cannot
  override a failing test.
- Executor paths are worktree-confined and credential-like paths are denied.
- Added a guarded `molicoder-exec` Python/pytest entry point and an opt-in real-model
  integration test that operates only in a temporary project.
- The real `molicoder:latest` model completed the isolated edit-and-pytest proof; the
  packaged Windows application also passed startup smoke testing.

### Implemented milestone — Phase 0 foundation

- Renamed the private GitHub repository, packaged application, installer, shortcut, and
  local executor alias to MoliCoder while preserving the existing chat database location.
- Added a persistent project registry with add, select, restart recovery, and remove-only-
  from-registry semantics.
- Added deterministic Python, Node, Flutter, and unknown project detection.
- Added `.molicoder/` project metadata, instructions, plans, tasks, runs, logs, and failure
  directories without overwriting user-authored instructions.
- Added the complete orchestration SQLite schema and supporting indexes.
- Added the Projects sidebar and Chat/Build view shell; the original chat experience and
  conversation persistence remain functional.

---

## 1. Purpose

MoliCoder allocates work to the cheapest computational resource that can do it reliably:

```
deterministic software -> local tools -> local Qwen -> fresh local Qwen
    -> local critique/ensemble -> external frontier reasoning
```

The goal is **not** to minimize external calls. It is to avoid spending frontier
intelligence on work local resources handle adequately, while still escalating when
escalation has positive expected value. Quality is a constraint, not a variable.

A run that burns 2M local tokens and 90 minutes to produce a poor result is a failure,
even at zero external cost.

---

## 2. Measured hardware and model reality

Everything below was measured on this machine, not assumed. These numbers drive the
architecture, so they are recorded here for future reference.

| Property | Measured value |
|---|---|
| GPU | NVIDIA GeForce RTX 5060, 8151 MiB VRAM, driver 595.95 |
| Ollama | 0.32.15 at `127.0.0.1:11434` |
| Executor model | `local-coding:latest` = Qwen 2.5 Coder 7B instruct Q4_K_M (4.36 GiB, native ctx 32768, caps: completion / **tools** / insert) |
| Reasoning model | `local-reasoning:latest` = DeepSeek R1 0528 Qwen3 8B Q4_K_M (4.68 GiB, native ctx 131072, caps: completion / **thinking**, **no tools**) |

### 2.1 VRAM ceiling (the binding constraint)

| Executor context | Footprint | GPU residency |
|---|---|---|
| 16384 | 5.09 GiB | **100% GPU** |
| 32768 | 6.35 GiB | 92% GPU — **spills to CPU** |

**16K is the executor sweet spot.** 32K spills and costs throughput. The repo history
already reflects this pressure: commit `636a69c` downgraded the reasoning model from
Q6_K to Q4_K_M specifically to regain full GPU residency.

Consequence: **only one model is resident at a time.** Coder + reasoning together do not
fit. Every switch between them is a real eviction and reload.

### 2.2 Throughput

| Measurement | Value |
|---|---|
| Output generation | 73–80 tok/s |
| Prefill, large prompt (6635 tok) | ~2985 tok/s |
| Prefill, tiny prompt (51 tok) | ~685 tok/s — dominated by fixed overhead, not representative |
| Cold model load | ~3.2 s |

**Correction worth recording:** a first measurement on a 51-token prompt suggested
prefill was ~685 tok/s, which would have made large contexts painfully slow and pushed
the design toward aggressive context minimization. Re-measuring with a realistic 6.6K
prompt showed ~2985 tok/s. Context is cheap; **output tokens are the bottleneck.**

Derived cost model for one executor turn:

```
8K prompt prefill      ~2.7 s
500 output tokens      ~6.5 s
-----------------------------------
per agent turn         ~9-10 s
8-turn task attempt    ~80 s + validation
realistic task         ~2-3 min
6-task feature         ~20-40 min including a repair cycle or two
```

Local-first is genuinely viable on this hardware. That is the finding that makes this
project worth building rather than a token-saving fantasy.

### 2.3 Tool calling is broken in the default path (must-fix)

Reproducible across repeated runs, on **both** `local-coding:latest` and the base
`qwen2.5-coder:7b-instruct-q4_K_M`:

```
request : tools=[{list_dir...}]
response: message.tool_calls == null
          message.content    == '{"name": "list_dir", "arguments": {"path": "src"}}'
```

The model emits a bare JSON object in `content` instead of the `<tool_call>` wrapper
Ollama's parser expects, so `tool_calls` never populates. Both models ship the identical
1615-char template containing `.Tools`, so this is **the model's behaviour, not a broken
custom Modelfile**.

**Any naive `for call in message.tool_calls:` agent loop will do nothing on this stack.**
A tolerant parser is therefore a mandatory Phase 1 component, not a nicety (see §6.2).

### 2.4 Toolchain and the Codex integration surface

Verified present on this machine:

| Tool | Version | Relevance |
|---|---|---|
| git | 2.51.2.windows.1 | worktrees available |
| node / npm | 24.11.1 / 11.6.2 | web target validation |
| Flutter / Dart | 3.47.0 stable / 3.13.0 | Flutter target validation |
| Codex CLI | 0.130.0-alpha.5 | external reasoning provider |

Two findings that shape the provider adapter:

**Codex is installed but not on `PATH`.** The binary lives at
`%LOCALAPPDATA%\OpenAI\Codex\bin\codex.exe`; neither `command -v codex` nor PowerShell's
`Get-Command codex` resolves it. The adapter must **discover or be configured with an
explicit path**, never assume `codex` resolves.

**Auth is ChatGPT-subscription OAuth, not an API key.** `~/.codex/auth.json` contains
`auth_mode` and `tokens`, and `OPENAI_API_KEY` is absent. Consequences:

- There is no per-token dollar cost to meter. **The scarce resource is subscription
  rate-limit quota, not money.** Budgets must be expressed as call counts and
  rate-limit-window awareness, not estimated spend (corrects §6.11).
- The adapter cannot inject a key; it inherits the CLI's own auth state, so "external
  provider unavailable" means expired login or exhausted quota, both of which must
  degrade cleanly to local-only.

`codex exec` is close to an ideal planner surface:

| Flag | Why it matters |
|---|---|
| prompt via **stdin** | avoids Windows command-line length limits for large packets |
| `--output-schema <FILE>` | **Codex enforces the plan JSON Schema itself** — structured planner output solved at the source, not just validated after |
| `-o, --output-last-message <FILE>` | clean capture of the final answer without parsing JSONL |
| `--json` | JSONL event stream for live progress and telemetry |
| `-s read-only` | **enforces the consultant model at the process level** — Codex plans, it cannot edit |
| `-C <DIR>` | working root = the project |
| `--ephemeral` | no session files persisted |
| `-m <MODEL>` | model selection |

Also available and directly useful later: `codex exec review` (non-interactive code review
→ the review-packet path in §6.8) and `codex apply` (git-apply the agent's diff → the
"exception" case where a direct external patch is warranted).

Note: `codex exec --oss --local-provider ollama` means the Codex CLI can itself drive local
Ollama models. That is *not* the architecture here — it would invert control and lose the
fine-grained orchestration this project exists to provide — but it is a viable fallback if
the local executor loop underperforms.

---

## 3. Where this plan diverges from the brief

The brief asked not to take it as literally true. Five points where measurement or the
existing code contradicts it:

**3.1 "Use parallelism aggressively" is not available for inference.**
One 8 GB GPU holds one model. Logical task concurrency is fine, but every inference
request funnels through a single GPU. Two 16K workers do not fit; two 8K workers fit but
halve each one's usable context. The real overlap win is **inference running concurrently
with builds/tests/git**, which are CPU- and I/O-bound. Design for one serialized inference
lane plus a parallel deterministic-work pool — not `max_qwen_workers: 4`.

**3.2 "Optimize Qwen context quality more than token count" — correct, with a caveat.**
Measurement supports it: prefill is ~3000 tok/s, so context is cheap. But **output** is
73–80 tok/s, so verbose model output is the true cost. Control response length and
reasoning verbosity, not input size.

**3.3 Phase ordering should change.**
The brief puts planning in Phase 1 and the tool/repair loop in Phase 2. The single
riskiest unproven assumption is *"a 7B Q4 model can reliably drive a tool loop and edit
files."* §2.3 shows the default path already fails. Building a planner, DAG and provider
abstraction on top of an unvalidated executor risks discovering in Phase 2 that the
foundation does not hold. **Validate the executor loop first** (Phase 1 below), then plan
on top of it.

**3.4 DeepSeek R1 cannot be a tool-using critic.**
It has no `tools` capability — text in, text out only. It is also expensive: Qwen3-8B's
GQA geometry makes its KV cache roughly 3x the coder's per token, which is why 8192 was
chosen for it. And loading it **evicts the coder** (~3.2 s each way). So it is a batched,
occasional diagnostician invoked at escalation points, never a per-attempt critic. Prefer
a **fresh coder context** as the routine critic — same weights, no reload — and the
brief's own point that a fresh context beats asking the author to self-assess still holds.

**3.5 Tasks must be sized to a 7B Q4 model, which is smaller than the brief implies.**
Realistic competence: single-file edits, adding functions, fixing imports/syntax/type
errors, writing tests against existing code, small mechanical refactors. Weak at:
multi-file coordination, architectural judgment, subtle logic, holding many constraints at
once. The planner must emit tasks that are **single-file where possible, explicitly
specified, and mechanically verifiable**. This is a hard constraint on plan quality, and it
is the main reason external planning earns its cost on non-trivial work.

**Scope note:** the brief describes ~60 sections of system — many months of work as
specified. The phasing in §8 targets the smallest thing that genuinely runs a real task,
then grows.

---

## 4. Existing codebase inventory

~2000 lines, PySide6 + SQLite + Ollama, no heavy dependencies. It is small, clean and
well-tested (8 test modules, CI on Windows). Most of it survives.

### Keep essentially as-is

| File | Role in MoliCoder |
|---|---|
| `ollama.py` | Streaming client + metrics. Add: `tools` param, non-streaming call, tolerant parse. |
| `telemetry.py` | `nvidia-smi` GPU telemetry — feeds the resource panel directly. |
| `context.py` | Token estimation + budget maths — reuse for packet sizing. |
| `attachments.py` | UTF-8 / binary / extension safety checks — **reuse as the repo file-read guard**. |
| `formatting.py` | Markdown normalization for transcript display. |
| `ui/styles.py`, `ui/widgets.py` | Dark theme, `MetricCard`, `ChatBubble` all reusable. |

### Adapt

| File | Change |
|---|---|
| `config.py` | Keep `ModelPreset`; add roles (executor / critic / diagnostician), raise executor ctx 8192 → 16384, add `.molicoder` paths and budget config. |
| `storage.py` | Keep the `_connect` / `closing` / WAL / settings-KV pattern; add orchestration tables (§6.4). |
| `workers.py` | `ChatWorker` pattern extends to `OrchestratorWorker`. |
| `domain.py` | Add Project / Run / Plan / Task / Validation / Event dataclasses alongside Conversation / Message. |
| `ui/main_window.py` | 721 lines, the largest change. Becomes a shell hosting Chat and Build views. |

### New modules

```
src/local_model_chat/molicoder/
  registry.py      project list, add/remove/select, persistence
  projectstate.py  .molicoder/ directory read+write
  gateway.py       serialized inference lane, model residency
  tools.py         tool schema + tolerant parser + dispatch
  executor.py      the agent loop
  recon.py         deterministic + Qwen reconnaissance -> evidence packet
  planner/
    base.py        provider-agnostic planner protocol
    local.py       Qwen planner
    external.py    CLI/API adapters (Codex, Claude, ...)
    schema.py      plan validation
  validate.py      command runner, log capture, error extraction
  fingerprint.py   failure normalization + progress detection
  repair.py        repair loop + escalation policy
  scheduler.py     DAG, readiness, conflict-aware ordering
  worktree.py      git worktree lifecycle
  metrics.py       token/time/cost accounting
  events.py        structured event log
```

**Naming decisions (confirmed):**

- Keep the Python package `local_model_chat`. Renaming it touches the spec file, CI, entry
  points and every import for zero functional gain. The org name in `main.py` is already
  `molicoder-bit`.
- Rename user-facing strings (`APP_NAME`, window title, README, installer paths) to
  **MoliCoder**.
- **`local-coding` → `MoliCoder`.** This is the executor slot the user selects, and the
  name is right precisely because it is *not* purely local: behind it sits local Qwen for
  the overwhelming majority of work plus escalation to Codex when that earns its cost. Two
  layers, distinguished deliberately:
  - The **Ollama tag** `local-coding:latest` → `molicoder:latest` (a one-line `ollama cp`,
    then update `MODEL_PRESETS`). This still names raw local weights.
  - The **UI executor role** is presented as "MoliCoder" — the hybrid capability, not a
    raw model. `local-reasoning:latest` stays as-is; it is an internal diagnostician the
    user does not select directly.

---

## 5. Target architecture

```
                        PySide6 UI (shell)
        Projects sidebar | Chat view | Build/Run view | Resource panel
                                |
                        OrchestratorWorker (QThread)
                                |
      +-------------------+-----+------------------+------------------+
      |                   |                        |                  |
  Scheduler          InferenceGateway         Validator          Git/Worktree
  (DAG, ready set)   (ONE serialized lane)    (subprocess pool)   (isolation)
      |                   |                        |
      |            +------+------+                 |
      |            |             |                 |
      |     Local (Ollama)  External providers     |
      |     executor/critic  Codex/Claude/...      |
      |                                            |
      +--------------------+-----------------------+
                           |
                  SQLite (app state)  +  .molicoder/ (per project)
                           |
                    Repository = source of truth
```

Layer rules:

- **Python decides, models reason.** State transitions, retry counts, budgets, DAG
  readiness and escalation triggers are all deterministic code.
- **The repository is memory.** Git and files are authoritative. Conversation transcripts
  are disposable traces, stored separately from operational events.
- **One inference lane.** Everything hitting the GPU queues at the gateway.

---

## 6. Key subsystems

### 6.1 InferenceGateway — model residency and serialization

The single most hardware-critical component.

- Owns a mutex; all local inference serializes through it.
- Tracks which model is resident. A request for a different model pays an eviction
  (~3.2 s reload); the gateway **batches by model** to avoid thrash — if three diagnosis
  calls are queued, run them together rather than alternating.
- Enforces per-role context: executor 16384 (measured 100% GPU), diagnostician 8192.
- Records tokens and wall-clock per call into `metrics.py`.
- Exposes the same streaming interface `ChatWorker` already uses, so the existing chat UI
  keeps working unchanged.

Config surface:

```
executor_context        16384   # measured ceiling for full GPU residency
executor_max_output      2048
diagnostician_context     8192
max_inference_lanes          1  # hardware fact, not a preference
max_deterministic_workers    4  # builds/tests/git — CPU bound, safe to parallelize
```

### 6.2 Tool loop and the tolerant parser (mandatory — see §2.3)

`tools.py` must:

1. Read `message.tool_calls` when present (works with other / future models).
2. **Fall back** to extracting JSON from `content`: bare objects, ` ```json ` fences, and
   `<tool_call>` wrappers.
3. Validate against the declared schema (name exists, required args present, types).
4. On malformed output, re-prompt once with a corrective message before failing the turn.
5. Treat content that is a pure tool-call JSON object as a call, not as prose.

Keep the tool menu **small** — 7B models degrade as the menu grows:

| Tool | Notes |
|---|---|
| `list_dir(path)` | scoped to the worktree |
| `read_file(path, start, end)` | reuses `attachments.py` binary / UTF-8 guards |
| `grep(pattern, glob)` | deterministic, no model involvement |
| `write_file(path, content)` | full-content write; most reliable for a 7B |
| `replace_in_file(path, old, new)` | exact-match, verified before applying; retry on miss |
| `run_validation()` | fixed project commands only — the model does not choose commands |

**Edit strategy:** do not ask a 7B Q4 model to emit unified diffs; exact-match failure
rates are high. Prefer full-file writes for small files and verified exact-string
replacement for larger ones, with the orchestrator computing the real diff afterwards.

Every path is confined to the task's worktree; writes outside it are rejected by the
dispatcher, not by prompt instruction.

### 6.3 Project registry and `.molicoder/`

App-level SQLite holds the project list (path, name, type, added-at, last-opened).
Per project, on disk:

```
.molicoder/
  project.json     type, build/test commands, constraints
  instructions.md  user-authored project rules fed into every task
  plans/           plan-<run>.json
  tasks/           task-<id>.json
  runs/            run-<id>.json
  logs/            raw validation logs (gitignored)
  failures/        fingerprints and clusters
```

The repository is never ingested or duplicated. Project detection is deterministic:
`pubspec.yaml` → Flutter, `package.json` → Node, `pyproject.toml` → Python, etc., which
also fixes the default build/test commands.

Removing a project removes the registry row only; the folder and `.molicoder/` stay.

### 6.4 Storage schema additions

Following the existing `storage.py` conventions (WAL, `closing()`, FK on):

```sql
projects(id, name, path, kind, created_at, last_opened_at)
runs(id, project_id, request, mode, status, started_at, ended_at)
plans(id, run_id, source, provider, json, created_at)
tasks(id, run_id, key, title, objective, status, risk, attempts,
      worktree, branch, base_commit, changed_paths, created_at, updated_at)
task_deps(task_id, depends_on)
worker_runs(id, task_id, role, model, provider, prompt_tokens, output_tokens,
            seconds, status, created_at)
validations(id, task_id, command, exit_code, log_path, summary, created_at)
failures(id, task_id, fingerprint, cluster_json, created_at)
external_calls(id, run_id, task_id, provider, purpose, reason,
               input_tokens, output_tokens, est_cost, created_at)
events(id, run_id, task_id, kind, payload_json, created_at)
```

`events` is the operational timeline; model transcripts live separately so a run is
understandable without reading conversations.

### 6.5 Reconnaissance

Deterministic first, model second:

1. Deterministic: detect project type, read manifests, git branch / status / recent log,
   locate tests and config, list candidate files by path and name matching.
2. `grep` for symbols named in the request.
3. Only then, Qwen summarizes what the deterministic pass surfaced into an evidence packet
   with `path:line-line` references rather than whole files.

Output is the packet shape from the brief (objective, project_type, architecture,
relevant_files, relevant_symbols, tests, constraints, risks, open_questions,
evidence_refs). This packet feeds both local and external planning, so building it well is
what makes external calls cheap.

### 6.6 Planning and provider abstraction

`PlannerProvider` protocol: `plan(packet) -> Plan`, plus declared capabilities.
Implementations: `LocalPlanner` (Qwen), `CodexCliPlanner` (subprocess). Orchestration code
never imports a vendor.

**First external provider: Codex CLI** (confirmed target). Given §2.4, the adapter is:

```
codex exec --json                          # JSONL events for live progress
           -C <project_dir>                # working root
           -s read-only                    # Codex plans; it cannot edit
           --output-schema <plan.schema.json>   # Codex enforces the plan shape
           -o <plan_out.json>              # capture final answer cleanly
           --ephemeral                     # no session files
           --skip-git-repo-check
           < packet.md                     # prompt via stdin, no arg-length limit
```

Design notes specific to this adapter:

- **Binary resolution:** configurable path, defaulting to a discovery order of
  `%LOCALAPPDATA%\OpenAI\Codex\bin\codex.exe` → `PATH`. It is not on `PATH` here.
- **`-s read-only` is the architectural enforcement of the consultant model** from brief
  §35. Codex cannot modify the repo even if it decides to; Qwen implements. The §36
  exception (a direct external patch) becomes a deliberate, separately-coded path using a
  writable sandbox mode plus `codex apply`, not an accident.
- **`--output-schema` largely solves brief §18.** The plan schema is enforced at
  generation rather than repaired after, which cuts the malformed-plan path considerably.
  Local schema validation stays as a backstop.
- **Unavailability** = missing binary, expired OAuth login, or exhausted quota. All three
  fall back to `LocalPlanner` rather than failing the run.
- `codex exec review` covers the review-packet path in §6.8 without a second integration.

Modes: `LOCAL_ONLY`, `FRONTIER_PLAN`, `AUTO`, `MANUAL_PLAN`.

`AUTO` starts as readable heuristics, not a classifier — escalate planning when the request
touches auth / payments / crypto / migrations, spans many files, has high ambiguity, or
recon flags an unfamiliar architecture. The user can always override.

Plan output is validated against a JSON schema. **A malformed plan is repaired locally by
Qwen, never by re-calling the external provider** — that is exactly the false economy the
brief warns about, inverted.

**Local-only must be first-class**: absent credentials, network or budget, `LocalPlanner`
takes over and the app degrades in capability, never structurally.

### 6.7 Validation, error extraction, fingerprinting

```
command -> raw log saved -> deterministic parser -> error clusters -> Qwen diagnosis
```

Full logs to `.molicoder/logs/`; only extracted, deduplicated errors reach a model.
Cascade-aware: one malformed interface producing 25 downstream errors is one root cause,
not 25 tasks. Group deterministically by file and symbol first, then let Qwen judge shared
root cause.

Fingerprint = normalized (error type, file, symbol, message, failing test). Compare across
attempts:

```
A, A, A, A   -> stuck strategy: escalate
A, B, C      -> progress: keep going locally
A, B, A, B   -> oscillation: escalate
```

Progress detection — not a fixed retry count — governs escalation.

### 6.8 Repair and escalation

Ladder, with escalation gated on expected value rather than a counter:

1. Executor repairs in its current context.
2. Fresh Qwen context with a compact failure packet.
3. Fresh Qwen diagnosis (separate from repair).
4. Qwen implements the diagnosis.
5. Optional: DeepSeek R1 diagnosis — batched, accepting the ~3.2 s eviction cost.
6. External rescue.

Escalate when: identical fingerprint 3x, oscillation detected, task wall-clock over budget
with no fingerprint movement, high-risk area, or the user asks. Do **not** escalate merely
because a compile failed, a test failed, an import was wrong, or a counter hit N while
fingerprints still show movement.

External rescue receives a compact packet (task, plan fragment, current diff, failing
validation, attempted strategies, local diagnosis, one precise question) and returns
**strategy**. Qwen implements it. The exception in brief §36 is supported — a direct
external patch for a small subtle fix — but stays exceptional and is recorded as such in
`external_calls.reason`.

### 6.9 Concurrency and worktrees

Given §6.1, concurrency is **one inference lane + a deterministic worker pool**. The
scheduler keeps a DAG and a ready set; while task T1's build runs on CPU, T2's inference
can use the GPU. That overlap is the actual throughput win.

Worktrees (`worktree.py`) give each task an isolated branch off a known base commit, so
autonomous work never touches the user's checked-out main branch. Conflict-aware ordering
serializes tasks with expected file overlap (shared manifests, routing files, generated
code), using observed changed paths to improve future predictions.

Worktrees arrive in Phase 3; Phases 1–2 run sequentially on a dedicated branch, which is
sufficient and much simpler to debug.

### 6.10 UI

The current single-view window becomes a shell:

- **Projects sidebar** — add folder / add repo / switch / remove, with name, path, type,
  branch, dirty state and run status per project.
- **Chat view** — the existing experience, preserved.
- **Build view** — request box, mode selector (AUTO / LOCAL_ONLY / FRONTIER_PLAN / MANUAL),
  planner and executor pickers, task list with live status, diff viewer, validation
  results, event timeline.
- **Resource panel** — extends the existing inspector: GPU residency (already built),
  tokens, local vs external call counts, and remaining external call budget (not dollar
  cost — see §6.11).

Controls: pause, resume, cancel, retry locally, request fresh diagnosis, escalate now,
inspect diff / logs, merge. Harmless local operations do not prompt; destructive and
external actions do.

### 6.11 Metrics

Track per run: external calls (count, purpose, reason, wall-clock), local runs (count,
tokens, inference seconds, attempts), deterministic work (builds, tests, durations). Record
**why** each external call happened (`planning_complexity`, `local_repair_exhausted`,
`architecture_conflict`, `security_review`, `human_requested`) so the system can later be
analysed for which escalations actually paid off — the long-term point of the project.

**Cost model correction (per §2.4).** Codex here authenticates via ChatGPT-subscription
OAuth, not an API key, so there is **no per-token dollar cost to compute**. Estimated spend
would be a fabricated number. The scarce resource is **subscription quota and rate-limit
windows**. Therefore:

- Meter **external call counts**, not estimated dollars.
- Treat a rate-limit refusal as a first-class, expected outcome that triggers local
  fallback — not an error state.
- Keep the `est_cost` column in the schema but leave it null for OAuth providers; it
  becomes meaningful only if an API-key provider is added later.

Budgets are ceilings, not targets: `max_external_calls_per_run`, `max_planning_calls`,
`max_rescue_calls`, `max_qwen_attempts`, `max_qwen_runtime_per_task`.

### 6.12 Project-type adapters

Three targets, in the order they will actually be used. Each supplies detection, build/test
commands, and an error parser.

| Target | Detect | Validation | Speed |
|---|---|---|---|
| **Python** (this repo, Phase 1) | `pyproject.toml` | `pytest`, offscreen Qt | seconds — ideal for developing the loop |
| **Web / Node** | `package.json` | `tsc --noEmit`, eslint, `vitest`/`jest`, build | seconds to a minute |
| **Flutter** | `pubspec.yaml` | `flutter analyze`, `flutter test`, `flutter build` | **minutes** |

The speed spread is the reason this matters architecturally. A `pytest` cycle is fast
enough that a wasteful repair loop costs little; a `flutter build` cycle is slow enough
that the same loop is expensive in wall-clock time. So:

- **Escalation thresholds must be per-target, not global.** On Flutter, the expected cost
  of another local attempt is much higher, which legitimately shifts the escalation
  calculus toward Codex earlier than on Python.
- Prefer the **cheapest validation that can falsify the change**: `flutter analyze` before
  `flutter test` before `flutter build`, failing fast at each tier rather than running the
  full ladder every attempt.
- The §6.9 inference/deterministic overlap matters most here — a multi-minute Flutter
  build is exactly the window where another task's inference should be using the GPU.

Developing against this repo first (§8, Phase 1) is deliberate: fast validation makes the
executor loop cheap to iterate on. Flutter and web adapters land once the loop is proven.

---

## 7. Safety model

- All autonomous file writes confined to the task worktree, enforced in the dispatcher.
- Command allowlist per project type; the model calls `run_validation()`, it does not
  choose arbitrary shell commands.
- Never autonomous: `git push`, publish, deploy, production database access, destructive
  migrations, credential access.
- Secrets are never placed in external packets; `.env` and credential-like files are
  excluded from evidence gathering by extension and name matching.
- Nothing runs on the user's checked-out branch without explicit confirmation.

---

## 8. Phased delivery

Reordered from the brief per §3.3 — the executor loop is the riskiest assumption and is
validated first.

### Phase 0 — Foundation (no orchestration yet)

- Rename user-facing strings to MoliCoder; keep the package name. `ollama cp
  local-coding:latest molicoder:latest` and update `MODEL_PRESETS`.
- Project registry + `.molicoder/` read/write + project type detection (Python / Node /
  Flutter per §6.12).
- Storage schema additions.
- UI shell: projects sidebar + view switching. **The chat view is kept and keeps working
  unchanged** — it costs little and preserves a working app while the orchestrator is built
  behind it.

**Exit:** add several folders, switch between them, restart, list persists, chat still works.

### Phase 1 — Prove the local executor (highest risk)

- `InferenceGateway` with the serialized lane and 16K executor context.
- `tools.py`: the six tools, tolerant parser, schema validation, path confinement.
- `executor.py`: the agent loop with turn and time budgets.
- `validate.py`: run project commands, capture logs, extract errors.
- One task, one dedicated branch, no DAG, no worktrees, no planner — a hand-written task
  runs end to end.

**Exit:** on this repo, a request like *"add a `format_duration` helper to `telemetry.py`
with tests"* completes autonomously: files edited, `pytest` green, diff reviewable.
If the 7B cannot do this reliably, stop and reconsider before building anything on top.

### Phase 2 — Planning and repair

- Recon pipeline and evidence packet.
- `LocalPlanner` + plan schema validation + local schema repair.
- Sequential multi-task execution.
- Repair ladder, failure fingerprinting, fresh-context critic.

**Exit:** a multi-task feature planned locally, executed, self-repaired through at least
one genuine failure.

### Phase 3 — Orchestration

- Git worktrees, DAG scheduler, conflict-aware ordering.
- Integration merge + integration validation + merge-conflict repair.
- Overlap inference with deterministic work.

**Exit:** independent tasks run in isolated worktrees and merge cleanly.

### Phase 4 — External intelligence (Codex CLI)

- **Step 1: a live `codex exec` smoke test.** Deliberately not run during planning, to
  avoid spending subscription quota unasked. This must be the first action of the phase,
  since binary path, OAuth validity and `--output-schema` behaviour are all assumptions
  until proven.
- `CodexCliPlanner` per §6.6: binary discovery, stdin packets, `--output-schema`,
  `-o` capture, `--json` event stream, `-s read-only`.
- Planning / rescue / review packets; `codex exec review` for the review path.
- Evidence-request rounds (capped).
- AUTO heuristics, per-target escalation thresholds (§6.12), call-count budgets,
  escalation reasons, rate-limit-aware fallback.

**Exit:** the same feature runs identically with external planning on or off; a missing
binary, expired login or exhausted quota degrades to local-only rather than failing.

### Phase 5 — Resource optimization

- Adaptive concurrency from observed throughput.
- Reusable skills.
- Historical analysis of which external calls actually helped.

---

## 9. Testing

Extends the existing pytest + pytest-qt + offscreen-Qt setup. All LLM providers mocked —
**unit tests never require real inference.**

Priority targets:

- Registry: add / remove / persist / switch / non-git folder / missing path.
- **Tolerant tool parser** — structured `tool_calls`, bare JSON in content (the real
  observed case), fenced JSON, `<tool_call>` wrappers, malformed, and non-call prose.
- Path confinement: writes outside the worktree rejected.
- Plan schema: valid, malformed, local repair path.
- DAG order, conflict detection, worktree lifecycle.
- Validation parsing and error clustering with real captured logs.
- Fingerprinting: stuck vs progress vs oscillation sequences.
- Escalation triggers and budget exhaustion.
- Provider unavailable → local fallback.
- State recovery after restart mid-run.
- Process timeout handling.

A small set of **opt-in integration tests** (marker-gated, skipped in CI) should exercise
the real model, since the tool-calling behaviour in §2.3 is exactly the class of bug mocks
cannot catch.

---

## 10. Risks

| Risk | Assessment |
|---|---|
| **7B Q4 cannot sustain an agent loop** | The core risk. Tool-call formatting already fails by default (§2.3). Phase 1 exists to answer this before anything is built on top. |
| Single GPU limits real concurrency | Confirmed by measurement. Mitigated by overlapping inference with CPU-bound work rather than pretending inference parallelizes. |
| 16K context too small for multi-file tasks | Mitigated by task sizing and targeted evidence rather than whole-file dumps. Larger contexts spill to CPU. |
| Exact-match edits fail on a 7B | Mitigated by preferring full-file writes and verifying replacements before applying. |
| Codex CLI is a moving target | Installed version is `0.130.0-alpha.5` — an alpha. Flag names and JSONL event shapes may change. Keep the adapter thin, pin nothing to undocumented event internals, and fail soft to local-only. |
| Slow Flutter validation makes repair loops expensive | Per-target escalation thresholds and cheapest-falsifying-validation-first (§6.12). |
| Scope | The brief is many months as written. Phasing targets a working slice early. |
| MoliCoder outgrows its own value | Brief §58. Keep dependencies at zero-new for Phase 0–2; no vector DB, no agent framework, no queue. |

## 11. Decisions taken

| Question | Decision |
|---|---|
| First real target | **This repository.** Fast `pytest` validation makes the executor loop cheap to iterate on. Web and Flutter are the real long-run workloads and get adapters in §6.12 order. |
| External provider | **Codex CLI**, via `codex exec` (§2.4, §6.6). Not an API — the CLI's non-interactive surface is a better fit than raw API calls, and auth is already established. |
| Executor naming | **`local-coding` → `MoliCoder`** (§4). Ollama tag becomes `molicoder:latest`; the UI role is the hybrid local-Qwen-plus-Codex capability, which is why "local" was the wrong word. |
| Chat view | **Kept.** Internal architecture need not be mirrored in the UI. |

**Still open — one question that materially affects quality:**

Is the executor model fixed at Qwen 2.5 Coder 7B Q4, or is swapping it in scope? The 8 GB
ceiling makes this the single highest-leverage quality lever available: a 14B at Q3 will not
fit at 16K, but a different 7B/8B coder tuned for tool use could raise the §2.3 reliability
floor substantially. This does not block Phase 0–1 — the tool-loop architecture is
model-agnostic — but Phase 1's exit criteria will show directly whether the current model is
good enough, and that is the natural moment to decide.

---

## 12. Immediate next step

Phase 1's tolerant tool parser and executor loop are the whole bet. Everything else is
conventional engineering; that part is not. Build it first against this repository, where
validation is a fast `pytest` run, and let its exit criteria answer the model question above.
