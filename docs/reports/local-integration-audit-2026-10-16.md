# Local integration audit — October 16, 2026

The application is **not validated**. This audit used real desktop interaction initially, then CLI/headless execution after the user authorized that change. A headless pass does not close a desktop UI acceptance gap. No paid provider calls were intentionally executed in this audit.

## Results

Primary inventory: **26 PASS, 0 FAIL, 18 PARTIAL, 6 BLOCKED**, out of 50 scenarios. Supplemental real executor CLI suite: **7 PASS, 1 FAIL**. The standalone CLI complex-task failure is still open; the successful multi-file tests used the shared desktop planning runtime instead. Shared Chat/controller with real local inference: **9 PASS**. These counts overlap; they must not be summed.

## Environment and isolation

- Baseline: source revision `4d2ed8c`, version 1.3.5, Windows; tested working changes described below.
- Ollama: loopback port 11434, actual `molicoder:latest` (Qwen family). Inference effort is not selected autonomously.
- Desktop test DB: `artifacts/ui-audit-20261016/profile/LocalModelChat/chats.sqlite3`. User chats/configuration were kept separate.
- Every CLI case used its own clean `molicoder/audit` Git branch. Build runtime cases used their own repos, SQLite databases and worktrees.
- Installed desktop executable was inspected initially. Current source repairs have not been packaged or installed by this audit.

## Execution evidence and reproduction

All paths below are relative to the repository. Evidence remains local under ignored `artifacts/` directories. The machine-readable primary inventory includes fields for each scenario and points to its evidence.

| Evidence | What it establishes |
|---|---|
| `artifacts/ui-audit-20261016/scenarios.json` | All 50 primary scenarios, final statuses, method and limitations |
| `artifacts/ui-audit-20261016/initial-chat-messages.json` | Initial real UI failures, saved messages and measured tokens |
| `artifacts/ui-audit-20261016/launch.json`, numbered snapshots | Timestamped real Windows accessibility observations; screenshots were displayed in the chat |
| `artifacts/cli-audit-20261016-run1/` through `run3/` | Initial failures and successive repairs; each case includes executor trace, command, disk checks and independent verification |
| `artifacts/chat-cli-audit-20261016/` | Project reads, file explanation, follow-up, nonexistent file, 1005-file listing, project switching, path containment and post-apply read |
| `artifacts/build-audit-20261016-run2/` | Local plan → reviewed revision → local execution → independently verified worktree → reviewed CLI apply → verified source |
| `artifacts/build-multistep-20261016*/` | Initial failures, checkpoint-policy repair, scoped writes and fresh-context recovery; run4 passed and was applied |
| `artifacts/build-three-step-20261016*/` | Malformed-call failure, narrow parser repair, successful three-task rerun and reviewed apply |
| `artifacts/ui-audit-20261016/kandev-readiness.json` | Actual HTTP 200 health, `v0.97.0`; no task lifecycle claim |

Rerun the executor suite in a fresh directory:

```powershell
.\.venv\Scripts\python.exe scripts\audit_local_workflows.py --output artifacts\fresh-cli-audit
```

The exact headless Chat and Build preparation/review/execution scripts are preserved in `artifacts/ui-audit-20261016/`: `audit_chat_cli.py`, `plan_build_retry.py`, `execute_build.py`, `apply_build.py`, `plan_multistep_final.py`, `execute_multistep_final.py`, `plan_multistep_recovery.py`, `execute_multistep_recovery.py`, `apply_multistep.py`, `plan_three_steps_retry.py`, `execute_three_steps_retry.py`, and `apply_three_steps.py`. Use fresh output folders when reproducing so original failures remain available. Build approval scopes bind project, run, revision, digest, base and route. The audit inspected each generated plan before recording approval.

## Defects and repairs

