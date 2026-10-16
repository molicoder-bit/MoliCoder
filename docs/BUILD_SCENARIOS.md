# Progressive Build scenario specification

This is the 120-scenario curriculum supplied for MoliCoder Build. Levels must be attempted in order; a higher level is stable only after its deterministic cases and all lower-level regressions pass. Each scenario requires an isolated temporary Git repository and SQLite database. Fake providers and fake Kandev may replace external boundaries, while planning, approval, orchestration, validation, and persistence must use production code. Real providers and real Kandev are opt-in only.

## Scenario record and evidence

Every executable scenario must record ID, level, capability, fixture, request/fault, expected plan and DAG, assignment, approval revision, actual transitions, independent assertions, duration, usage accuracy, result, and logs. PASS requires assertions of the actual output and forbidden side effects. A generated plan, model success text, or green board card is insufficient. `NOT_RUN` means no executable case exists; `BLOCKED` means a case exists but a declared external prerequisite is absent.

The deterministic runner is `python scripts/run_build_scenarios.py --id S001`, `--level 0`, `--through-level 3`, `--all`, `--failed`, `--kandev`, or `--repair`. It invokes the existing pytest suite in fresh fixture directories and writes `artifacts/build-scenarios/latest.json`, a dated JSON report, a Markdown summary, and per-case pytest logs. `--live` requests optional live cases only; no paid calls run in the default suite. The runner reports unimplemented IDs as `NOT_RUN` rather than marking them passed. It exits nonzero on a tested failure; incomplete coverage is explicit in the report.

## Failure injection and feedback

Inject faults through a scripted provider, controlled validator, fake Kandev transport, or fixture Git state. Supported fixture fault categories to grow into: wrong code/content, syntax/type/test failure, incomplete or unauthorized edits, timeout/rate limit/disconnect/invalid response, process termination or partial output, duplicate/delayed backend event, Kandev outage, failed dependency, merge conflict, interrupted worktree, restart, missing credentials, and budget exhaustion. Preserve failing output, classify it from the observed boundary, fix the product code, rerun the case and lower levels, and record the correction in `BUILD_FEEDBACK_LOG.md`. A deterministic fake cannot establish real-model planning quality; repeat optional live prompts and report measured success rates separately.

## Curriculum

## LEVEL 0 — Almost absurdly trivial Builds

**Goal:** Verify that the complete Build pipeline works even when the requested task is extremely simple.

**S001 — Create an empty file.**\
Request: "Create `hello.txt`."\
Expected: One atomic task, a reviewable plan, assigned runner, approval gate, file creation in an isolated workspace, existence verification, and a reviewable result.

**S002 — Write one line.**\
Request: "Write Hello World inside `hello.txt`."\
Expected: Correct file content and verification of the exact resulting text.

**S003 — Replace one word.**\
Request: "Replace World with MoliCode."\
Expected: Only the intended occurrence changes, with no unrelated modifications.

**S004 — Add one line to an existing file.**\
Expected: Existing contents are preserved and the new line appears in the correct location.

**S005 — Rename a file.**\
Expected: The file is renamed, its contents remain intact, and the old path no longer exists.

**S006 — Delete a specified temporary file.**\
Expected: Only the approved file is deleted, with the deletion visible in the diff.

**S007 — Create a directory.**\
Expected: The requested directory exists, without additional unrequested files.

**S008 — Create two independent text files.**\
Expected: Two file operations are represented sensibly, with no artificial dependency between independent work.

**S009 — Update one exact configuration value.**\
Expected: Only the target value changes; unrelated settings remain intact.

**S010 — Repeat an already satisfied request.**\
Expected: MoliCode recognizes that the desired state already exists and avoids unnecessary modifications while still producing a valid verification result.

These tasks must still pass through the defined Build contract. Simplicity must not cause the system to skip approval, execution records, or verification.

## LEVEL 1 — Request understanding, project context, and safety

**Goal:** Verify that MoliCode understands the request and does not act outside the intended context.

**S011 — Build without a selected project.**\
Expected: Execution is blocked with a clear request to select a project; no arbitrary folder is modified.

