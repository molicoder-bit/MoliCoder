# MoliCoder: unified request orchestration implementation plan

Date: 2026-10-16
Status: **Implemented in 1.4.0; CLI/component/runtime qualification recorded in [implementation evidence](unified-request-orchestration-evidence.md). Installed UI acceptance remains unverified under the user's CLI-only testing instruction.**
Baseline inspected: version 1.3.6, commit `f02888f`.

## 1. Product goal and required outcome

MoliCoder should complete useful project work by choosing appropriate local, free, subscription, or paid AI integrations automatically. Local and free models should handle tasks they can reliably complete. Stronger models should handle demanding reasoning and take over when a cheaper route cannot satisfy the request.

The user submits an objective, not a model configuration. MoliCoder evaluates the objective, supplies instructions and actual tools, checks structured results against evidence, and continues through suitable alternatives when needed. Creating a file is one operation within this system, not the definition of the product.

The required flow is:

```text
User request + conversation + selected project
    -> evaluate intent, required capabilities, scope, and success criteria
    -> choose an eligible runtime / provider / model / effort
    -> supply task instructions, structured output contract, context, and tools
    -> execute tool steps or produce a structured answer / Build plan
    -> independently check the proposed result
         -> verified result: finish and show evidence
         -> recoverable failure: preserve progress, select another suitable route
         -> missing user information / authorization: explain the specific blocker
         -> exhausted routes or budget: preserve work and report incomplete status
```

Build extends that flow with project analysis, capable reasoning, an approved plan of meaningful atomic tasks, per-task assignments, dependency scheduling, verification, integration, and apply. Kandev must display the actual tasks and execution state while work proceeds.

This plan implements the latest clarified workflow while retaining the working contracts in [BUILD_WORKFLOW.md](../BUILD_WORKFLOW.md). When implementation begins, update that specification alongside each changed contract. The latest user direction takes precedence over older descriptions that confine ordinary Chat to tool-free conversation.

## 2. Where the implementation drifted

The problem is architectural fragmentation. Adding more phrase-specific handlers would leave the same failure pattern in place.

| Current source / behavior | Gap | Planned treatment |
| --- | --- | --- |
| `controller.py` recognizes selected file, folder, status, and Build phrases | Other actionable requests can return `None` and enter ordinary conversation | Put every request through shared evaluation; retain exact handlers only as optional optimizations |
| `file_actions.py` handles empty text files | A request for file contents can fall through to advice instead of execution | General typed file operations with content verification |
| `directory_queries.py` interprets natural text as a path | “This directory” can become a literal child directory | Resolve references through the request's project and conversation context before path validation |
| Ordinary Chat and `repository_analysis.py` use different worker paths | Tools and fallback depend on which handler recognized the wording | One request coordinator and lifecycle for Chat, investigation, and Build intake |
| `repository_analysis.py` has tools, citations, and fallback | Its behavior is limited to repository questions and uses route preferences specific to that service | Reuse its retrieval and evidence checks within shared tool execution and route selection |
| `inference/registry.py`, `catalog.py`, `discovery.py`, `router.py`, `policy.py`, and `fallback.py` already exist | Capability facts, selection, availability, and fallback are not consistently used across all paths | Extend and connect these components; avoid a second unrelated registry or router |
| Build has plans, approvals, a scheduler, worktrees, artifact checks, and repair | It does not establish the requested unified request evaluation and capability-based assignments | Reuse its execution machinery under the shared contracts |
| Provider profiles include Codex, Claude, Copilot, and a nonselectable OpenCode preview | Discovery of a binary is not a working integration; Kilo discovery is not a complete runner adapter | Qualify real adapters, including OpenCode and Kilo Code, before making them eligible |
| `kandev/run.py` has its own orchestration path and notes unverified per-task model selection | Opening a board does not prove that the approved DAG and assignments are represented | Share task identities, plan revisions, ownership, and events with Kandev |
| Existing passing tests cover particular components and narrow journeys | They did not establish that arbitrary actionable Chat requests reach execution | Add cross-layer and installed-app acceptance gates with independent filesystem and runtime evidence |

The two reported examples become required regressions, but they do not define the entire fix.

## 3. Architecture and ownership

Introduce a small `orchestration/` package that coordinates existing services. Names below are proposed implementation boundaries, not claims that these modules already exist.

