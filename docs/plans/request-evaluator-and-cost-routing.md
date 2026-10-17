# Request evaluation and economical model selection

Release: **1.4.4**. Baseline: `d31746d` / 1.4.3. Date: 2026-10-17.

## Reported failure

The saved receipt for request `a2362347-5184-49cf-a27f-27f7150a8df8` confirms the problem. The local evaluator correctly described reading README.md as low complexity. A separate routing branch nevertheless treated every `read_file`/`search_code` investigation as source reasoning, excluded the local model and chose Codex. OpenCode/Kilo were not authorized for that project, and the router did not offer their access before using the already authorized subscription. Request routing also reused the Build planner default: the first normal advertised Codex model, at medium effort. It did not compare individual Codex models for this request.

## Evaluation pipeline

There is no new README-specific handler or request-phrase-to-provider table.

1. The existing shared request evaluator returns strict, versioned JSON. Version 1.1 now requires `work_type`: direct answer, extraction, analysis, planning, implementation or operation. It also supplies required capabilities, complexity, objective, acceptance checks and unresolved decisions. Reporting existing text differs from reasoning about its behavior even when both use the same read tool.
2. `WorkRequirements` converts that evaluation into a work profile, including reasoning demand and current context size. `assess` compares that profile against each candidate's context, capabilities and qualification/runtime evidence. A cheap candidate that cannot satisfy those requirements is excluded.
3. Suitable candidates are ordered by local, verified/guarded free, subscription, metered and unknown-cost classes. Registered API prices can produce actual estimates. Opaque subscription costs remain unknown. Advertised descriptions such as “affordable” are relative selection hints within a subscription, not invented dollar prices or quota multipliers.
4. Ordinary request selection enumerates advertised normal Codex models independently of the Build planner default. An explicit project model override remains a constraint. Effort is chosen from advertised levels according to this request's reasoning demand. The user's excluded extreme model families remain excluded.
5. A suitable free provider requiring project consent stays visible in the ranking. The app asks for that access before silently proceeding to a subscription candidate. Consent is project-scoped and retained. Declining stops the request; CLI callers without an interactive permission callback receive a clear blocked result. No project files are sent to an untrusted provider.
6. Failure triggers a new suitability/cost ranking, excluding the failed route and observing cooldowns and current context. A second suitable free provider precedes subscription escalation. The shared deadline remains in force, with at most five model-route attempts. Completed tool effects and approval boundaries are preserved.

Native pinned free profiles verify zero prices before inference. Other configured native models advertised as free are also rechecked before inference; changed or unknown prices stop that route. Selecting a different paid model in OpenCode/Kilo does not make it free merely because of its provider name.

Existing typed local retrieval/actions continue to record a no-model result when they already have an exact host operation. General requests still enter the evaluator. Development requests continue to hand off to the reviewed Build workflow; this change does not replace its detailed planning/execution lifecycle.

## Evidence and limits

Receipts now include `route_evaluation` with policy, work profile, candidate models/efforts, cost classes, suitability estimates, permission requirements and exclusions. The journal retains the evaluation and subsequent fallback rankings.

Suitability scores are policy estimates based on advertised/configured profiles and qualification evidence; they are not benchmark accuracy percentages. Low-price metadata alone does not prove reasoning quality. Host artifact, process and citation checks still determine completion where objectively checkable; semantic correctness remains fallible. Older persisted 1.0 evaluations remain readable during recovery, while new model responses must satisfy the 1.1 contract.

The first real CLI candidate run completed the exact reported README wording locally, ordinary conversation locally, and a missing-flag code question through Kilo after a rejected local answer. The local evaluator initially understated the code reasoning need; that is retained as evidence, and the explicit `work_type` field was added before final qualification. No subscription runtime was permitted by that live test harness; the actual local/free model responses were not faked.

The typed-evaluator CLI conversation then completed all three turns with subscription runtimes blocked by the harness: README via Kilo, ordinary conversation via Ollama, and code semantics via OpenCode after Kilo failed an acceptance check. The code question was classified as `analysis` / medium, and the unqualified local model only evaluated that turn. The failure and transfer between the two free providers are retained in `qualified/live-requests.json`.

A candidate full regression found one obsolete assertion: it expected a context-overflow failure after a model attempt. The new evaluator rejects insufficient context before inference. The test now requires zero attempts, an explicit context exclusion and no generation call. Additional coverage checks enrichment of legacy model-cache rows and escalation to a newly needed provider without repeating earlier consent choices. An intermediate run interrupted by those changes is marked superseded and is not counted as qualification.

## Final qualification and installation

- Full regression: **806 passed, 10 opt-in tests skipped, zero failures/errors** (816 total). All four shards passed and the source/test/config snapshot stayed unchanged. Source digest: `6cc6d44c879a2bf3affb9fba294d1fb436d597622063cba4c38ac244ba22969b`.
- All **nine final packaged checks passed**: startup/storage, WebEngine, real provider setup button, General identity, greeting, the reported README wording, code semantics, exact file creation and exact editing. The final README request used Ollama as extraction; the code question used OpenCode after local and Kilo failures; creation/editing used Kilo. No subscription runtime was used. The code check took 124.05 seconds, so these results do not imply consistently fast free-provider latency.
- The qualified executable SHA-256 is `f2baef675bbdf57a69ec861c74939a865fd51d1b933a9540b6730705cf48b88d`.
- Installed through `scripts/install.ps1` after MoliCoder was closed. The installed executable matches that hash. All **four installed-copy CLI checks passed**: startup/storage, WebEngine, actual provider setup with a fresh response, and General identity. No interactive app window was opened for verification.
- The previous installation is preserved at `%LOCALAPPDATA%\Programs\MoliCoder.previous-20261017-115555-e458203166ee43d59fc2d66f4cad8ccf`.

Final reports are in `artifacts/request-routing-20261017/verified/`. Earlier candidate failures and superseded runs are retained in the sibling directories. Raw candidate failures and final results remain under `artifacts/request-routing-20261017/`.
