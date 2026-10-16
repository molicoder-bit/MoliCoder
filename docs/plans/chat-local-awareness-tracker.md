# Chat project identity and local folder queries

**Status: complete — version 1.3.5 installed and verified on 2026-10-16.**

The completed 1.3.5 regression follow-up below corrects a later-reported phrasing gap; the original 1.3.4 results remain historical.

- [x] LA01 Recognize conversational questions about the current project's identity and answer from the conversation's saved project association.
- [x] LA02 Include basic project name/path/type metadata in model requests and their context estimates/receipts, without reading project files automatically.
- [x] LA03 Recognize explicit folder-listing questions, including the reported C:/Dev wording, quoted paths, and project-relative folders.
- [x] LA04 Enumerate only immediate names/types in a background worker; bound the result and show missing/access-denied/cancelled outcomes truthfully.
- [x] LA05 Integrate local read progress, cancellation, provenance and history without inference or a Build run.
- [x] LA06 Verify project switching, no-project clarification, literal filenames, limits, errors, worker isolation, and model request metadata.
- [x] LA07 Update usage documentation, package/smoke-check the release, and record delivery/install status accurately.

## Evidence

- Focused local-awareness, continuity, controller and intent suites: **37 passed**. Tests cover both reported prompts, project association/metadata/estimates, same-chat context, general/relative path clarification, bounded read-only enumeration, nested/file-content exclusion, literal Markdown filenames, missing/file/permission errors, background completion receipts, and late callback isolation.
- Bounded local `molicoder:latest` check used synthetic project metadata and an earlier incorrect assistant disclaimer. It correctly identified **Cedar Compass** and **C:/Dev/CedarCompass** from the supplied metadata (333 input / 24 output tokens). No user conversation or file contents were sent in that check.
- Read actions use the existing `AuthorizationPolicy` READ path. Folder enumeration runs on a worker and uses no shell, model call or Build. Listing receipts record the target, checked time, displayed count, limit state and outcome.
- The reported conversation was associated with a saved project. Previous normal Chat requests omitted that metadata; the current basic-metadata packet and local project-identity route address both direct questions and conversational follow-ups.
- Final native Windows local-awareness suite: **16 passed**. Full Windows regression: **528 passed, 6 skipped** in 219.38 seconds. Skipped tests require opt-in live inference; focused/native counts overlap the full suite and must not be summed.
- Both 1.3.4 package checks passed: desktop/legacy-data/migration recovery and embedded-browser startup. Built executable SHA-256: `E63E496F1A3C14B0D9CEE9F39941FE289176301670BAC4D37D9AD820A06031AE`.
- Implementation source `9f2fca9` is pushed to `main`; no user history was rewritten or repaired in place.
- After the user confirmed closure, installed **1.3.4** at `C:\Users\luism\AppData\Local\Programs\MoliCoder\MoliCoder.exe` with no app process running. The installed executable matches the verified SHA-256 above and passed its desktop/migration smoke check. The desktop shortcut was refreshed; the previous installation is retained at `C:\Users\luism\AppData\Local\Programs\MoliCoder.previous-20261016-041101-b9176926e9464f699d5b3f17e888020a`. Verification above is local Windows evidence, not a claim of a newly completed hosted platform matrix.

## Conversational listing regression — 1.3.5

- [x] LA08 Reproduce the saved wording, "Ok that's good, can you tell me the contents of the folder c:/Dev?", as a parser test and typed-chat integration test that fails if any model worker is created.
- [x] LA09 Parse conversational acknowledgments, polite query forms, contents/files noun phrases, and folder/directory qualifiers; retain negative cases for explanations, negation, and unrelated lists.
- [x] LA10 Add the real typed-chat journey to the mandatory packaged desktop smoke check, including saved project association, existing history, real background enumeration, exact target and persisted No AI receipt.
- [x] LA11 Run focused/native and full regressions; verify the frozen executable with the expanded journey.
- [x] LA12 Package, install if closed, and push the verified follow-up with recorded delivery status.

Evidence: before the fix, the new tests reproduced **6 failures**, including the exact user wording falling through to model-worker construction. After normalization changes, **44 focused tests passed**, including the expanded smoke journey against source, and **23 native Windows tests passed**. Full regression: **535 passed, 6 skipped** in 199.21 seconds; focused counts overlap. After switching the smoke driver to the actual Send button, its final focused suite passed **23 tests** and its source command passed again. The original 1.3.4 package smoke checked startup/migration; it did not exercise a typed folder-listing request.

Both final frozen 1.3.5 package checks passed. The desktop check now drives the composer/Send button in a saved fixture chat and verifies project identity, real folder enumeration, exact target and persisted No AI receipt. The installed executable passed this same expanded desktop check. Installation happened with the app closed; shortcut refreshed and prior installation retained at `C:\Users\luism\AppData\Local\Programs\MoliCoder.previous-20261016-042231-286b96b8a3d948009a6b9a76f99c41a7`. Built and installed SHA-256 match: `B39D952D919EDC818F3767B5B4B270A3D7AA6BD057B04323D08C9B8ED628E35D`. These are local Windows results.

Final 1.3.5 source revision: `5e770f8`, pushed to `main`. All follow-up tasks are checked against the completed evidence above.