| Severity | Reproduction / root cause | Change and actual result |
|---|---|
| High | Desktop structure/file questions fell through to a model without file evidence. | Added bounded project reads and fresh file context; shared headless workflow passes, desktop rerun pending. |
| High | Empty filename-only request used an agent and software validation. | Exact empty `.txt` creation is exclusive, verified directly, and uses no model in CLI/Internal Build. CLI passed; desktop confirmation requires rerun. |
| Critical | Validation-only request caused the local executor to rewrite the intentionally failing test and claim success. | Read-only tools enforced; failed validation stops without repair. Real CLI rerun preserved test bytes and returned failure. |
| High | A successful validation could remain valid after a later write; some models also repeatedly omitted the validation tool. | Writes invalidate the gate; host invokes fixed validation on final responses. Regression checks and simple live reruns passed. |
| High | Local planner copied `src/` from its template for a root-level file. | Template now uses a real evidence path and canonical validation criterion. Real replanning chose `calculator.py`; local execution and apply passed. |
| High | First dependency task was evaluated against tests requiring unfinished later tasks. | Runtime uses existing intermediate-check policy for nonfinal tasks; final task runs full suite. Each local task can write only its approved paths. |
| High | Two-file local feature stopped with a missing import and the local repair loop was configured for only one attempt. | A bounded fresh-context second local attempt recovered. Independent function calls and pytest passed, followed by reviewed CLI apply. Initial failures remain in the earlier reports. |
| Medium | Three-task feature produced invalid JSON escapes around apostrophes in Python code. | Repair only that invalid escape at the JSON decoder error offset, with an eight-repair bound; all other malformed calls remain rejected. The three-task rerun and apply passed. |
| Open | Flat executor CLI failed the complex new-file scenario. | That standalone case remains FAIL; success of the separate planning runtime does not close it. |
| High | Offline resume could enter the external-provider implementation path. | Explicitly blocked that route. Offline recovery remains unavailable pending a dedicated local implementation; no paid call was used to reproduce it. |
| Medium | Headless runtime assumed project state folders had already been initialized by UI. | Initialize them in the runtime entry point. Fresh headless planning now reaches awaiting_approval. |

## Routing and integration assessment

- Baseline: perform exact filesystem actions deterministically; use a capable healthy local model for inference; justify any external escalation. Unknown provider cost/effort stays unknown.
- Exact empty-file action meets that baseline. Simple local code tasks succeeded with real tool calls and independent checks.
- Hybrid still always uses the selected external planner. Local eligibility remains restricted to one or two low-risk existing-file edits. General new-code task routing is incomplete.
- CLI model selection is a fixed argument/default. Desktop local planning/execution uses the configured preset. Complexity-based autonomous model and effort selection has not been established.
- Actual two-task plans and dependency ordering were observed. The default desktop orchestrator is sequential; safe concurrent execution is not exposed there.
- Kandev health matches the pinned version. No isolated Kandev ownership profile was configured for this audit. Task creation, launch, assignment, dependencies, cancellation and completion remain unverified. Environment-variable absence alone is not evidence that the user has no saved project configuration.
- Standalone executor CLI, shared headless desktop runtime, and actual desktop UI are explicitly distinguished in the inventory.

## Regression and remaining work

Focused automated groups passed (76 tests before the final runtime changes; 47 relevant tests after them; 21 after adding persisted local execution traces; 19 tool/local-workflow checks after the parser repair). Counts overlap. These support repairs and do not establish UI or complete product acceptance. The full standalone executor CLI rerun passed 7/8 cases; its complex new-file case remains failed. After the later repairs, a focused four-case CLI regression passed. The actual headless desktop Build runtime separately completed and applied two- and three-task features. Read-only failure and unavailable-model cases passed their negative expectations. Local Build completion events now retain requested model, unknown effort, usage, status, error and bounded tool traces, with the existing event redaction applied.

Next acceptance gates: close the flat executor CLI complex-task gap; implement and evaluate automatic model/effort routing; provide local-only recovery; validate an isolated Kandev task lifecycle; rerun the repaired desktop workflows and approval/apply controls. Desktop UI gaps and scenarios that depend on unavailable routing/concurrency capabilities remain partial or blocked rather than receiving inferred passes.

The final successful two-file evidence is in `artifacts/build-multistep-20261016-run4/`; the successful three-file evidence is in `artifacts/build-three-step-20261016-run2/`. Both include approval-scoped execution, actual local tool traces, independent tests and reviewed apply results. The local models used no paid provider calls.

## Primary scenario inventory

