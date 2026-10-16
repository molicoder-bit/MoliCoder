# Build verification report

Date: 2026-10-16. Scope: this checkout, deterministic scripted providers, temporary Git projects and SQLite databases, Qt test fixtures, and a fake Kandev API. The curriculum did not invoke paid providers, a live Kandev server, or an autonomous coding model.

## Measured result

The complete 120-scenario catalog reported **120 PASS, 0 FAIL, 0 BLOCKED, 0 NOT_RUN**. The machine-readable [scenario report](reports/build-scenarios-2026-10-16.json) records every ID, test node, outcome, elapsed time, and local pytest/JUnit evidence path. `artifacts/build-scenarios/latest.json` is the working copy. An ID is counted as PASS only when its mapped pytest assertion ran successfully.

| Level | PASS / total |
| ---: | ---: |
| 0 | 10 / 10 |
| 1 | 10 / 10 |
| 2 | 10 / 10 |
| 3 | 10 / 10 |
| 4 | 10 / 10 |
| 5 | 10 / 10 |
| 6 | 10 / 10 |
| 7 | 10 / 10 |
| 8 | 10 / 10 |
| 9 | 10 / 10 |
| 10 | 10 / 10 |
| 11 | 10 / 10 |

## What the passing cases establish

The active desktop Build path saves a reviewable plan before execution and requires an exact revision, digest, project, Git base, and runner-policy approval. Stale approval and changed project HEAD stop work. Scoped edits run in a managed worktree, receive independent allowlisted checks, and remain isolated until reviewed Apply. A repair that needs a new file proposes a fresh scope revision and preserves earlier accepted checkpoints. A dead-owner run can be reviewed against its Git state, stash unverified edits, and resume unfinished tasks after new approval without replaying completed work. Unverified commits block replay.

Level 2 now checks a config/import edge, a three-step verified chain, explicit user ordering, and a review warning for repetitive task fragmentation. The plan editor can bind a task to an already configured, ready alternate model; a changed assignment needs new approval and is observed in execution. The separate `WorktreeOrchestrator` backend has approval-gated tests for parallel independent work, dependency diamonds, failed prerequisites, same-file integration conflicts, prompt activation of ready dependents, and a 20-task graph. The desktop's active internal path remains sequential.

The pinned Kandev REST adapter has tests for a single remote task, readiness, external identity, duplicate-event suppression, stale-status handling, and reconciliation. A separate `KandevBoardOrchestrator` projects up to 30 approved tasks using backend `blocked_by`, `start_when_unblocked`, and agent-profile fields. It verifies remote task identity, dependencies, profiles, and observed models; it recovers an ambiguous create by external ID without relaunch. Its acceptance stage checks clean registered remote Git worktrees, approved file scope, independent task validation, bounded local repair, completion records, merged integration, and final project validation before `ready_to_apply`. S119 executes a 24-task fake Kandev DAG with two profiles and a repaired defect. S120 combines a multi-step feature, independent branches, two profiles, a lost create reply, backend disconnect, fresh store/orchestrator instance, transient repair-provider outage, code defect, resolved merge conflict, and final project verification. The selected source checkout remains untouched pending Apply.

Security cases exercise path traversal, symlink escape, credential-file reads and search, untrusted repository instructions, unsupported external actions, and recognized credential redaction in provider, executor, validator, and persisted runner output. UI cases cover pause/resume, Chat navigation during Build, and rapid task-event display. Other Level 11 fixtures use independent Python and Node tests for application changes, feedback-guided repair, and approval conflict during execution.

## Practical limits

**100% scenario PASS is deterministic curriculum coverage, not proof of 100% product reliability.** The large Kandev cases use a fake API and scripted providers; they do not establish live Kandev scheduling or autonomous model design quality. The multi-task board bridge is separate from the desktop's active single-task Kandev route. Its repair callback runs in a MoliCode-managed worktree rather than a Kandev agent session. The active internal desktop path remains sequential. General post-execution graph replanning, crash-safe replay during parallel integration, arbitrary per-task runner/model provisioning, and exact paid CLI spend accounting remain open. A live end-to-end run and the opt-in real Codex, Claude, and Ollama tests are still needed for those external behaviors.

The complete regression suite reported **655 passed, 6 skipped in 510.50 seconds**. After its collection had started, a focused S037 run passed with the final incompatible-model assertion. The six opt-in skips require real Codex, Claude, or Ollama runs.