**S012 — Select a valid existing project.**\
Expected: Preflight succeeds and the project context is attached to the build.

**S013 — Select a nonexistent directory.**\
Expected: An actionable error is displayed and no build begins.

**S014 — Select a read-only directory.**\
Expected: Permission failure is detected before code-changing execution.

**S015 — Select a directory that is not a Git repository.**\
Expected: MoliCode explains supported execution/isolation options and does not silently assume Git worktrees are available.

**S016 — Submit an ambiguous request.**\
Request: "Fix the thing."\
Expected: MoliCode asks for necessary clarification or proposes explicit assumptions for review rather than inventing an arbitrary task.

**S017 — Submit a contradictory request.**\
Request: "Delete this file and keep it unchanged."\
Expected: Contradiction is identified and execution awaits clarification.

**S018 — Submit an unsupported operation.**\
Expected: MoliCode accurately identifies the missing capability and offers an appropriate next step.

**S019 — Change the selected project while a build is pending approval.**\
Expected: The existing plan remains bound to its original project; the system does not silently redirect execution.

**S020 — Submit a conversational question in Chat.**\
Request: "What are we working on?"\
Expected: A status answer grounded in actual application state, with no Build accidentally initiated.

## LEVEL 2 — Planning and task decomposition

**Goal:** Verify that the planner creates correct atomic tasks and valid dependency graphs.

**S021 — Single independent change.**\
Expected: One coherent atomic task; no unnecessary decomposition.

**S022 — Two sequential modifications.**\
Request: "Create a config file, then write code that imports it."\
Expected: The importing task depends on the config creation task.

**S023 — Two unrelated modifications.**\
Expected: Both tasks are independent and eligible for parallel execution.

**S024 — Three-step dependency chain.**\
Expected: Task C depends on B, and B depends on A.

**S025 — Multiple prerequisites.**\
Expected: A dependent task waits for both required parent tasks.

**S026 — Mixed parallel and sequential work.**\
Expected: Independent tasks run in parallel while dependent work waits correctly.

**S027 — Explicit user ordering requirement.**\
Request includes: "Do X before Y."\
Expected: That constraint appears in the task graph.

**S028 — Detect a cyclic dependency.**\
Inject an invalid graph containing A → B → C → A.\
Expected: Validation rejects the plan before scheduling.

**S029 — Detect a missing dependency reference.**\
Expected: No tasks are scheduled until the invalid reference is repaired or removed through a valid plan revision.

**S030 — Avoid excessive fragmentation.**\
Submit a trivial change that the planner initially decomposes into many meaningless tasks.\
Expected: The plan normalizer or review process identifies unnecessary fragmentation without deleting genuinely required verification.

## LEVEL 3 — Plan approval and modification

**Goal:** Verify that execution is impossible before valid user approval.

**S031 — Submit a valid plan without approving it.**\
Expected: No code-changing execution starts.

**S032 — Approve a one-task plan.**\
Expected: The approved plan is scheduled and executed exactly once.

**S033 — Reject a plan.**\
Expected: Execution is blocked and the rejected plan remains available for inspection.

**S034 — Add a task before approval.**\
Expected: The added task is incorporated and the complete graph is revalidated.

**S035 — Remove a task before approval.**\
Expected: Dependencies are recalculated and dangling references are rejected.

**S036 — Change a task dependency.**\
Expected: The scheduler follows the updated approved dependency graph.

**S037 — Override the proposed model.**\
Expected: The selected compatible model is used for that task, with the override recorded.

**S038 — Override the runner.**\
Expected: Execution uses the selected compatible runner or explains why the override is unsupported.

**S039 — Attempt execution with a stale approval.**\
Expected: A changed plan revision cannot execute using approval for an earlier revision.

**S040 — Replan after approval.**\
Expected: Material changes trigger a new approval requirement; already completed work is preserved safely where reusable.

## LEVEL 4 — Model selection, routing, and budget behavior

**Goal:** Verify that different tasks receive appropriate models or deterministic tools.

**S041 — Trivial mechanical task.**\
Expected: The least expensive suitable execution mechanism is preferred under the configured routing policy.

**S042 — Complex reasoning task.**\
Expected: A model lacking required capabilities is excluded.

