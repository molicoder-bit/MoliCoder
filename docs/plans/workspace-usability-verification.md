# Workspace usability verification — 1.3.0

Date: 2026-10-15. Demonstration databases/projects and provider fixtures were used. No paid or local model inference was required for UI acceptance.

## Acceptance evidence

| Scenario from the plan | Evidence and observed result |
| --- | --- |
| Launch with neworld registered | `test_new_chat_scope.py`: default startup clears saved project scope; explicitly enabled workspace restoration retains it. |
| New chat from Chat | Header click and platform New shortcut share the action; fixture verifies empty composer, new conversation, preserved old draft. |
| New chat during Build | `test_usability_journeys.py`: original worker is retained; the visible return action opens the same Build. |
| Change model and inspect old answer | `test_receipts_settings_activity.py`: persisted previous-model receipt stays visible while next model remains separate; legacy messages remain un-attributed. |
| Unknown external model | `test_route_identity.py`: CLI default is explicitly not reported; a configured GPT-4 fixture is never inferred from connected Ollama. |
| Hybrid handoff/fallback | `test_route_identity.py`: local attempt, external takeover, and fallback clear stale active identity; saved provider events retain per-call reports. |
| Deterministic answer | Existing controller/UI suite and new message receipts: no inference route is recorded and attribution says No AI model used. |
| Explicit folder outside Git | `test_local_actions.py`, `test_usability_journeys.py`, existing folder UI tests: exact confirmation, recorded outcome, verified directory, no coding worker. |
| Missing folder destination | Local-action fixtures return clarification and never fall through to inference. |
| Outside-worktree plan | `test_plan_scope.py`: outside, traversal, foreign-drive and drive-relative paths rejected; contained absolute paths become relative. Validation happens before workspace allocation. |
| Handoff exception | `test_sequential_orchestrator.py`: active task fails, untouched task blocks, ended run and terminal JSONL event agree; completed work is retained. |
| Restart/crash | `test_work_failure.py`: recovery preserves Kandev and live local ownership, preserves unverifiable ownership, and detects reused process IDs. |
| Silent provider | Activity fixture ages the last genuine event by 45 seconds; waiting copy appears, and animation ticks do not reset that time. |
| Included context | Receipt fixtures verify per-reply conversation-turn IDs and attachment names; project selection does not manufacture repository context. |
| Friendly settings | Navigable categories, direct model chooser, inline custom-provider validation, readiness cards, explicit connection checks, platform limitation copy and scoped overrides. Existing onboarding/policy tests cover account/authentication boundaries. |
| Compact screen/themes | Native Chat/active/failed/settings screenshots in both themes at 1020 × 680 logical size; actual output verified at 100%, 150%, 200%, plus 225%/300%. Essential actions and outcomes remain visible; details can scroll. |

## Verification runs

- Full Windows regression suite before the last presentation refinements: **487 passed, 6 skipped**; skipped checks require opt-in live inference.
- Focused final behavior checks: 52 UI/identity/journey/settings tests; 36 failure/orchestration/storage tests; 14 scope/draft/workspace tests; 17 provider/action checks; 12 activity/package checks. Later focused runs for final UI refinements passed 11, 9, and 6 tests respectively. These overlap and must not be summed as unique tests.
- Database schema v15 adds per-message receipts and local action result history. Fixture migration verifies legacy message preservation, an online pre-upgrade backup, cascade cleanup, and interrupted local run reconciliation.
- Screenshots under `docs/images/usability/` are fixture illustrations, not evidence of real provider execution. Native rendering and automated geometry checks verify the visible controls at the supported minimum size. The existing larger desktop workspace remains resizable.

## Delivery evidence

- Final source: `a3c4f0d`, MoliCoder **1.3.0**. Later source refinements were covered by focused Windows checks: 50 UI/model cases, 23 journey/orchestration cases, and 15 journey/activity cases. These counts overlap and are not totals of unique tests.
- Linux x64 and macOS Intel: **492 passed, 8 skipped** each, plus packaged desktop/migration and embedded-browser smoke checks. [Final-source platform CI](https://github.com/molicoder-bit/MoliCoder/actions/runs/38828193560).
- Final-source hosted Windows CI: **494 passed, 6 skipped**, plus packaged desktop/migration and embedded-browser smoke checks; workflow completed successfully. [Windows CI](https://github.com/molicoder-bit/MoliCoder/actions/runs/38828193431).
- Final Windows package desktop/migration and embedded-browser smoke checks passed. The package was installed at the existing user location with rollback preserved. Built and installed executable SHA-256 match: `5199474BDBC4A1E87252162795CE25708F5A3B5ABE46B43908634ED26ACF3033`.
- Desktop migration smoke upgrades a v14 fixture, preserves its conversation, verifies the pre-upgrade backup, and reconciles its interrupted local task. Provider inference was not needed.
- The supplied logo remains in the executable/window/shortcut. The generated ICO contains nine sizes; executable icon resource verification passed.

All 129 implementation tasks in the workspace usability plan are complete, including the final CI gate. Earlier package revisions were temporary development checkpoints and are superseded by the source/hash above.