| ID | Level | Objective | Status |
|---|---:|---|---|
| S01 | 1 | Launch MoliCode and verify the application loads correctly. | PASS |
| S02 | 1 | Switch between Chat and Build modes. | PASS |
| S03 | 1 | Ask a simple general question in Chat mode. | PASS |
| S04 | 1 | Ask what project is currently open. | PASS |
| S05 | 1 | Ask the assistant to identify the project structure. | PARTIAL |
| S06 | 1 | Ask which directories and files exist in the active workspace. | PARTIAL |
| S07 | 1 | Ask it to locate a specific file and explain its contents. | PARTIAL |
| S08 | 1 | Ask follow-up questions that depend on previous conversation context. | PARTIAL |
| S09 | 1 | Ask about a nonexistent file or directory and verify that the assistant does not invent information. | PASS |
| S10 | 2 | Create a simple file in the active project. | PASS |
| S11 | 2 | Modify an existing file according to a precise instruction. | PASS |
| S12 | 2 | Create a simple function and verify its actual behavior. | PASS |
| S13 | 2 | Ask for a small bug fix and verify the fix. | PASS |
| S14 | 2 | Test whether the application correctly distinguishes conversational questions from executable build requests. | PARTIAL |
| S15 | 2 | Create empty file without external AI | PASS |
| S16 | 3 | Give the orchestrator a trivial task requiring minimal reasoning. | PASS |
| S17 | 3 | Give it a moderately complex programming task. | BLOCKED |
| S18 | 3 | Give it a task requiring deeper architectural reasoning. | BLOCKED |
| S19 | 3 | Give it a task requiring inspection of multiple project files. | BLOCKED |
| S20 | 3 | Give it a task involving multiple distinct capabilities. | BLOCKED |
| S21 | 4 | Give Build mode a task requiring multiple steps. | PASS |
| S22 | 4 | Verify that the task is analyzed and decomposed into appropriately atomic subtasks. | PASS |
| S23 | 4 | Verify that the generated plan is presented for user evaluation. | PARTIAL |
| S24 | 4 | Approve the plan through the application. | PARTIAL |
| S25 | 4 | Verify automatic model/runner assignment for individual subtasks. | PARTIAL |
| S26 | 4 | Verify task dependency creation and ordering. | PASS |
| S27 | 4 | Verify that independent tasks can execute concurrently when safe. | BLOCKED |
| S28 | 4 | Verify that dependent tasks cannot start before their prerequisites are complete. | PASS |
| S29 | 5 | Trigger a controlled task execution failure. | PASS |
| S30 | 5 | Verify whether the system detects the actual failure. | PASS |
| S31 | 5 | Verify that it reports an accurate and understandable error. | PASS |
| S32 | 5 | Verify that recovery or retry mechanisms behave correctly. | PASS |
| S33 | 5 | Test unavailable or unauthorized providers. | PARTIAL |
| S34 | 5 | Test model/runner selection when the preferred option cannot execute. | BLOCKED |
| S35 | 5 | Test interruptions, state persistence, and resuming work. | PARTIAL |
| S36 | 5 | Verify that dependent tasks do not proceed after an unsuccessful prerequisite. | PASS |
| S37 | 6 | Request a small but complete feature involving multiple files. | PASS |
| S38 | 6 | Verify correct planning, decomposition, dependencies, and autonomous model assignments. | PARTIAL |
| S39 | 6 | Execute the approved plan through the actual application. | PARTIAL |
| S40 | 6 | Verify every generated artifact and its integration with the existing project. | PASS |
| S41 | 6 | Run the resulting feature and test the actual user-visible behavior. | PASS |
| S42 | 6 | Switch to Chat mode and ask questions about the changes just made. | PARTIAL |
| S43 | 6 | Verify the assistant can accurately inspect and explain the resulting project state. | PASS |
| S44 | 6 | Repeat with a more complex feature requiring multiple dependent and independent operations. | PARTIAL |
| S45 | 7 | Repeat previous simple scenarios after complex workflows. | PASS |
| S46 | 7 | Test context switching between projects. | PARTIAL |
| S47 | 7 | Test confusing, incomplete, or ambiguous user instructions. | PARTIAL |
| S48 | 7 | Verify that destructive or unauthorized changes are not performed without appropriate authorization. | PASS |
| S49 | 7 | Test behavior with larger directory structures and multiple related files. | PARTIAL |
| S50 | 7 | Verify that the application accurately communicates execution progress, blockers, failures, and completed work. | PARTIAL |
