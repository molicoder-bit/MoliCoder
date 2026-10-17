# Unified request orchestration: implementation and evidence

Historical 1.4.0 release evidence. Subsequent conversation defects and 1.4.1 qualification are recorded in [the conversation follow-up](conversation-reliability-evidence.md).

Date: 2026-10-16 (America/New_York). Release: **1.4.0**. Baseline: `f02888f` / 1.3.6.

## Delivery status

The orchestration implementation is delivered in source and the Windows package. Verification uses CLI, independent filesystem/process checks, real provider calls, real Kandev API calls, and headless Qt tests. **Installed UI acceptance is NOT RUN**, following the user's instruction to use only the CLI and not control their desktop. This is not a 100% acceptance claim.

The installed 1.3.6 window remained open during packaging. After the user closed it and authorized the CLI installation, `scripts/install.ps1` installed 1.4.0 and preserved the previous install. Installed headless verification is recorded below; interactive UI acceptance remains NOT RUN.

## Implemented

- One desktop/shared-CLI coordinator for evaluation, typed tools, host verification, route changes and durable receipts. General actions enter tool execution. Explicit Build submissions register typed intake and delegate to their persisted Build run; delegation never claims completion. Empty-file/folder Build shortcuts use the same lifecycle with no inference.
- Strict evaluation and turn schemas, known evidence IDs, literal source citations, real file bytes/hashes and process outcomes. Actions finish from host checks rather than a final model success claim. Semantic explanation correctness remains a review judgement.
- Cached startup model/capability refresh; configured API offerings; native OpenCode/Kilo model inventories; Codex advertised effort metadata. Disabled/removed routes, stale discovery, observed outcomes and durable failure cooldowns are recorded. Settings exposes the catalog and project trust for all five coding runtimes.
- Bounded local-first routing and automatic trusted fallback. Completed tool operations carry forward. Restart recovery checks process ownership and actual file state; unknown command outcomes cannot replay automatically. Denied scope/permission is a blocker, never a reason to evade policy through another provider.
- Normal advertised Codex model/medium effort for substantive planning, up to 30 meaningful tasks, reviewed assignments and permitted handoffs. Planning can continue through another trusted reasoning runtime on provider failure. Hybrid can assign bounded low-risk tasks to a trusted economical native runtime. An unavailable local model is excluded from task execution.
- OpenCode and Kilo adapters use native executables, stdin prompts, pinned model profiles, price/availability checks, scoped edit permissions, bounded calls and process cancellation. CLI tool permissions do not establish independent OS sandboxing.
- Internal Build tasks project onto a real Kandev 0.97 board with stable external IDs, dependencies, assignment/check descriptions and current state. Mirroring cannot launch agents. Reconnect retries durable pending updates by external ID. Board loading and service startup use real readiness/progress.
- Inline operation review, automatic Chat-to-Build preparation, request animation, guarded late callbacks, persisted drafts and resume for interrupted requests. Schema 16 migrates additively with a pre-upgrade backup. Chat/Build history deletion also removes the corresponding request events.

## Regression and package gates

Final complete regression: **721 passed, 7 skipped**, zero failures/errors (`release-final-regression.json`), across 728 collected checks. Source/test snapshots matched before and after the run. Three Python files subsequently had only trailing blank lines normalized, with AST equality checked; documentation changes do not affect runtime behavior. Earlier complete regression: **720 passed, 7 skipped**, zero failures/errors (`release-regression.json`). Later shared Build intake/privacy delta: **49 passed**. Deterministic Build shortcut delta: **33 passed**. The final full run records matching source/test snapshots before and after execution.

The seven opt-in skips are legacy real Codex/Claude/Ollama tests gated by environment variables or explicit subscription use. They are not counted as passes. Separate real runtime qualifications below ran outside those skipped tests.

## Real runtime and artifact evidence

Artifacts are retained locally under `C:/Dev/LocalModelChat/artifacts/unified-request-20261016/` and excluded from Git because they contain process traces and fixture profiles. Each request JSON includes the original request, correlation ID, routes, evidence, checks and usage. Tests use explicit fixture databases and do not replace the user's profile.