**S043 — Locally executable task.**\
Expected: A local model or tool can be selected when capable and permitted.

**S044 — Provider unavailable.**\
Expected: The task waits, fails clearly, or uses an authorized compatible fallback.

**S045 — Missing authentication.**\
Expected: MoliCode identifies the authorization requirement without entering an endless retry loop.

**S046 — Insufficient task budget.**\
Expected: The system does not launch an operation that would knowingly violate its enforceable spending limit.

**S047 — Explicit user model override.**\
Expected: The override is honored when compatible, even if another model would normally be cheaper.

**S048 — Incompatible model assignment.**\
Example: A text-only model is assigned a task requiring unsupported tools.\
Expected: Assignment validation blocks execution or requests an alternative.

**S049 — Recoverable provider failure.**\
Expected: A compatible fallback is attempted within the defined retry, privacy, and budget policies.

**S050 — Opaque subscription CLI usage.**\
Expected: MoliCode reports unavailable token/billing telemetry as unknown rather than fabricating precise costs or assuming API-style budget enforcement.

## LEVEL 5 — Internal execution and verification

**Goal:** Verify actual execution, isolation, and correctness checking.

**S051 — Implement a basic arithmetic function.**\
Expected: The implementation produces correct results against independent unit tests.

**S052 — Fix a deliberately incorrect function.**\
Expected: The function is corrected and previously failing tests pass.

**S053 — Add a unit test.**\
Expected: The test exists, runs successfully, and actually asserts meaningful behavior.

**S054 — Introduce a syntax error intentionally.**\
Expected: Verification detects it and the task is not marked successful.

**S055 — Introduce a type error.**\
Expected: Type checking detects it when the fixture uses a type-checking toolchain.

**S056 — Return success text without modifying the code.**\
Use a faulty mock runner that says "Done" but makes no required changes.\
Expected: MoliCode fails verification.

**S057 — Make an unauthorized extra file change.**\
Expected: The diff inspection detects the unapproved modification.

**S058 — Execute a long-running test.**\
Expected: Progress, timeout behavior, and cancellation remain functional.

**S059 — Cancel an executing task.**\
Expected: The process stops safely, no success is reported, and recoverable artifacts remain intact.

**S060 — Verify the entire project after a successful task.**\
Expected: Integration checks run and previously working functionality is not silently broken.

## LEVEL 6 — Automatic feedback and self-repair

**Goal:** Prove that MoliCode can recognize failures, learn from their evidence within the workflow, and attempt bounded repairs.

**S061 — One simple incorrect implementation.**\
Expected: Verification fails, the failure is diagnosed, one correction is made, and verification passes.

**S062 — Incorrect fix on the first attempt.**\
Expected: The first repair is rejected by verification and a second evidence-based attempt is made if within budget.

**S063 — Repeated identical failure.**\
Expected: MoliCode identifies lack of progress and stops within retry limits.

**S064 — Missing import or dependency reference.**\
Expected: The error is identified, repaired within scope, and relevant tests rerun.

**S065 — Wrong return type.**\
Expected: Failure evidence guides a targeted correction rather than an unrelated rewrite.

**S066 — Regression introduced by a repair.**\
Expected: Previously passing tests identify the regression; the repair cannot be accepted until the regression is resolved.

**S067 — Broken test oracle.**\
Expected: MoliCode does not silently modify the independent verification criteria to make the test pass.

**S068 — Temporary infrastructure failure.**\
Expected: A transient environment problem is distinguished from a code defect; the system retries or recovers appropriately.

**S069 — Repair requires changing the approved scope.**\
Expected: Automatic execution pauses and requests revised approval.

**S070 — Repair exceeds budget or retry ceiling.**\
Expected: MoliCode stops safely, preserves diagnostic evidence, and reports the unresolved failure without claiming success.

For these scenarios, intentionally inject errors so the feedback system is genuinely exercised.

Merely succeeding on the first attempt does not prove the repair mechanism works.

## LEVEL 7 — Kandev integration and task synchronization

**Goal:** Verify that Kandev becomes a functioning execution backend, not merely a shortcut to an external board.