| Component | Responsibility | Existing pieces to reuse |
| --- | --- | --- |
| `RequestCoordinator` | Own request lifecycle, evaluation, dispatch, fallback, cancellation, and completion | Controller, workers, storage, Build entry points |
| `RequestEvaluator` | Turn user intent and project evidence into a typed task specification and acceptance criteria | Intent classification, reconnaissance, project queries |
| `CapabilityCatalogService` | Load cached offerings, refresh facts, expose compatible route combinations | Inference registry, catalog, discovery, health, onboarding |
| `RouteSelector` | Filter by requirements and policy; select model, effort, and runner with a recorded reason | Inference router, eligibility, policy, budget, metrics |
| `ToolSession` | Expose authorized reads, searches, edits, and commands; record actual results | Repository index, directory reads, executor tool dispatcher, validation tools |
| `ResultVerifier` | Check requested outcomes against host-created evidence | Citation checks, artifact checks, validation, worktree checks |
| `BuildCoordinator` | Plan, assign, schedule, verify, repair, integrate, and apply meaningful tasks | Existing Build orchestrator, planner, scheduler, approval, recovery |
| `RuntimeAdapter` | Discover and invoke one execution runtime; normalize events and results | Internal agent and current CLI provider adapters |
| Event/persistence layer | Durable request, attempt, task, tool, verification, and board events | `ChatStore`, inference ledger, existing run events |

The desktop UI and CLI call the same application service. They may render differently, but they must not choose different execution logic for the same request. The UI displays state and gathers meaningful input; it does not decide that a task succeeded.

Preserve user chats, project associations, settings, receipts, and existing unfinished runs. Introduce additive, versioned storage migrations and compatibility readers for old records.

## 4. Cached provider, model, effort, and capability catalog

### Identity and facts

Keep these concepts separate:

- **Provider / offering:** inference endpoint, account, exact model, and access route, including any gateway.
- **Model:** inference capability and supported reasoning settings.
- **Runtime:** internal tool loop, Codex CLI, OpenCode, Kilo Code, or another qualified coding runner.
- **Execution backend:** internal scheduling or Kandev-managed execution.

A free coding runtime does not imply free inference. A model name alone does not establish its tools, actual provider, entitlement, or cost.

Extend the current records with the following facts or linked records:

| Group | Required fields |
| --- | --- |
| Identity | Stable offering ID, provider ID, model ID/version or digest, runtime ID/version, account reference, gateway route when applicable |
| Capabilities | Structured-output support, tool-call protocol, project read/search/edit/command support, context/output limits, streaming, cancellation, resumability |
| Reasoning | Supported effort values, default effort, mapping to real API/CLI parameters, unsupported versus unknown |
| Availability | Installation, configuration, authentication, model availability, health, cooldown, known quota and reset time |
| Quality | Task-family qualification results, sample counts, verification pass rates, latency, last evaluation, provenance |
| Economics | Local, recurring free, temporary credit, subscription, metered, or unknown; known prices and limits with timestamps |
| Policy | Enabled/disabled state, project trust, permitted data scope, permitted spend and runtime restrictions |
| Freshness | Last successful discovery, last attempt/error, source, adapter version, snapshot revision, stale state |

Some capabilities belong to the model, others to its runtime. Route eligibility uses their compatible combination. Native function calling is not mandatory if a model passes the constrained JSON tool protocol. A runtime without usable tool access cannot turn a text-only reply into an executed task.

### Startup and refresh behavior

1. Load the last complete catalog snapshot immediately so opening the app remains responsive.
2. Start a bounded background refresh **every time the app opens**. Refresh configured integrations and installed local runtimes; record unavailable integrations explicitly.
3. Use supported discovery endpoints, installed CLI metadata, and versioned adapter metadata. Do not run paid model generations or download models just to refresh the list.
4. Merge successful discovery results atomically. A failed refresh preserves prior facts as stale; it does not erase the catalog or re-enable user-disabled offerings.
5. Keep unknown capabilities unknown. A model list that omits effort levels is not permission to invent them. Supported static metadata needs a source and version; empirical qualification is recorded separately.
6. Before dispatch, recheck relevant auth, availability, capability compatibility, policy, and budgets. Pin the request's catalog revision, recording any later refresh used for fallback.
7. Also refresh after credential/model configuration changes, explicit user refresh, or evidence that a selected route has disappeared. Debounce repeated failures.

