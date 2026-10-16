# Chat continuity and transcript selection

**Status: complete — version 1.3.2 installed on 2026-10-16.**

- [x] CS01 Inspect saved turn receipts and the Chat → policy → Ollama request path for the reported conversation.
- [x] CS02 Give every Chat model explicit instructions to use the supplied same-conversation history and correct misleading earlier memory claims.
- [x] CS03 Build history, request receipts, and context estimates from the same complete message snapshot, including the system instruction; block overflow without silently trimming history.
- [x] CS04 Verify restored-chat history reaches the adapter/transport and does not include another chat's messages.
- [x] CS05 Support dragging and Shift-clicking text selection across message bodies, with forward and reverse ranges.
- [x] CS06 Support transcript Select all, Copy selection, and Copy chat without capturing composer shortcuts.
- [x] CS07 Keep cross-message selection usable during scrolling, streaming, theme changes, and history navigation.
- [x] CS08 Verify behavior in both themes and run relevant regressions; record a bounded local-model continuity check.
- [x] CS09 Update README, package/smoke-check the release, install if the app is closed, and push with completed evidence.

## Evidence

The reported saved chat contains earlier user/assistant turns, with prompt counts increasing through the conversation. Its final reply's receipt is checked against saved turn IDs. The configured local model template supports the messages list. These observations identify a misleading model response; they do not prove that every response will recall history correctly.

## Focused verification

- **10 passed** with offscreen Qt, and **10 passed** with native Windows Qt. Tests cover request order, restored history through the policy/adapter, exact receipt IDs, HTTP message-array preservation, system-inclusive overflow blocking, forward/reverse drag, Shift-click, clipboard contents, composer shortcuts, scrolling, theme/streaming stability, and selection clearing on chat navigation.
- Bounded local `molicoder:latest` check used synthetic history including two misleading memory disclaimers. It correctly recalled **Cedar Compass** and **purple** from the first user message (247 input / 17 output tokens). No cloud request or user conversation replay was used.
- The reported chat's last saved receipt includes all **15** prior user/assistant turns, matching their saved IDs.
- Native minimum-window controls checked at 1020 × 680 logical pixels; larger fixture captures show selection across four cards: [Matrix](../images/usability/chat-selection-matrix.png) · [Light](../images/usability/chat-selection-light.png). These screenshots contain demonstration text, not live provider output.

Full Windows regression: **509 passed, 6 skipped** in 202.35 seconds. The six skipped checks require opt-in local/paid live inference; the separate bounded local continuity check above passed. Both packaged checks passed: `--package-smoke-check` (desktop, legacy data, migration/recovery) and `--package-board-smoke-check` (embedded browser). Counts from focused runs overlap the full suite and must not be summed.

Source `1b94413` is pushed to `main`. Release **1.3.2** is installed at `C:\Users\luism\AppData\Local\Programs\MoliCoder\MoliCoder.exe`. Built and installed executable SHA-256 match: `DFC6EC7007CE7532FBD5AC77DC3DDB5F6008B8B0C135C5197A370999E60DD4B7`.

The installed executable also passed its desktop/migration smoke check after replacement.

The user approved installation after saving and closing the app. Windows still showed a responsive window; it accepted a normal close request and exited before installation. The desktop shortcut was refreshed. The previous installation remains at `C:\Users\luism\AppData\Local\Programs\MoliCoder.previous-20261016-021749-4b881baefb04402eb707a3bf976581d5`. These are local Windows results, not a new claim of completed hosted platform CI.