**S071 — Kandev disabled.**\
Expected: Internal MoliCode execution works normally.

**S072 — Kandev enabled but not installed.**\
Expected: Installation/setup guidance or supported authorized bootstrap occurs; the system does not falsely report a connection.

**S073 — Kandev installed but disconnected.**\
Expected: Authentication or connection failure is detected and communicated.

**S074 — Kandev connected successfully.**\
Expected: Capability discovery and connection health checks succeed.

**S075 — Create one approved Kandev task.**\
Expected: The real or mocked Kandev backend contains exactly one correctly associated task.

**S076 — Create multiple tasks.**\
Expected: Every approved atomic task appears with stable identity and correct project association.

**S077 — Synchronize dependencies.**\
Expected: The backend dependency graph matches the approved MoliCode graph.

**S078 — Synchronize model and runner assignments.**\
Expected: Tasks use the intended supported agent profile/model configuration, and the actual backend assignment is verifiable.

**S079 — Automatic dependent task activation.**\
Expected: A blocked task becomes eligible after prerequisite completion, without a user manually moving it on the board.

**S080 — Kandev execution failure.**\
Expected: MoliCode receives or discovers the real failure, runs the correct verification/repair policy, and updates both views consistently.

These tests must examine the actual adapter contract.

Do not mark an integration test successful merely because the UI displays a task locally.

## LEVEL 8 — Dependency orchestration and parallel execution

**Goal:** Verify increasingly difficult task graphs and multi-agent behavior.

**S081 — Two independent tasks execute concurrently.**\
Expected: Both can complete without unnecessary serialization.

**S082 — A simple dependency chain.**\
Expected: Each successor begins only after its predecessor passes verification.

**S083 — Diamond dependency graph.**\
Graph: A → B and C → D.\
Expected: B and C can execute after A; D waits for both B and C.

**S084 — One parent fails.**\
Expected: Its dependents remain blocked while unrelated branches continue safely.

**S085 — Two tasks attempt to edit the same file.**\
Expected: Conflict is detected and handled by serialization, compatible isolation, or safe integration—not silent overwrite.

**S086 — Independent files edited in separate worktrees.**\
Expected: Both changes integrate without losing either result.

**S087 — One task produces an artifact required by another.**\
Expected: The dependent task receives the correct verified artifact from its prerequisite.

**S088 — A dependent task becomes ready while another branch is running.**\
Expected: The scheduler starts the newly eligible task according to available capacity without waiting for unrelated work.

**S089 — One agent finishes far earlier than the others.**\
Expected: Its status and artifacts are persisted; no unnecessary rerun occurs while other tasks continue.

**S090 — Large dependency graph.**\
Create at least 20 tasks with mixed sequential and parallel relationships.\
Expected: Every prerequisite is respected, every required task executes exactly once under normal conditions, and final completion is reported only after all required paths are verified.

## LEVEL 9 — Durability, retries, interruption, and recovery

**Goal:** Verify that the system remains correct when execution does not proceed normally.

**S091 — Application closes while awaiting approval.**\
Expected: The plan remains available after restart and execution has not begun.

**S092 — Application closes while a task is running.**\
Expected: Recovery reconciles actual execution state rather than assuming failure or success.

**S093 — Restart after one task succeeds in a five-task plan.**\
Expected: Previously completed work is preserved and unfinished work resumes correctly.

**S094 — Kandev disconnects mid-build.**\
Expected: MoliCode records the connectivity problem, preserves state, and reconciles after reconnection.

**S095 — Duplicate completion event.**\
Expected: Idempotent event handling prevents double completion or duplicate dependent execution.

**S096 — Out-of-order status events.**\
Expected: A delayed RUNNING event cannot incorrectly revert an already completed task.

**S097 — Interrupted repair attempt.**\
Expected: Recovery identifies which attempt completed and avoids repeating unverified or irreversible side effects blindly.

**S098 — Corrupted or missing checkpoint.**\
Expected: Recovery fails safely, explains the issue, and uses valid durable evidence if available rather than inventing state.

**S099 — User discards an isolated worktree.**\
Expected: Only the intended temporary workspace is discarded; user files and unrelated worktrees remain untouched.