Acceptance: a new installed model appears after reopening; removed or unavailable routes are not selected; an offline launch retains readable cached facts; no startup inference charge is introduced.

## 5. Evaluate every request before choosing how to handle it

Create a persisted `RequestSpec` containing:

- Request/chat/project IDs, original text, submission time, and original conversation references.
- Canonical project root and the scope captured at submission; switching projects later cannot retarget running work.
- Intent: conversation, project investigation, bounded local action, development Build, or clarification.
- Requested outcomes, required capabilities, relevant context, complexity/risk, and acceptance checks.
- Whether reasoning/planning is needed, routing policy, resource limits, and existing authorization references.
- Interpretation confidence and unresolved ambiguity, as advisory information rather than proof of correctness.

Use exact deterministic interpretation when the operation is unambiguous. Otherwise invoke a qualified inexpensive evaluator with the request, relevant conversation, known project context, and a strict evaluation schema. The evaluator may ask for read tools before deciding. If evaluation is invalid or inadequate, escalate evaluation itself to a more capable reasoning route.

Do not ask the user to choose a model or restate a resolvable request. Clarify only missing information that affects the result, such as two plausible destination files. Requests that ask how to do something remain explanations; requests asking the app to do it enter an action workflow.

Resolve “here,” “this directory,” “this project,” and references to recently inspected folders from typed context. An explicitly quoted literal path takes precedence. Preserve Windows path spelling for display while validating canonical paths, case handling, links/junctions, and authorized boundaries. Never append unresolved conversational words to the project root.

If a project-dependent action has no project, show a clear red warning with a useful project-selection action. General conversation can continue. A known missing file is an observed result, not a reason to try models until one invents it.

## 6. Structured JSON protocol and host verification

JSON provides a machine-readable contract for decisions and results. It does **not** prove that a model performed the requested work. The host owns the final state and the evidence used to justify it.

Implement versioned JSON Schemas for evaluation, agent turns, Build plans, and normalized attempt results. Reject unknown fields, malformed values, invalid state combinations, and references to nonexistent evidence. Validate provider-specific schema compatibility before sending a request, including nested required fields; preserve a regression for the earlier `artifact_checks` schema rejection.

### Evaluation output

The evaluator returns these required fields: `schema_version`, `intent`, `objective`, `scope_refs`, `required_capabilities`, `complexity`, `needs_planning`, `acceptance_checks`, `missing_information`, and `reason`. Each acceptance check has a stable ID, typed check kind, target, and expected result or declared semantic-review requirement. The host validates this proposal against the original request and allowed scope.

### Agent turn output

All fields in this example are required. Empty arrays and a nullable failure field avoid omitted-field ambiguity. Tool arguments use a per-tool schema.

```json
{
  "schema_version": "1.0",
  "status": "needs_tools",
  "message": "I will inspect the selected project directory.",
  "tool_calls": [
    {
      "call_id": "call-1",
      "name": "list_directory",
      "arguments": {"scope_ref": "project_root", "relative_path": "."}
    }
  ],
  "evidence_refs": [],
  "unmet_criteria": [],
  "failure": null
}
```

Allowed model statuses: `needs_tools`, `answer_ready`, `work_ready`, `cannot_complete`, and `needs_clarification`.

- `needs_tools` requires one or more valid tool calls. Calls are proposals until the host authorizes and executes them.
- `answer_ready` and `work_ready` require no pending calls. For project facts or actions, they must reference relevant host-issued evidence.
- `cannot_complete` requires a typed failure reason and unmet criteria; it invokes routing/recovery policy.
- `needs_clarification` requires a specific missing fact. The coordinator first checks whether existing context or tools can supply it.
- A response containing an `echo` command is not evidence that a file was created.

Give the model its task, success criteria, available tools, scope, bounded execution instructions, and schema together. Feed actual tool results back into the loop. Native tool APIs and constrained JSON tools normalize to the same protocol. For CLI agents, require a structured final result and normalize observed tool/process events; unparseable prose cannot authorize completion.

### Host result and evidence

Persist a separate `VerifiedResult` with request/attempt IDs, final host status, satisfied/unmet criterion IDs, evidence IDs, artifacts, routing history, actual provider/model/effort when observable, and usage. These fields are host-owned; the model cannot mark itself verified.

