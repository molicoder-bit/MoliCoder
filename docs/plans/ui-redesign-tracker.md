# UI redesign tracker

Tasks are checked only after verification. Existing execution and authorization rules remain authoritative.

## UI01 — Foundation
- [x] UI01-01 Inventory features and acceptance criteria.
- [x] UI01-02 Shared visual system and supplied logo.
- [x] UI01-03 Horizontal shell and Chat/Build navigation.
- [x] UI01-04 Bottom drawer and compact layout.
## UI02 — Chat
- [x] UI02-01 Transcript, code, reasoning, attribution.
- [x] UI02-02 Composer, attachments, model and generation controls.
- [x] UI02-03 History, export and conversation actions.
- [x] UI02-04 Project answers, knowledge, folder confirmation and Build handoff.
## UI03 — Build
- [x] UI03-01 Group execution controls and readiness.
- [x] UI03-02 Stages, task selection and live activity.
- [x] UI03-03 Changes, validation and task evidence.
- [x] UI03-04 Outcome-specific recovery and apply actions.
## UI04 — Usage and settings
- [x] UI04-01 Persistent usage strip.
- [x] UI04-02 Detailed budgets and pacing.
- [x] UI04-03 Connections, agents, APIs and trust.
- [x] UI04-04 Environment, installs, repair and hardware.
## UI05 — Kandev lifecycle
- [x] UI05-01 Project connection profiles and discovery.
- [x] UI05-02 Owned service startup and readiness.
- [x] UI05-03 Safe Stop, reconnect and recovery.
- [x] UI05-04 Installer and native resource setup flow.
## UI06 — Embedded board
- [x] UI06-01 Lazy embedded browser and fallback.
- [x] UI06-02 Loading and recovery screens.
- [x] UI06-03 Existing reconciliation and acceptance controls.
- [x] UI06-04 Package browser helpers and resources.
## UI07 — Delivery
- [x] UI07-01 Keyboard, resizing and scaling checks.
- [x] UI07-02 Behavioral regression suite.
- [x] UI07-03 Live Kandev lifecycle and embedded board.
- [x] UI07-04 Screenshots, documentation and package.

## Verification evidence � 2026-10-15

- Full Windows suite: 454 passed, 6 opt-in live inference tests skipped. No provider quota was spent.
- Focused workspace tests cover separate drafts, compact Build access, visible context, project-specific connection profiles, process identity mismatch and active-agent Stop refusal.
- Native Windows screenshots at 150% scaling: `docs/images/workspace-chat.png`, `workspace-build.png`, `workspace-kandev.png`; compact 1020 � 680 logical window checked with vertical scrolling. Chat/Build screenshots contain fixture conversation/run records.
- Real pinned Kandev service: start, health/version, safe idle stop, restart with the same data, and embedded DOM/rendering verified. No agent launched. Local managed service remains available on loopback port 38429.
- Windows packaged UI/doctor and WebEngine local-page/JavaScript smoke checks passed. Final revision packaging and cross-platform CI are recorded below when complete.
- Existing environment configuration remains a fallback; saved per-project profiles take precedence. Service data and process ownership receipts are separate from agent-task authorization.
- Project selection is locked for the duration of the existing single active Build; Chat/Build navigation remains available. This keeps worktree and apply actions bound to the original project.

## Final delivery

- MoliCoder 1.2.0 installed at the existing user executable location; the previous installation was preserved.
- Both final Windows package startup checks passed, including the embedded browser's local-page JavaScript check.
- Live connection discovery verified workspace, agent profiles, repository catalog and workflow steps. A UI regression saves a complete profile using the pinned workflow **snapshot** endpoint. Focused setup/workspace suite: 7 passed.
- Source revision `53d1892`: Linux x64 and macOS Intel CI passed, each with **453 passed, 8 skipped**, plus packaged desktop and embedded-browser smoke checks. [Platform run](https://github.com/molicoder-bit/MoliCoder/actions/runs/38813196483).
- Windows hosted tests and packaged desktop/browser checks passed; artifact archival was in progress at final review. Validation is recorded at [Windows run](https://github.com/molicoder-bit/MoliCoder/actions/runs/38813196729). Local Windows full suite: 454 passed, 6 skipped before the final additional setup regression; final focused UI/package suite: 52 passed, with the added setup regression verified separately.
- Supplied `molicodeLogo.png` is copied unchanged into package assets and used in the workspace header and application window icon. SHA-256 comparison with Downloads matched.
