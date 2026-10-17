# Provider setup simplification

**1.4.2 is installed and verified through the CLI.** Baseline: `13d2333` / installed 1.4.1.

## Changes

- Replaced five repeated provider cards with one compact status list and one detail editor. No per-provider model Save buttons or trust checkboxes remain on the settings page. Empty overrides use provider defaults; changes save automatically and preserve other providers, strategy and project access.
- Added one project access dialog. A project's first opening offers the choice once; Later remembers the deferral without granting access. Existing configured projects retain their choices. Project access can be changed or revoked from Settings.
- Added a worker-to-UI permission pause when a request requires an installed external provider. Approval updates only the captured project, then continues the existing coordinator with its original history, objective and completed tool evidence. Cancellation stops the request. A stale worker or switched project cannot grant access. A scope denial never triggers this prompt; local-only mode and disabled automatic fallback remain local.
- Build and Build recovery use the same project chooser. All selected Build providers must be approved together; allowing a Build provider does not turn on disabled chat fallback.
- Moved API account, key, offering and test controls into a separate advanced dialog with a scrollable body and a reachable single Save action. They remain available without a selected project.
- Added a direct Download and install action for supported provider releases. It preselects the tool and reuses the existing exact source/hash/destination approval and verified installer. Verified completion updates connection and routing facts. Other providers show their official setup/sign-in instructions. This does not automate account creation or sign-in.
- Runtime discovery now includes receipt-backed managed executables and Codex Desktop's bundled executable when they are absent from PATH.

## CLI and headless verification

All work uses CLI commands, offscreen Qt, isolated fixture profiles and real native inference. No desktop automation or interactive application launch is used. Layout checks explicitly load the Windows Segoe UI font because the offscreen platform otherwise produces missing glyph boxes.

Headless layout artifacts, at 900 × 640 and the default 17 px text size:

- `artifacts/provider-setup/settings-light.png`: every provider and normal settings action fits without scrolling.
- `artifacts/provider-setup/settings-install.png`: missing OpenCode exposes the verified download action.
- `artifacts/provider-setup/project-access.png`: one project selection dialog; its body can scroll at larger text sizes.

The deterministic gates exercise automatic saving across providers, project switching, deferred first-open setup, access revocation, cancellation, stale owners, the real RequestWorker permission wait/resume, preservation of completed writes during fallback, local-only enforcement, Build selection, managed discovery and the exact install plan. Provider responses are scripted only in these regression tests.

Live qualification uses `tests/test_live_provider_setup.py` (two real OpenCode turns after granting access once) and `tests/test_live_conversation.py` (13 normal developer turns using real local/native models). The former disables local offerings only in its test profile to exercise the actual permission path; neither test invents assistant replies. Raw logs and transcripts are under `artifacts/provider-setup/`.

## Failure caught by real conversation

The first real 13-turn run stopped on turn 9. The local model incorrectly said that `note.get("archived", False)` had no missing-key behavior; its own relevance review accepted that claim. The live test rejected the answer. The preceding 750-test regression and package checks had passed, so they were insufficient to establish conversational correctness.

The fix uses the existing task-family qualification record when routing source explanations, even when the evaluator calls the question low complexity. When a capable external alternative is available, unqualified local self-review cannot approve that explanation: a qualified local handles it or a provider is selected, with the same on-demand access dialog if needed. There is no phrase-specific reply or dictionary-key handler. Conversation, literal folder tools and explicit local-only operation retain their local routes. With no qualified or permitted alternative, model-only semantic review remains a judgement, not a proof.

Additional regression cases check qualified and unqualified routes, permission before source reasoning, and that a falsely self-approved local answer cannot be published in this configuration. The final real conversation is rerun after this change. Initial failing transcripts and preceding package evidence are retained separately from final results.

## Final results

| Gate | Result |
| --- | --- |
| Default regression coverage | **754 passed, 9 skipped**, after the focused expectation correction described below. |
| Real project permission and conversation | **2 OpenCode turns passed**, with exactly one access prompt; original source files unchanged. This gate preceded the source-qualification policy adjustment; the native path remains covered by the final conversation. |
| Final real developer conversation | **13/13 completed**, in 214.53 seconds: 1 app-record answer, 6 Ollama answers, 6 OpenCode answers. Missing flags correctly default to False; missing active titles raise KeyError; General-chat memory and recovery from an old bad reply passed. |
| Final packaged executable | **7/7 passed**: startup/storage, WebEngine, General identity, real greeting, real code semantics, exact `yeay` creation and exact `ready` edit. |
| Installed executable | **4/4 passed**: startup/storage, WebEngine, General identity and a real greeting. Its SHA-256 matches the tested package. |

The final complete frozen-source run reported 753 passes, 9 skips and one obsolete assertion: it expected three attempts before a desktop Codex handoff. The new policy correctly performs local evaluation and then hands source reasoning to Codex, skipping the unqualified second local model. The assertion now checks the exact two providers and the evaluated state. All **24 tests in that file passed on rerun**. Production source did not change between the full run, the focused rerun, real dialogue, packaging and installation. `final/reconciled-regression.json` reconciles the individual JUnit cases; the original failed run is preserved, not relabeled.

Final production source digest: `4eeb6aa820c6b85db3b067c5dabd7c20b2568ba98204dabec596f59a182afc71`.

Tested and installed executable SHA-256: `789af8f51da365ecd3bc766f967d1de338a94d69ebbdaa2640401bbd8a1bf230`.

Installed through `scripts/install.ps1` while the installed app was closed. The previous installation is retained at `C:\Users\luism\AppData\Local\Programs\MoliCoder.previous-20261016-235413-bd76e0dbb399464684ee6b6c0898f09a`. The app was not opened interactively. Existing user project permissions and chats were not changed by the qualification profiles.

## Reproduce

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest -q tests/test_provider_setup.py tests/test_request_conversation.py tests/test_inference_discovery.py tests/test_repository_analysis.py

$env:MOLICODER_LIVE_PROVIDER_SETUP='1'
.venv\Scripts\python.exe -m pytest -q tests/test_live_provider_setup.py

$env:MOLICODER_LIVE_CONVERSATION='1'
$env:MOLICODER_LIVE_FALLBACK_PROVIDER='opencode,kilo'
.venv\Scripts\python.exe -m pytest -q tests/test_live_conversation.py
```

The live gates require installed working providers and use isolated profiles. Raw final logs, transcripts, package results and installation checks are in `artifacts/provider-setup/final/`. These checks establish this tested workflow; they do not prove that every model will answer every future request correctly.