| Request type | Required verification |
| --- | --- |
| List a directory | Actual directory-read record, resolved path, entries, time, truncation/errors |
| Locate/explain project behavior | Actual files and quoted source ranges; source freshness; answer relevance and remaining uncertainty |
| Create or update a file | Correct destination, actual bytes/text as specified, before/after hashes, intended diff, no unexpected writes |
| Run a command | Actual command/arguments, working directory, exit status, stdout/stderr, and requested behavioral checks |
| Fix a bug or implement a feature | Acceptance checks against real behavior, relevant regression results, scope/diff checks |
| General conversation | Valid response contract and relevance; no fabricated tool actions or claim of externally verified facts |

Not every semantic claim can be proved deterministically. Use explicit checks where possible and qualified review where necessary, recording the distinction. A second model agreeing with the first is advisory evidence, not independent proof that an action occurred.

## 7. Shared tools and execution

Expose a bounded, typed tool set: directory listing, file reads, code search, project/Git status, directory creation, file writing, patch application, approved command execution, and validation. Reuse existing implementations where they meet the shared contract.

Every tool call records its request/attempt/task IDs, canonical scope, arguments, authorization, start/end, actual output/error, and artifact evidence. Validate scope and arguments immediately before execution. Enforce time/output bounds and cancellation. Repository text and tool output are data, not new authority to expand the task.

File writes must preserve requested contents and use existing-content preconditions. A retry must not append duplicate content, overwrite changed user work, or repeat an already completed action. New files use create-only semantics unless replacement is authorized. Multi-step development uses isolated worktrees; a bounded non-Git file action can use a recorded snapshot and exact-file verification without requiring Git setup.

Command tools execute real processes in the authorized workspace and capture real output. Describe tool availability accurately to the model. Start with existing project validation commands and explicitly authorized commands; implement argument-array invocation and a defined policy for other development commands. Tool descriptions must never imply access the runtime lacks.

An unambiguous deterministic action can use no AI inference while still producing the same evaluated request, authorization, operation records, and verification outcome. Model-backed execution must also handle paraphrases and unfamiliar supported tasks; exact phrase matching is an optimization, never the capability boundary.

## 8. Automatic route selection and fallback

### Selection policy

Select the complete route: execution backend, runtime, provider offering, model, and effort. Keep task execution priority separate from model suitability.

1. Filter candidates by actual availability, required tools, context size, output protocol, policy, authorization, and enforceable budgets.
2. Require the quality level appropriate to the task family. Use recorded qualification with sample counts and freshness; do not infer capability from model names or self-confidence alone.
3. Prefer deterministic execution when reasoning adds no value. For suitable inference work, prefer qualified local/free routes, then authorized subscription or metered routes according to policy.
4. Among qualified routes, compare observed reliability, expected total attempts/cost, and latency. Do not let nominally zero token cost justify endless retries with an incapable local model.
5. Choose the lowest supported effort that meets the task's qualification requirements. Record requested effort, transmitted parameter, and observed effort separately; use unknown where observation is unavailable.
6. Persist eligible alternatives, exclusions, catalog revision, and a short selection reason. Use stable tie-breaking for reproducibility.

Use a quality gate plus a transparent ordering initially; avoid inventing a universal numerical intelligence score. Unqualified routes can be evaluated in isolated qualification scenarios before normal autonomous selection.

### Fallback conditions and behavior

| Evidence | Action |
| --- | --- |
| Explicit `cannot_complete`, wrong capability, repeated unsupported tool requests | Pick the next eligible route suited to the missing capability |
| Invalid JSON or protocol response | One bounded correction opportunity; then use another qualified route |
| Instructions instead of requested execution, irrelevant answer, missing evidence, failed acceptance | Treat the attempt as incomplete; repair or change route |
| Provider unavailable, rate-limited, model removed, auth failure | Record failure/cooldown; try another already authorized route where possible |
| File genuinely absent, permission boundary, missing project, ambiguous user objective | Return the observed fact or exact blocker; do not evade it through another model |
| Unknown outcome after a write or external runner timeout | Reconcile tool journal, files, process/backend state before any further mutation |
| Scope expansion or budget/authorization boundary | Preserve work and request only the required new decision |
| User cancellation | Stop the current work and all pending retries/fallbacks |

