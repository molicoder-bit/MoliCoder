# Project chat organization

**Status: complete — delivered in 1.3.1 on 2026-10-15.**

Each project owns many conversations. A conversation belongs to at most one project; unassigned conversations appear under General chats. Existing associations and message history are preserved.

## Implementation

- [x] PC01 Create project-linked conversations atomically using the existing one-to-many database relationship.
- [x] PC02 Query project conversations separately from General chats, with deterministic recent-first ordering.
- [x] PC03 Scope History to the selected project and label that scope and chat count visibly.
- [x] PC04 Offer General chats alongside projects in History and the header selector.
- [x] PC05 Restore the last selected chat when switching projects; create a chat only for an empty scope.
- [x] PC06 Preserve each conversation's draft and attachments across project switches.
- [x] PC07 Retire active Chat generation safely when switching conversations; preserve active Build ownership.
- [x] PC08 Keep New chat, rename, delete, restart, and direct history selection within the correct project scope.
- [x] PC09 Verify multi-project storage/UI journeys and existing regressions; document behavior in README.
- [x] PC10 Package and smoke-check the update, install when the app is closed, and record delivery evidence.

## Verification and delivery

Storage and UI acceptance: **5 passed** in `tests/test_project_chats.py`. Covers one-to-many ownership, atomic creation failure, separate General/project histories, draft and attachment restoration, remembered older-chat selection after restart, scoped deletion, archived project preservation, direct chat reopening, late-stream isolation, and active Build ownership. Existing focused scope/journey/storage suite: **29 passed**.

Native Windows screenshots in Matrix and Light were rendered with demonstration records and visually checked: [Matrix](../images/usability/project-chats-matrix.png) · [Light](../images/usability/project-chats-light.png). Windows full regression: **499 passed, 6 skipped** in 206.95 seconds. Skipped tests require opt-in live model or paid subscription usage. Final project-chat and existing new-chat checks: **10 passed** after the General-chat project-action guards. Counts overlap and must not be summed. No schema migration was required: `conversation_context` already enforces one project per linked chat and supports many chats per project.

- Source revision: `013b94e`, pushed to `main`; version **1.3.1**.
- Both final Windows package checks passed: `--package-smoke-check` (desktop, legacy data, migration/recovery) and `--package-board-smoke-check` (embedded browser).
- Installed at `C:\Users\luism\AppData\Local\Programs\MoliCoder\MoliCoder.exe` while the app was closed. Desktop shortcut refreshed; previous installation preserved at `C:\Users\luism\AppData\Local\Programs\MoliCoder.previous-20261015-212618-a03179953fa24a7289290f401fce5f76`.
- Built and installed executable SHA-256 match: `C813E646CEE488841C0B117645C4C634D72E6F7BBF97C40B10841D2B33704B28`.
- Results above are local Windows verification for 1.3.1. The preceding 1.3.0 supported-platform CI results remain recorded in [workspace usability verification](workspace-usability-verification.md).
