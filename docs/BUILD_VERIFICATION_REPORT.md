# Build verification report

Date: 2026-10-16. Scope: this checkout, deterministic scripted providers, temporary Git projects and SQLite databases, Qt test fixtures, and a fake Kandev API. The curriculum did not invoke paid providers, a live Kandev server, or an autonomous coding model.

## Measured result

The complete 120-scenario catalog reported **109 PASS, 0 FAIL, 0 BLOCKED, 11 NOT_RUN** (90.8% PASS). The machine-readable [scenario report](reports/build-scenarios-2026-10-16.json) records every ID, its test node, outcome, elapsed time, and local pytest/JUnit evidence path. `artifacts/build-scenarios/latest.json` is the working copy of the report. An ID is counted as PASS only when its mapped pytest assertion ran successfully.

| Level | PASS / total | Remaining IDs |
| ---: | ---: | --- |
| 0 | 10 / 10 | — |
| 1 | 10 / 10 | — |
| 2 | 6 / 10 | S022, S024, S027, S030 |
| 3 | 9 / 10 | S037 |
| 4 | 10 / 10 | — |
| 5 | 10 / 10 | — |
| 6 | 10 / 10 | — |
| 7 | 6 / 10 | S076–S079 |
| 8 | 10 / 10 | — |
| 9 | 10 / 10 | — |
| 10 | 10 / 10 | — |
| 11 | 8 / 10 | S119–S120 |

## What the passing cases establish

The active desktop Build path saves a reviewable plan before execution and requires an exact revision, digest, project, Git base, and runner-policy approval. Stale approval and changed project HEAD stop work. Scoped edits run in a managed worktree, receive independent allowlisted checks, and remain isolated until reviewed Apply. A repair that needs a new file proposes a fresh scope revision and preserves earlier accepted task checkpoints. A dead-owner run can be reviewed against its Git state, stash unverified edits, and resume unfinished tasks after new approval without replaying completed work or resetting crash-recovery attempt counts. Unverified commits block replay.

The separate `WorktreeOrchestrator` backend has approval-gated deterministic tests for parallel independent work, dependency diamonds, failed prerequisites, same-file integration conflicts, prompt activation of ready dependents, and a 20-task graph. The desktop's active internal path remains sequential. Fake Kandev tests cover a single remote task, readiness checks, external identity preservation, duplicate-event suppression, stale-status handling, and explicit reconciliation after failure or connection loss.

Security cases exercise path traversal, symlink escape, credential-file reads and search, untrusted repository instructions, unsupported external actions, and recognized credential redaction in provider, executor, and persisted runner output. A separate validator regression confirms recognized bearer tokens are redacted before command output is saved to `.molicoder/logs`. UI cases cover pause/resume through worker state, Chat navigation during Build, and rapid task-event display. Level 11 uses independent Python and Node tests for application changes, a feedback-guided repair, and an approval conflict during execution. These scripted fixtures verify workflow and acceptance behavior; they do not measure an autonomous model's ability to design or implement arbitrary features.

## Remaining limits

The 11 `NOT_RUN` IDs have no registered executable scenario case. S022/S024/S027/S030 need specific decomposition and ordering assertions; S037 needs editable per-task model override; S076–S079 need a multi-task Kandev board with dependency and assignment synchronization; S119 needs a large mixed-complexity Kandev project; S120 needs a full-system adversarial reliability exercise. General post-execution graph replanning, crash-safe replay in the parallel backend, and exact paid CLI spend accounting also remain open. The Kandev and model-dependent claims above are limited to mocks or scripted providers unless explicitly stated otherwise.

The broader regression suite reported **638 passed, 6 skipped in 430.39 seconds**. After the final validation-log redaction change, the focused security and validator suites reported **16 passed**; the new log-redaction test was added after the broader run had started. The six opt-in skips require real Codex, Claude, or Ollama runs.