Fallback carries the original objective, acceptance checks, relevant context, failed-attempt diagnosis, completed operations, current diff/checkpoint, and remaining work. It does not restart the user's task blindly. Stop or reconcile the earlier writer before launching another writer.

Initial configurable limits: at most three distinct routes per request phase, one schema-correction retry per phase, and two repair attempts per Build task. Apply a shared request/run deadline and token/cost/call limits across these attempts so nested retries cannot multiply without bounds. Tune limits using recorded scenarios; do not describe these initial values as measured optimal settings.

Switch automatically within existing user-approved provider, data-sharing, spend, and plan policies. Display a short event such as “Local model could not complete the checks; continuing with …”. No manual model selection should be required. If all eligible routes fail, report unresolved criteria and preserve recoverable work.

## 9. Build planning, atomic tasks, and approval

### Planning policy

For substantive development work, default to a qualified frontier reasoning model at a sensible supported effort. Do not default to the most expensive/extreme model or maximum effort. A free/local reasoning route can replace that default only after it meets the same planning qualification criteria for the relevant complexity.

Do not hardcode today's marketing names into the policy. Select from the refreshed catalog using verified reasoning ability, context needs, available effort, and user constraints. If no qualified planner is available, report that blocker instead of silently accepting a weak plan.

An exact trivial operation may produce a deterministic one-task plan and avoid a frontier call. This preserves the purpose of using local tools for work that does not need AI reasoning.

### Task contract

Extend the existing plan schema rather than inventing a parallel plan representation. Each task needs:

- Stable ID, coherent objective, expected outcome, input references, and affected scope.
- Dependencies, acceptance criteria, verification commands/check IDs, and expected artifacts.
- Complexity, risk, required capabilities, and reason for the selected runtime/model/effort.
- Primary assignment, permitted fallback policy, resource limits, and repair limit.
- Durable status, attempts, output/checkpoint references, and verification records.

Atomic means a coherent, independently verifiable change suitable for its own review/merge, possibly depending on earlier completed tasks. Keep implementation and its focused tests together where practical. Do not split “open file,” “write code,” and “save file” into artificial tasks.

Validate the DAG for cycles, missing dependencies, incomplete acceptance coverage, conflicting write scopes, and excessive fragmentation. Replace the current five-task schema ceiling with a documented configurable plan-size bound; large coherent features should not be forced into five overloaded tasks. Do not increase task counts just to appear sophisticated.

Schedule ready tasks according to dependencies, risk, available runtime capacity, and useful critical-path progress. Independent tasks can run concurrently only when workspace isolation and resource limits permit it. A dependent starts only after its prerequisites have independently passed.

### Approval versus automation

Keep default Build review tied to the exact plan revision, project, scope, and authorization policy. Show assignments and the permitted fallback envelope before approval. Once approved, tool steps, scheduling, in-scope repair, and eligible provider switches proceed automatically without repeated permission prompts.

Chat requests for actions enter this same operation/Build lifecycle and show the review or progress inline, with a link to the Build view. The user should not have to retype the request or manually move it between modes. A bounded local action may use an existing project action policy; when review is required, present one concrete action review.

Changing the objective, widening write scope, allowing a new external destination, or exceeding the approved spend policy requires a new decision. Changing to a route already covered by the approved fallback policy does not invalidate approval. Apply remains governed by the existing explicit apply policy.

After task verification, integrate outputs and rerun project-level checks. `ready_to_apply` and “applied to your project” remain distinct states. Completion text must identify the actual artifact location until apply succeeds.

## 10. OpenCode, Kilo Code, and other runtime integrations

Interpret “kalicode” in the request as **Kilo Code**, alongside OpenCode. Confirm exact installed runtime identity during discovery rather than assuming CLI compatibility.

For each adapter, inspect the installed version and current official interface during implementation. Establish model discovery, model/effort selection, authentication detection, structured output, tool permissions, working directory, event streaming, timeout, process-tree cancellation, exit/error handling, and resume/reconciliation behavior.

Normalize adapter execution through the same request/result/event contracts. Keep requested and actual model identity separate when a CLI chooses its own default. If a runtime can silently switch an underlying provider, either constrain that behavior or record and authorize the actual route; it cannot bypass the orchestrator's policy.

