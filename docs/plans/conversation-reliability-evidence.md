# Conversation reliability follow-up

Date: 2026-10-16. Target release: **1.4.1**. Baseline: `690b31d` / installed 1.4.0. **1.4.1 is now installed and verified through the CLI.**

## Why the previous checks were insufficient

The reported exchange was real: “what project are we on” and “uhh?” both displayed `project`. The saved receipt showed a General chat with no selected project. The evaluator called that missing information, and the coordinator displayed its raw field label as the assistant's reply. The earlier release tests covered isolated requests and host artifacts, not a sustained developer conversation or the usefulness of every visible reply.

This follow-up uses real inference through `MainWindow._send_message` and `RequestWorker`, launched from the CLI with Qt offscreen. It checks the visible text and relevant behavior, as well as request status. No desktop control or interactive application launch is used.

## Changes

| Failure observed in conversation | Change |
| --- | --- |
| A selected project was followed by a redundant destination question | The project root explicitly supplies the default scope for relative paths. One bounded correction supplies already-resolved facts before fallback; no extra user question is needed. |
| A raw missing-information label became the reply | Clarifications go through a natural-language reply contract; bare missing-information labels are rejected. Known General/project identity is answered from app records. |
| The model treated known General-chat state as missing context | Every model request includes an explicit authoritative application-state sentence, alongside literal metadata. |
| A generic tool contract confused ordinary dialogue and examples | Conversation/clarification use a smaller read-only grammar, with the complete reply inside `message` and an explicit JSON format example. File actions retain the guarded tool loop. |
| A task JSON envelope and correction diagnostics displaced the actual question | Evaluation and execution retain the current user's plain message as the final user turn; corrections repeat that current request. Full conversation history is preserved. |
| Follow-ups lost the file/function being discussed | Retrieval uses recent dialogue as search data and re-reads actual files. A bounded root README excerpt supplies project-purpose context. |
| The evaluator invented README contents as an acceptance oracle | Observation criteria must name actual tools; conversation has no artifact criteria. Inconsistent evaluations get a bounded correction. |
| A missing evidence reference caused premature fallback | One explicit verification repair identifies the failed check. The oracle is unchanged; unresolved failures still fall back. |
| Valid source quotes concealed an irrelevant or unanswered local reply | Local code answers receive a separate structured relevance/support review. Review failure triggers eligible fallback. This is a model judgement, not a proof of semantic correctness. |
| Code-answer failures disabled the model for General chat | Semantic cooldowns are scoped to task family. Outages/auth/quota remain route-wide. Compatible readers interpret older semantic cooldowns using their recorded intent. |
| A promised example/list stopped at its introduction | An obviously unfinished introduction receives one bounded correction; repeated incompleteness cannot count as a completed answer. |
| Directory answers were forced to cite an unrelated README, and source footers duplicated | Source citations are required for source-read/search criteria; the host renders verified source footers once. |

## Repeatable real conversation gate

`tests/test_live_conversation.py` is an opt-in real-provider regression. It creates its own project/profile, drives the actual desktop request worker headlessly, preserves messages across turns and reopens a General chat after project work. It also seeds the earlier broken `project` reply and checks recovery.

The gate covers:

1. Project identity with no selected project, then a confused reaction.
2. Remembering an idea name and answering a follow-up from the same chat.
3. Supplying a real Python example without creating files.
4. Describing the selected project from its actual README.
5. Locating archived-entry filtering in source.
6. Explaining title trimming, missing flags, missing keys and the failure a test should expect.
7. Reopening the earlier General chat and recalling its idea name.
8. Responding usefully after a historical one-word `project` reply.

Code assertions exclude the rendered Sources footer, so merely quoting `.strip()` cannot pass a prose explanation that fails to discuss trimming. File contents and the project directory are checked for unintended changes. A useful clarification may be `awaiting_input`; that state is distinct from an error or a raw field label.