| Layer and input | Host-observed result | Evidence |
| --- | --- | --- |
| Shared CLI + actual Ollama: create `Iworked.txt` with text `yeay` | PASS; exact `b"yeay"` on disk, approximately 10.8s | `create-v4.json` |
| Actual local-to-free handoff after directory conversation: create `UIWorked.txt` with `yeay` | PASS; Ollama's unsupported reply rejected, OpenCode continued automatically, exact bytes checked | `local-free-handoff-qualified.json`, request `f367a260-7561-4a97-b618-734c96b7604c` |
| Shared free-runtime create, then edit `before` to `after` in a Windows text file | PASS; actual content and preserved CRLF checked | `shared-free-qualified-v3.log` |
| Shared free-runtime source question: locate `choose_runner` | PASS; actual `routing.py:1–2` quotes and freshness verified | `source-final-qualified.json`, request `6f357833-9776-4499-bd30-cc192e2be96c` |
| Controlled unavailable Ollama endpoint, trusted Codex fallback | PASS; actual source located through normal `gpt-6.1-sol`, medium effort | `external-fallback-v2.json`; predates the stricter citation extension |
| Real native OpenCode and Kilo: read/fix arithmetic, verify, reject prohibited operations, cancel | PASS for both; three independent assertions, synthetic `.env` read denied, out-of-scope edit denied, sentinel unchanged; active cancellation approximately 0.44s / 0.37s | `current-runtime-qualification.json` |
| Real frontier planning + free Build execution: converter core/tests, then dependent CLI/tests/README | PASS; Codex `gpt-6.1-sol` medium plans, OpenCode `opencode/big-pickle` implements both in one attempt each; task/final checks and separate holdout exit 0; source checkout unchanged | `feature-build-qualified.json`, `feature-build-plan.json`; result **ready_to_apply**, integration commit `3c697ae395e434720b690b9108eb654bbe5abc75` |
| Real Kandev API: mirror two tasks with a dependency, change state, reconnect | PASS; same external IDs and native task IDs, assignments/checks retained, no agent sessions launched | `kandev-fixture/mirror-qualified.json`; harness drives internal task state; owned fixture server stopped after qualification |
| Packaged executable: create `packaged.txt` with `yeay` | PASS; Ollama failed, real OpenCode continued, exact bytes and exit 0 | `package-action-qualified.json`; earlier package, before the final intake-only changes |

The real feature result was deliberately left ready for apply in its isolated worktree. That journey does not claim it was applied to the user's project. Existing apply/component regressions test final application and preservation of unrelated changes.

## Acceptance coverage by declared layer

`C` denotes shared-service/component/CLI checks; `P` real inference/runtime; `K` real Kandev; `Qt` headless desktop components. None of these imply installed UI acceptance. All `U` layers in the plan remain NOT RUN.

| Scenarios | Evidence and extent |
| --- | --- |
| A01 catalog/startup | C/Qt: cached, offline, disabled, removed, native metadata and asynchronous startup contracts |
| A02 general explanation versus action | C/Qt: explanation has no writes; model proposals are validated |
| A03 directory references/follow-ups | C/Qt and packaged CLI: actual entries, project-relative references and conversation context |
| A04 requested file contents | C/P and packaged CLI: actual `yeay` bytes; review and journal |
| A05 paraphrase/edit/folder/read | C/P: general tools, CRLF-preserving real edit, scoped reads; folder/deterministic paths in C/Qt/package |
| A06 source investigation | C/P: actual indexed source, citations and freshness; fake-provider follow-up continuity |
| A07 missing/literal/spaced paths | C/Qt: correct failures and path handling; no invented result |
| A08 no selected project | C/Qt: truthful blocker, red warning and disabled Build controls |
| A09–A10 incapable local output/fallback | C/P and package: protocol/advice/claim rejection; actual local-to-free continuation |
| A11 provider failures/effort | C: auth/quota/model/effort exclusions and planning handoff; P: controlled unavailable local-to-Codex path |
| A12 exhaustion | C: bounded attempts, honest incomplete result, retained evidence |
| A13 retry/restart after write | C: actual file reconciliation and hash mismatch tests; completed write not replayed |
| A14 commands/checks | C: actual process cwd/output/exit and failing command; P: runtime qualification host checks |
| A15–A16 planning/tasks | C/P: appropriate assignments; real two-task dependent feature, component six-task and parallel-backend coverage |
| A17 approval/revision | C: rejected/stale approvals block execution; assignment/fallback policy binding retained |
| A18 repair | C: bounded repair and dependent gating; real free runtimes completed the qualification feature without needing repair |
| A19 OpenCode/Kilo | C/P: actual read/edit/check/denial/cancellation and normalized adapter events |
| A20 board progress/reconnect | C/K: real graph/dependency/state/idempotency; Qt loading/readiness contracts; installed embedded interaction unverified |
| A21 cancel/restart/project changes | C/Qt: no stale cross-chat callbacks, dead-owner recovery, active native cancellation |
| A22 integrate/apply/inspect | C: existing apply and fresh-read regressions; P feature integration verified, real feature not applied |
| A23 scope/source instructions | C and native negative P checks: project, secret, write-scope and citation constraints |
| A24 installed regression journey | NOT RUN under the user's CLI-only instruction |

## Failures retained and corrected