Required delivery sequence: internal tools + Ollama through the shared service; existing supported CLI adapters; OpenCode qualification; Kilo Code qualification. Provider catalog endpoints and runtime adapters remain separate integrations even when they share a brand.

An adapter becomes selectable only after real isolated read, edit, verification, failure, and cancellation scenarios pass. Missing credentials or unsupported capabilities produce explicit unavailable/partial status. The plan does not assume a discovered binary or configured gateway is ready for Build.

## 11. Kandev, progress, and application usability

Use one logical plan and task-ID mapping for internal and Kandev execution. MoliCoder owns the objective, plan, routing, verification, and orchestration decisions. Persist exactly one execution owner per task so mirroring a task cannot launch it twice.

- When Kandev is configured, create/update the actual approved task graph automatically and associate stable external IDs.
- Show per-task runtime/model/effort, dependency blockers, running state, checks, failures, repairs, and results using the supported Kandev API.
- If a field or dependency mechanism is unsupported, show an accurate compatible representation and retain enforcement in MoliCoder. Do not claim the board enforces something it only displays.
- Support opening the relevant board during planning/execution; network calls and launches must not block the UI thread.
- For internal execution with a connected board, mirror status only where the API supports it; mark MoliCoder as owner. For Kandev execution, dispatch through Kandev and reconcile its events with host verification.
- Buffer/reconcile board updates after disconnects. Reconnect by stable IDs and event cursor; never recreate tasks blindly.

Drive UI progress from persisted events: evaluating, inspecting, planning, awaiting review, executing, switching route, verifying, repairing, integrating, and applying. Animate active work, display elapsed time and the current operation, and stop the animation on terminal or blocked states. Show indeterminate activity when progress is unknown; do not manufacture percentage completion.

Keep the active stage visible while scrolling. Distinguish waiting for a model from executing a tool. Support reduced motion, readable contrast, obvious disabled controls with an explanation, and the missing-project warning. Loading animation must not depend on receiving tokens and must not imply a stalled process is making progress; show time since the last real update.

## 12. Persistence, cancellation, and recovery

Persist request specifications, catalog revisions, route decisions, attempts, tool journals, evidence, approval revisions, tasks, worktree checkpoints, board IDs/cursors, and usage. Use one correlation chain from the user's message to every resulting operation.

Request state flow:

```text
received -> evaluating -> gathering_context -> routing
    -> awaiting_input / awaiting_approval
    -> running -> verifying -> completed
                     -> recovering -> routing / running
                     -> blocked / failed / cancelled
```

Context gathering may repeat within bounded evaluation/execution. Build expands the running stage using its existing plan/task/integration state machine. Only the host's transition service may persist terminal success after required verification; recovery cannot turn an unknown outcome into success.

Cancel reaches the evaluator, model stream, local command process, external runner, scheduler, and Kandev owner as applicable. Once cancellation begins, no new tool action or fallback starts. On restart, reconcile interrupted attempts against disk, process state, and backend records before resuming. Preserve completed verified work and partial evidence.

Keep the normal installed user data directory stable across launch locations and upgrades. CLI test databases must be explicit and distinguishable from the user's real profile. Persist drafts or coordinate a clean application close before installing a release.

## 13. Implementation milestones and dependencies

M01–M09 are implemented and qualified at the component, CLI, and real runtime layers described in the evidence report. M10 is implemented with headless Qt coverage; its installed UI gate remains unverified. M11 includes the package and release checks, with installed UI acceptance deferred under the user’s latest CLI-only instruction. These statuses do not imply every acceptance layer has passed.