Run from PowerShell with an available local model:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
$env:MOLICODER_LIVE_CONVERSATION='1'
$env:MOLICODER_LIVE_FALLBACK_PROVIDER='opencode,kilo' # optional, explicit test-profile trust
.venv\Scripts\python.exe -m pytest -q tests/test_live_conversation.py
```

The fallback provider is authorized only in the isolated test profile. Ordinary unit-suite runs skip this live gate; its enabled result is recorded separately. Real transcripts, receipts, failed attempts and package reports are retained under `artifacts/conversation-qualification-20261016/` and excluded from Git.

## Retained failures

- Baseline/local-only dialogue failed the General question and project-purpose question, and returned incomplete introductory replies.
- Intermediate runs exposed missing code context, repeated answers to earlier questions, unfulfilled evidence criteria, and a source-quoted reply that still did not answer a missing-key question.
- An early live regression reached the reopened General chat and failed because a code-answer cooldown had disabled the route globally. The task-family fix addresses that observed failure.
- These attempts remain in the artifact directory. They are not counted as passing conversations. Final passing evidence is recorded below. The enabled gate was rerun in a persistent workspace fixture so parallel pytest cleanup cannot remove its profile.

## Limits

Local/free model quality and availability vary. Exact files, process outcomes and literal source quotations are host-checked. A relevance/support review remains fallible; it does not establish that every natural-language answer is correct. Free or stronger fallback must already be authorized for the project. No installed interactive UI journey or universal 100% model-quality claim is made.

## Reported chat replay

A read-only copy of the reported chat's **38 saved turns** was supplied to the shared coordinator using the real local model and a separate fixture database. External providers were disabled. The user's actual database was not modified.

| Input | Actual response | Outcome |
| --- | --- | --- |
| what project are we on | This is a General chat with no associated project. Choose a Project in the header to open its chats, or start a new chat there. | completed, no inference required |
| uhh? | This is a General chat with no associated project. Choose a Project in the header to open its chats, or start a new chat there. | completed, real Ollama |
| okay, thanks | You're welcome! How can I assist you further? | completed, real Ollama |

Evidence: `reported-chat-replay.json` and `reported-chat-replay.log`. Earlier private history stays in ignored local fixture data and is not copied into this document.

## Final results

| Gate | Result |
| --- | --- |
| Real sustained conversation through `MainWindow` / `RequestWorker` | **PASS**, 13 turns, enabled `tests/test_live_conversation.py`, 205.09s total. [Read the full visible conversation](conversation-reliability-transcript.md). |
| Real reported-chat replay with 38 prior turns | **PASS**, three further exchanges, local-only, original database read-only. |
| Default full regression | **735 passed, 8 skipped**, zero failures/errors, `release-qualified-regression.json`; source/test snapshots matched during that run. |
| Focused regression after the final native-timeout adjustment | **104 passed**, `release-focused.log`. |
| Final packaged executable | **7 passed**: startup/storage, embedded renderer, General identity, real greeting, real code analysis, exact file creation, exact file edit. `release-package-checks.json`. |
| Installed executable | **4 passed**: headless startup, embedded renderer, General identity and real greeting. Installed SHA-256 matches the qualified package. `installed-1.4.1-checks.json`. |

The final production change after the full suite extended native provider turns from 45 to 90 seconds within the existing 240-second overall request deadline. The focused regression, real conversation and final package checks qualify that path. The separately skipped live test's assertion was also adjusted to accept correctly explained English paraphrases, then the enabled live gate ran and passed. The eight default skips are opt-in inference tests; the conversation gate was explicitly enabled and passed separately.

Source-answer timing remains variable: one free-runtime response took 105.91 seconds including orchestration turns. The passing transcript does not claim instant responses or universal model correctness. Replies came from actual app records/Ollama/OpenCode; Kilo remained eligible but was not the winning route in this conversation.

The package created exact `b"yeay"`, then edited it to exact `b"ready"` in an isolated project. General identity and code-error explanations were independently asserted. Fixture project files remained unchanged during the read-only conversation.

### Installed release

- Version: **1.4.1**, schema **16**; no new schema migration in this patch.
- SHA-256: `d072e6fcff441a0e746a9eeda8669c52868f733927db452e55cbce375a159a71`.
- Installed through `scripts/install.ps1` after confirming the app was closed. Previous 1.4.0 installation preserved as `MoliCoder.previous-20261016-232117-632eefece6f2418192364ee58e837971`.
- CLI/headless verification only. The app was not opened interactively, and no desktop control was used.
