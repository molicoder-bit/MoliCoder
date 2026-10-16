# Chat project identity and local folder queries

**Status: complete — version 1.3.4 installed and verified on 2026-10-16.**

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