| ID | Milestone and main changes | Depends on | Implementation gate |
| --- | --- | --- | --- |
| M01 | Define schemas, request/result records, state transitions, and storage migrations; align `BUILD_WORKFLOW.md` | None | Strict valid/invalid examples; old chats/runs still load; host-only success transitions |
| M02 | Extend catalog and add startup refresh, runtime compatibility, effort metadata, and freshness handling | M01 | Refresh/offline/disabled/removed-model behavior verified without inference calls |
| M03 | Implement shared evaluator, context/reference resolution, and RequestSpec generation | M01–M02 | Natural paraphrases, follow-ups, project switching, explanation versus action classification |
| M04 | Connect shared tool sessions, execution receipts, and host verification | M01, M03 | Read/search/write/patch/command outcomes verified against actual artifacts; duplicate writes prevented |
| M05 | Implement task-family route selection, bounded fallback, cooldowns, and checkpoint handoff | M02–M04 | Local failure reaches a capable authorized alternative; incomplete work never reported complete |
| M06 | Route desktop Chat and CLI through the same coordinator; integrate inline action review and progress | M03–M05 | Exact screenshot requests and broader actions use shared execution; ordinary conversation still works |
| M07 | Connect qualified planning, task assignments, DAG execution, repair, and apply to shared contracts | M02–M05 | Coherent multi-file feature planned and verified; assignment/effort reasons recorded; approvals remain valid within policy |
| M08 | Adapt existing coding CLIs, qualify OpenCode and Kilo Code with real runtime behavior | M02, M04–M05 | Each enabled runtime passes read/write/failure/cancellation qualification; unsupported ones remain visibly unavailable |
| M09 | Connect Kandev task graph, ownership, assignments, event sync, and recovery | M07–M08 | Real board reflects the approved tasks and host-verified results; reconnect creates no duplicates |
| M10 | Complete UI activity, readable states, cancellation, restart recovery, and profile consistency | M06–M09 | Responsive running UI, truthful blockers, recoverable interrupted work, stable installed user data |
| M11 | Run progressive acceptance, preserve evidence, package, and validate installed release | M01–M10 | Required applicable journeys pass at their declared test layer; remaining blockers explicitly prevent a 100% claim |

Build M01–M06 as the first complete user-facing slice: a request is understood, tools act, results are checked, and fallback works. Then extend that same path to substantive Build work and the integrated board. Do not ship isolated new phrase handlers as completion of this plan.

## 14. Acceptance scenarios and evidence

Use isolated fixture projects and explicit test profiles. Use the CLI for fast iteration and real provider/tool checks. The user subsequently instructed: “do not take control of my computer, just use the CLI for anything that you need to test.” Therefore installed-UI journeys below remain NOT RUN; headless Qt checks and CLI passes are recorded separately and do not substitute for installed-UI acceptance.

Legend: **C** = shared-service/CLI integration; **P** = real provider/runtime; **U** = installed desktop UI; **K** = real Kandev. Required layers identify separate checks, not interchangeable ways to declare success.

| ID | Scenario | Required result | Layers |
| --- | --- | --- | --- |
| A01 | Open app with cached catalog; reopen after model availability changes | Responsive startup; refreshed facts; visible stale/error status when offline | C, U |
| A02 | General question and “How would I create a file?” | Useful answer; no unintended mutation | C, U |
| A03 | “List this directory,” “What is here?”, and a follow-up referring to a previously inspected folder | Correct resolved location and actual entries; no literal `this directory` suffix | C, U |
| A04 | “Create a file called Iworked.txt with the text \"yeay\" in it” | Action/review lifecycle, actual file with exact specified content, truthful result location | C, U |
| A05 | Paraphrase A04, then request an edit, a nested folder, and reading the result | General tool workflow handles each request; output and scope independently checked | C, P, U |
| A06 | Locate behavior across several project files; ask a contextual follow-up | Relevant files/lines, grounded explanation, preserved conversation context | C, P, U |
| A07 | Request a missing file/folder; use a path containing spaces or an explicitly literal reference-like name | Accurate error or exact path handling; no invented content or unnecessary provider escalation | C, U |
| A08 | Submit project work with no selected project | Clear red blocker and useful action; no hidden destination or provider call for blocked work | C, U |
| A09 | Local model replies with command advice, invalid JSON, or unsupported completion claims | Failure detected; bounded correction/fallback; no false completed state | C, P, U |
| A10 | Local model reports inability or cannot pass checks; a stronger authorized model can complete | Automatic suitable switch, preserved context/progress, actual final checks pass | C, P, U |
| A11 | Preferred provider down, unauthenticated, rate-limited, or missing requested model/effort | Eligible alternative selected; exclusions recorded; no unsupported effort sent | C, P, U |
| A12 | All eligible routes fail or request budget is exhausted | Honest incomplete state, remaining criteria, retained artifacts; no endless loop | C, U |
| A13 | A tool writes successfully, then response delivery fails; app retries/restarts | Reconciliation prevents duplicate or destructive replay | C, U |
| A14 | Run an authorized project command and handle a failing command | Real process output and working directory; errors lead to suitable recovery | C, P, U |
| A15 | Simple action versus a moderate bug fix versus architectural planning | Appropriate distinct execution/planning choices and supported effort, with reasons | C, P, U |
| A16 | Multi-file feature requiring dependent and independent tasks | Meaningful task boundaries, detailed plan, assignments, correct scheduling and final behavior | C, P, U |
| A17 | Reject/edit a plan; use a stale approval; fallback within an approved envelope | No execution on rejected/stale plan; valid in-policy fallback proceeds without another prompt | C, U |
| A18 | Task fails verification; bounded repair succeeds or exhausts | Checks remain intact; failure history retained; dependents wait for verified success | C, P, U |
| A19 | Execute read/edit/check/cancel through OpenCode and Kilo Code individually | Real artifacts, attributable model/effort where supported, normalized status, effective cancellation | C, P, U |
| A20 | Open Kandev during a multi-task Build; disconnect/reconnect it | Actual tasks/assignments and progress, responsive loading states, no duplicate launches | C, U, K |
| A21 | Cancel during evaluation/tools/fallback; restart during a run; switch project/chat | No late cross-chat output, wrong-project writes, orphan duplicate execution, or lost verified progress | C, U |
| A22 | Integrate and apply completed work; inspect it from Chat | Actual user project matches verified output, unrelated edits preserved, Chat sees fresh contents | C, P, U |
| A23 | Attempt out-of-scope tool arguments or instructions embedded in source text | Scope/policy enforced; no fallback used to bypass a real restriction | C, U |
| A24 | Repeat simple journeys after complex work in the installed release | Earlier behavior still works; model route, profile, version, progress, and disabled states remain truthful | U |