- Initial actual local requests returned advice, invented that an absent file already existed, or reported inability. The host did not accept these as successful actions. The local model remains a probationary first candidate, with automatic eligible fallback; it is not qualified as a universal coding model.
- A real free-runtime edit initially encoded literal backslash-n instead of preserving CRLF. Supplying raw source/newline metadata and removing the CLI's extra JSON message-encoding layer resolved the qualification request.
- A four-character text quote initially hit the older code-analysis citation floor. Literal short data-file values now have exact line/hash checks. Native reasoning transport before `</think>` initially spoiled JSON; the adapter now isolates the final segment, then the host still validates it strictly.
- Earlier full suites stopped on stale route-label assertions (487/571/575 passing before their failures). These runs are retained and are not called passing suites. Headless tests also exposed a real missing activity-timer attribute; it was corrected, along with late worker cleanup and a dropped board connection callback.
- The last active five-task guard, missing planner fallback, and deterministic Build shortcut journal bypass were found during qualification and corrected. Explicit Build and local shortcut tests now assert the shared lifecycle.

## Practical limits

- JSON and model confidence cannot establish semantic correctness. Literal citations, exact artifacts and configured behavior checks provide separate evidence; explanations retain a review judgement.
- Local/free availability and model quality vary. OpenCode/Kilo free-route pricing is checked at invocation, not promised permanently. Kilo Auto's underlying model may remain unobservable; receipts distinguish configured and reported identity. Unknown opaque CLI costs are not zero. Metered API routes require permission, known prices and reservations.
- The optional Kandev-owned desktop backend retains its existing single-request execution path. The normal multi-task desktop workflow is owned and verified internally and mirrored to Kandev. Default desktop scheduling is sequential; independent parallel worktrees remain an existing separate backend. The default task ceiling is 30; `parse_plan(max_tasks=...)` exposes a parser bound, with no new UI size control.
- Codex, OpenCode and Kilo had real qualification here. Existing Claude/Copilot adapters have component coverage; this report does not claim a fresh live account qualification for them.
- No interactive UI testing or desktop control was performed after the user's CLI-only instruction. Headless rendering/startup checks cannot prove installed-screen legibility, animation or the full interactive board journey.

## Official interfaces checked during adapter qualification

- [Kilo CLI](https://kilo.ai/docs/code-with-ai/platforms/cli-reference), [Kilo Auto](https://kilo.ai/docs/code-with-ai/agents/auto-model), [limits and costs](https://kilo.ai/docs/getting-started/rate-limits-and-costs).
- [OpenCode model catalog](https://opencode.ai/v2/docs/console/models/). Installed native metadata supplies the dispatch-time model/price facts.
- [Kandev automation](https://kandev.ai/docs/automation-and-mcp). Actual isolated 0.97.0 responses establish the qualified adapter contract.

## Final Windows package

- Version: **1.4.0**. Schema: **16**.
- Executable: `dist/MoliCoder/MoliCoder.exe`.
- SHA-256: `37d3d82c577c0a221160d4389888d9882dee64594bda6ae12129614b10b6097b`.
- Six package checks passed: headless startup/storage migration, headless WebEngine loopback renderer, shared directory request, empty-file action, folder action, and a real inference content action. Exact empty/content bytes and actual directory existence were independently checked.
- The final packaged content request (`d196db17-1cc7-4e4b-903b-3650967def98`) rejected Ollama's unsupported inability, continued automatically through `opencode/big-pickle`, and created exact `b"yeay"`.
- Evidence: `package-1.4.0-checks.json`, `package-1.4.0-action.json`. Runtime tests used an isolated explicit fixture profile.
- Installation: **completed** through `scripts/install.ps1` after the user closed the application. The previous install was preserved as `MoliCoder.previous-20261016-222206-cb5e6ec383a34ec89ee146be922c50c5`. No user process was closed or desktop controlled by the agent.
- Installed executable SHA-256 matches the tested package. Installed headless startup/storage migration, WebEngine loopback rendering, and shared CLI directory listing all passed (exit 0). The listing used a separate fixture database and verified a known file. Evidence: `installed-1.4.0-checks.json`. The installed app was not launched interactively.

## Final regression identity

- Frozen Python source SHA-256 aggregate: `339315369840cac782bbba8ffd42399278650e2ab515deddefb6924175a16524`.
- Frozen source/schema/test/config snapshot: `7bbb66882ea446aa3631971842f45f2e7b499ee0b38cca1f3b2c109a2769fc57`.
- Per-shard results: 165 + 224 + 192 + 140 passed; skips 3 + 0 + 0 + 4. All four process exits were 0.
- Trailing-blank normalization after the run: `orchestration/__init__.py`, `contracts.py`, `journal.py`; ASTs unchanged. No behavior or package artifact changed.
