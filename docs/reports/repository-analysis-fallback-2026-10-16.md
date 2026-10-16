# Repository analysis and automatic fallback — 2026-10-16

## Implemented behavior

Desktop Chat and `molicoder-exec ask` use the same repository analysis service. Simple deterministic status/listing/file actions retain their existing local handlers. Project questions receive real source excerpts and bounded read/search tools. They do not receive a shell or write tools.

The service tries up to two installed models with distinct weight digests, then up to two trusted external CLI providers. Each route retains the question, complete current-chat history, and gathered evidence. Evidence is fitted to each context window; history is not silently discarded. Missing capability, explicit inability, bad citations, unsupported claims detected by local review, repeated tool calls, context overflow, and provider errors cause automatic escalation. Exhaustion returns unresolved, not success.

Project Provider settings expose external automatic fallback. It requires existing trust for each external provider and rechecks that trust immediately before dispatch. No new authentication or model download is performed. External analysis uses read-only CLI permissions and its own answer contract, independent of the Build plan schema. Build keeps its existing approval gates.

Replies preserve route attempts, failures, actual requested/reported model identity, source hashes, citations, tool traces, and measured usage. Unknown subscription cost and unreported CLI models remain unknown.

## Live repository questions

These were sent through the public CLI against this actual source checkout, without manually supplying relevant filenames. The final single five-question run used `molicoder:latest` for all answers, plus a local grounding review for each. It made no external inference calls.

| Question | Located implementation | Time | Result |
| --- | --- | --- | --- |
| Where is local versus provider execution selected? | `SequentialRunOrchestrator._task_assignments` | 11.35 s | PASS |
| Where are Chat folder listings implemented? | `directory_queries.list_directory` | 7.52 s | PASS |
| Where is exact plan approval checked? | `plan_approval.require_approved_plan` | 6.59 s | PASS |
| Where does the local executor enforce validation? | `AgentExecutor.run` | 8.53 s | PASS |
| Where are validated changes applied to the checkout? | Desktop `MainWindow._merge_last_build` | 8.53 s | PASS |

The fifth question has both a desktop apply path and a worktree-manager path. The original filename-only benchmark accepted only the worktree-manager file. The final benchmark accepts either implementation, and the desktop result was manually checked against its clean-checkout checks and `git merge --ff-only` invocation. Filename matching alone is not the quality assessment.

Earlier runs failed on retrieval, malformed structured output, invented class names, changed or ambiguous quotations, and irrelevant citations. They are retained under `artifacts/repository-analysis-20261016*`. Fixes added contiguous method context, parsed Python class/function scope, source-bound output choices, literal quote resolution within an exact named function, and a local grounding review. The final test is a small targeted assessment, not proof of arbitrary repository understanding or coding ability.

[Recorded answers, source quotes/hashes, local reviews, and usage](repository-analysis-2026-10-16.json) contain the final evidence. The CLI commands and intermediate reports remain in the ignored artifact directories.

## Automatic provider fallback

A separate live test used an isolated profile with Codex trusted and an unused loopback port for Ollama. The local service preflight failed; the same request automatically ran through real Codex and returned correct runner-selection sources in 10.11 seconds. The actual CLI model was not reported and remains labeled unknown. No running Ollama service was stopped, and no project files were edited.

This live outage test was performed twice while developing the adapter. Each used a real Codex CLI invocation and may consume subscription allowance; internal cost remains unknown. Claude/Copilot command and response handling were tested with fixtures, not live inference.

## Regression coverage

The final focused analysis, desktop worker, chat switching, route identity, policy, provider, and Ollama group passes **69 tests**. It covers inability, transport errors, invalid evidence, invented class names, source changes, credential exclusion, traversal, read-only tools, duplicate weight aliases, disabled offerings, cancellation, source propagation between models, project trust/settings, external identity/cost labeling, and actual desktop-thread reply/receipt persistence. Chat switching checks both worker types and rejects late results and late route updates from the previous project.

Regression coverage was completed in stages: the initial run passed 254 tests and skipped two live tests before stopping at a new adapter fixture failure. The fixture now creates its executable, so it exercises real command construction. The remaining files passed 444 tests and skipped five live tests, with one chat-intent failure. “Explain Python functions” was incorrectly routed to repository analysis; it now stays in ordinary Chat. That test was expanded to both worker types and passes in the final 69-test group. The earlier missing-project warning mismatch was also fixed without removing its visible blocked state.

Final collection contains **707 tests: 700 have passing results across the staged runs, and seven opt-in live tests are skipped**. This is combined regression coverage, not a claim that one uninterrupted final command passed all tests. The separate real Ollama and Codex tests above exercise this feature's live routes. Logs are in `artifacts/repository-analysis-20261016-final/`.

## Limits

- Grounding review is another local model judgment and can be wrong. Literal quote and hash checks establish source authenticity, not complete semantic correctness.
- Indexing is bounded: 4,000 supported UTF-8 files, 12 MB total, eight seconds, and 1 MB per file. Excerpts are at most 160 lines/9,000 characters. Dependency/build output, links, and credential paths are omitted.
- Each local route has four answer/tool turns, 45 seconds per call, and one 30-second grounding review. Each external call has a 90-second ceiling. A stalled local reasoner can still delay escalation within those bounds.
- Native tools and free-form command execution were not added. This change covers repository analysis; general Chat, Build, metered custom API routes, and application installation have separate workflows.