Expand paraphrases and fixtures across task families. Keep representative held-out prompts out of parser-specific development so the acceptance suite measures general behavior. Record model-quality evaluations separately from protocol/adapter qualification; supporting tools does not prove a model uses them effectively.

For each scenario record: ID, original input, expected checks, app version/commit, profile and fixture path, entry point, catalog revision, selected/actual routes and effort, selection reason, attempt/tool/event IDs, artifacts and hashes, checks, usage and unknown costs, screenshots where available, observed result, and PASS/FAIL/PARTIAL/BLOCKED. Preserve initial failures and subsequent fixes.

If real UI interaction or a required provider/Kandev account is unavailable, mark that layer **BLOCKED**, with the exact missing verification. Do not count it as passed because a mock, unit test, or CLI check passed. Do not reuse historical “all tasks complete” claims as evidence for this plan.

## 15. Completion and delivery criteria

- [x] Every Chat and Build request reaches the shared evaluation/lifecycle service.
- [x] Provider/model/runtime/effort selection uses the startup-refreshed catalog and a recorded task-based policy.
- [x] Local/free models demonstrably complete suitable work, and incapable attempts trigger automatic qualified fallback.
- [x] Structured model output is validated; host checks determine whether requested actions actually succeeded.
- [x] Directory access, project analysis, file changes, and authorized commands work beyond a fixed list of phrases.
- [x] Substantive Build planning uses qualified reasoning and creates coherent tasks with automatic assignments.
- [x] Approval, scheduling, repair, fallback, integration, and apply operate as one consistent workflow.
- [x] OpenCode and Kilo Code are implemented and qualified, or explicitly recorded as incomplete with blockers; discovery alone does not close the item.
- [x] Kandev displays the real plan and execution states, with usable activity indicators and verified synchronization.
- [x] Cancellation/recovery preserve work and prevent duplicate effects or cross-project writes.
- [ ] Applicable installed-app acceptance scenarios pass with inspectable evidence; blocked/partial cases remain explicit.
- [x] Release documentation states exactly what was verified, what remains incomplete, and which version contains the implementation.

Implementation and release evidence are recorded in the linked report. Checked items indicate delivered code and the declared component/runtime evidence, not an installed-UI acceptance claim. Legacy low-level `ask`/executor APIs remain compatibility interfaces; desktop requests and the shared `request` CLI use the coordinator. The normal desktop Build scheduler is sequential, and the default plan ceiling is 30 tasks (the parser also accepts an explicit `max_tasks` bound). Preserve user data and drafts when installing.