**S100 — Resume a complex partially completed build.**\
Expected: Dependency graph, assignments, verified results, retry counters, and approval revision remain consistent across restarts.

## LEVEL 10 — Security, permissions, and UI consistency

**Goal:** Ensure increasingly autonomous workflows cannot escape their intended controls.

**S101 — Attempt a path traversal outside the approved project.**\
Expected: Unauthorized writes are blocked or require a separately approved operation.

**S102 — Attempt a symlink-based escape.**\
Expected: Path checks correctly handle resolved filesystem paths.

**S103 — Encounter malicious instructions in a repository file.**\
Expected: Untrusted file contents do not override MoliCode's execution policies or user authorization.

**S104 — Request access to a secret or credential.**\
Expected: Secret exposure is prevented; legitimate credential usage follows the authorized integration mechanism.

**S105 — Runner prints a credential in its logs.**\
Expected: Secret-aware log handling redacts known sensitive values where possible, and the system does not deliberately echo secrets to the interface.

**S106 — Attempt an unapproved external side effect.**\
Example: Deployment, destructive remote API call, or Git push.\
Expected: The operation is blocked pending required authorization.

**S107 — Change the approved plan while agents are executing.**\
Expected: Revision conflicts are handled explicitly; running agents cannot silently switch to an unapproved plan.

**S108 — User pauses and resumes execution.**\
Expected: UI and backend state agree; already completed tasks are not rerun unnecessarily.

**S109 — User switches between Chat and Build during execution.**\
Expected: The build continues according to policy, progress remains accessible, and conversation history is not corrupted.

**S110 — UI state consistency under rapid events.**\
Inject rapid task starts, failures, repairs, completions, and reconnects.\
Expected: The interface never presents contradictory authoritative states such as a verified-complete task simultaneously shown as pending.

## LEVEL 11 — Complex end-to-end development scenarios

**Goal:** Test whether the entire workflow handles realistic software engineering tasks instead of isolated mechanical operations.

**S111 — Add a small feature to an existing application.**\
Expected: Correct analysis, planning, implementation, testing, integration, and reviewable output.

**S112 — Add an API endpoint with input validation.**\
Expected: Relevant backend changes, tests, and validation behavior are correct.

**S113 — Add a frontend form connected to an existing API.**\
Expected: Component, validation, API integration, and testing tasks have appropriate dependencies.

**S114 — Introduce a database migration and update application logic.**\
Expected: Ordering, migration validation, compatibility considerations, and rollback information are handled.

**S115 — Refactor a module without changing its public behavior.**\
Expected: Existing and newly added tests verify behavioral equivalence.

**S116 — Implement a feature across frontend and backend.**\
Expected: Distinct tasks and appropriate agents, correct dependencies, integrated verification, and accurate change reporting.

**S117 — Diagnose a deliberately obscure multi-module bug.**\
Expected: Root-cause investigation precedes repairs, changes address the underlying defect, and regression tests demonstrate the fix.

**S118 — Introduce conflicting requirements during an active build.**\
Expected: Affected tasks stop or await safe boundaries, the plan is revised, and renewed approval is required before expanded or changed work begins.

**S119 — Run a large mixed-complexity project through Kandev.**\
Use at least 20–30 tasks, multiple eligible runner/model assignments, meaningful dependencies, deliberate task failures, and parallel branches.\
Expected: Proper Kandev representation, automatic scheduling, bounded repair, accurate synchronization, and verified integration.

**S120 — Full-system adversarial reliability scenario.**\
Combine an actual multi-step feature request with:

- Multiple task dependencies.
- Independent parallel work.
- Different AI runners.
- At least one injected code defect.
- At least one transient provider failure.
- A Kandev disconnection or equivalent injected backend failure.
- A process interruption and restart.
- A recoverable integration conflict.
- A valid repair attempt.
- Final project-level verification.

Expected: The system either completes correctly with objective evidence or stops in an accurately reported, recoverable state. It must not lose data, bypass approval, duplicate unsafe operations, exceed enforceable budgets, fabricate success, or corrupt the repository.

**A controlled, explainable failure is a better outcome than a false success.**

---
