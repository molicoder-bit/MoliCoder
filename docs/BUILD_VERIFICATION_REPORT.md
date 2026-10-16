# Build verification report

Date: 2026-10-16. Scope: this checkout, deterministic scripted providers, temporary Git projects/SQLite databases, fake Kandev API. No paid providers, real Kandev server, or real model were run.

## Results

The full-catalog machine report is `docs/reports/build-scenarios-2026-10-16.json`; the runner also writes `artifacts/build-scenarios/latest.json` locally with per-case pytest/JUnit evidence paths. It lists each ID, level, outcome, test node, and elapsed time. Results: **61 PASS, 0 FAIL, 0 BLOCKED, 59 NOT_RUN**. This is a coverage count, not a 100% reliability claim.

| Level | Passing IDs | PASS / total | Main uncovered behavior |
| ---: | --- | ---: | --- |
| 0 | S001–S010 | 10 / 10 | Scripted providers and local Apply only; real planner/model quality unmeasured |
| 1 | S011–S020 | 10 / 10 | Deterministic UI fixtures; unsupported operation is production deployment |
| 2 | S028–S029 | 2 / 10 | Planner-quality and active parallel-scheduling cases not registered |
| 3 | S031–S036, S038–S039 | 8 / 10 | Compatible model override and replanning after partial completion |
| 4 | S041, S043–S045, S049–S050 | 6 / 10 | Complex capability selection, budget gating, model overrides |
| 5 | S051–S054, S056–S057, S059–S060 | 8 / 10 | Type checking and cancellation during long-running validation |
| 6 | S061–S070 | 10 / 10 | Scripted providers; broader replanning remains untested |
| 7 | S071–S075 | 5 / 10 | Multi-task Kandev and live integration |
| 8 | S082 | 1 / 10 | Parallelism, complex graphs, and conflict behavior |
| 9 | none | 0 / 10 | Durable recovery curriculum |
| 10 | S107 | 1 / 10 | Remaining security and UI consistency cases |
| 11 | none | 0 / 10 | Complex end-to-end workflows |

Existing broader pytest coverage includes planners, scheduler, worktrees, validation, repair, Kandev adapter, storage, UI, and recovery. The complete suite reported **599 passed, 6 skipped in 380.28 s** after the S069 scope-revision implementation. Subsequent focused S069/S057 and S059 runs reported **2 passed** and **1 passed**, respectively. The full scenario catalog reported **61 PASS, 59 NOT_RUN**. The six skips require opt-in real Codex, Claude, or Ollama runs. Individual scenario evidence is in the local JSON/JUnit artifacts. Earlier in this work, one combined targeted run ended in a Windows/Qt access violation in a model-install test; that test passed alone and the whole UI module passed on rerun. Its root cause remains unconfirmed.

## Verified invariants in the registered scenarios

The active internal and Kandev runtime paths wait for an exact plan approval before mutating execution. A plan revision change denies stale approval at task boundaries and before Apply, including if a new revision is approved during the last task. Explicit rejection records a denied decision and leaves the plan available. Internal plan editing validates a new graph, regenerates assignments, and requires new approval; an added task then executes and applies, and a changed dependency alters execution order. Unsupported runner overrides are rejected with an explanation. A changed project HEAD stops internal execution before the scripted provider runs. Internal simple file scenarios create verifiable artifacts in an isolated worktree and leave the source checkout clean pending Apply. Exact artifact checks let an already satisfied file pass without a new commit and carry an empty directory into the selected checkout during confirmed Apply. Level 1 UI cases block missing, absent, non-Git, and simulated read-only projects, keep a pending plan bound to its original project, and answer Chat status from persisted records. Invalid cyclic/missing task graphs stop before scheduling. A bounded edit uses the local route when eligible; injected provider outage/auth failures stop without project changes. A recoverable quota fault uses the predeclared fallback within two attempts, while opaque CLI cost stays unknown. Level 5 cases run independent arithmetic tests, reject a syntactically invalid edit, reject a false success without a diff, catch an out-of-scope file, preserve isolated partial output on provider cancellation, and stop Apply when a final project test finds a regression. Level 6 cases pass failed test evidence into bounded repairs, reject a new regression, preserve an independent test oracle, and stop after repeated identical failures or the attempt ceiling. A later repair that changes an extra file now proposes a scope-only plan revision, preserves a completed task checkpoint, waits for new approval, reruns the unfinished task, and applies after validation; a first-attempt extra file remains a failure. A simulated outage is classified and handed to an available route. Disabled Kandev permits internal Build; enabled Kandev now checks live health/version and a read-only task lookup before reporting adapter readiness. Kandev fake adapter launches once only after explicit harness approval and preserves its external task identity across reconciliation. A two-task dependency chain starts its child only after its parent has a passing persisted completion record.

## Limits and next work

The active internal path is sequential. It records proposed per-task assignments from one selected runner policy, but cannot override individual runner/model choices or negotiate arbitrary capabilities. The Kandev path still represents one task and has no multi-task dependency mapping. Pending internal plans can be edited; after execution starts, only a failed repair's file scope can expand while the task graph and completed task specifications stay fixed. The full run/task state machine, atomic graph scheduling in the active path, comprehensive fault injection, and crash-safe multi-task replay are incomplete. The 59 `NOT_RUN` scenarios must be implemented progressively; their absence cannot be inferred as passing from the broader unit suite. Exact paid CLI token charges and real-provider planning quality remain unmeasured.
