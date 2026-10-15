# Workspace usability plan and implementation tracker

Date: 2026-10-15

**Status: planning complete; implementation has not started.** This document responds to the two supplied screenshots and the requested usability changes. It does not authorize implementation, execute the failed folder request, or change the installed app or its saved records.

Tracker totals at creation: **4 completed planning tasks; 129 unchecked implementation tasks across 13 phases.**

## How to use this tracker

- Each checkbox represents one independently reviewable change. Complete tasks in the phase order below; within a phase, follow task order unless the stated prerequisite is already satisfied.
- Check a task only after its stated acceptance criterion is verified. Add the commit and test/manual evidence under that phase's Evidence line. A code change alone does not complete a behavior task.
- Keep incomplete, deferred, or blocked tasks unchecked. Record the reason and the next action beneath them. Do not check an entire phase because some tests passed.
- Historical UI01–UI11 completion remains in the [previous UI tracker](ui-redesign-tracker.md). This plan records newly identified gaps and revised acceptance criteria; those earlier checks do not complete these tasks.
- Use isolated fixture projects and databases for development checks. Real account calls are unnecessary for these UI changes; service connection checks and paid inference must remain distinguishable.

## Requests covered

| User request | Planned result | Primary phases |
| --- | --- | --- |
| Too many clicks to start a chat | A visible New chat button, usable from Chat or Build in one click | UX05 |
| Current model/provider remains unclear | Readable identity beside the conversation/run, with actual call attribution and honest unknowns | UX06 |
| Remove the old dropdown/context and Ollama text | Remove the legacy model toolbar and always-on token strip; show concise, relevant work context | UX06–UX07 |
| Why does Build already say neworld? | Explicit project choice, a useful no-project screen, and visible folder scope | UX04 |
| Running needs an animation | A visible animated activity indicator with stage, elapsed time, and last real update | UX08 |
| Simple failures must be obvious | Persistent failure summary, matching task/run states, and a useful recovery action | UX01–UX03 |
| Settings should be more user friendly | A single settings window with plain-language categories, provider cards, and guided setup | UX09–UX11 |

## Evidence and current causes

- [x] PLN-01 Inspect both screenshots and map the reported problems to the current UI code.
- [x] PLN-02 Inspect the relevant project selection, route identity, activity, worker failure, and folder-request code without changing it.
- [x] PLN-03 Inspect the relevant saved project/run records and run-4 log/plan using read-only access.
- [x] PLN-04 Write this atomic implementation tracker, proposed behavior, dependencies, and acceptance scenarios.

Confirmed findings:

1. **New chat is hidden in History.** `workspace.py` reparents the sidebar containing `_new_chat` into the History dialog. The header has no New chat action.
2. **The identity line exists but does not meet the usability requirement.** Screenshot 1 shows `Chat selected · Ollama (local) · molicoder:latest` in a small line, while another toolbar presents a preset name and model family. The alias, preset description, selected model, and actual responding model are not presented as one clear, durable identity. The banner returns to selected configuration after a worker finishes.
3. **The token meter is tied to Chat even when viewing Build.** `workspace.py` always puts the Chat context widgets in the global strip. Much of the displayed information describes settings or the next prompt rather than evidence about the current work.
4. **neworld is a real saved project.** The inspected settings select project ID 4, named `neworld`. Startup restores this selection; `_load_project_list` also falls back to the first registered project when there is no target. It is not a special built-in Build environment. The Personal workspace path changes UI state without clearing the registry's saved selection, so the two sources can disagree.
5. **The folder request entered the coding workflow.** The submitted sentence was `Let's create a new folder called "Messi" in c:\Dev`. The saved run-4 plan contains an absolute expected path outside the selected project: a folder named Messi under the Dev directory. `requested_folder` expects the parent path before `called "name"` and does not accept this prefix/word order. This mismatch sent the simple request to the planner.
6. **The failure was a worktree boundary rejection.** `TaskHandoff.__post_init__` rejects absolute expected paths. The task is marked running before `_task_prompt` constructs that handoff. This call is outside the preceding planning/worktree exception handler. The safety check should remain; the request routing and failure handling need correction.
7. **The persisted status is demonstrably stale.** At inspection, run 4 has status `running`, no end timestamp, provider `codex`, and no model ID. Its `create_messi_folder` task (ID 14) is also `running`. The screenshot shows the worker failure. `OrchestrationWorker` emits only an error string at its outer exception boundary, and `_build_failed` writes UI text without finalizing these records. The run log has no matching terminal failure event.
8. **An activity animation exists but is insufficient.** A four-character glyph rotation updates once per second in a label inside the scrollable Build content. It can be out of view, is overwritten by heartbeat/stage text, and resets to Idle on worker cleanup despite stale persisted running state.
9. **Settings is a menu of technical dialogs.** The current settings window closes when a section is chosen. Provider settings combine model IDs, CLI trust, API accounts, security facts, and route toggles in a long form.
10. **No evidence identifies GPT-4 as the model used by the inspected run.** Its recorded CLI model is unknown. Screenshot 1 shows a local Chat selection. The redesign must expose available evidence instead of inferring a model from provider names or connection health.

Relevant source entry points:

| Area | Files/functions to inspect or change |
| --- | --- |
| Shell, history, settings, summary refresh | `src/local_model_chat/ui/workspace.py` |
| New chat, project selection, worker callbacks, model controls | `src/local_model_chat/ui/main_window.py` |
| Provider/model presentation | `src/local_model_chat/ui/route_identity.py` |
| UI state and message rendering | `src/local_model_chat/ui/state.py`, `src/local_model_chat/ui/widgets.py` |
| Worker lifecycle | `src/local_model_chat/workers.py` |
| Run/task transitions and handoff creation | `src/local_model_chat/molicoder/orchestrator.py`, `src/local_model_chat/task_packet.py` |
| Project selection and persisted state | `src/local_model_chat/molicoder/registry.py`, `src/local_model_chat/storage.py` |
| Simple folder actions | `src/local_model_chat/local_actions.py`, `src/local_model_chat/controller.py` |
| Provider events and local model metadata | `src/local_model_chat/molicoder/providers.py`, existing Ollama inspection/discovery code |
| Kandev UI and reconciliation | `src/local_model_chat/ui/kandev_panel.py`, existing Kandev adapter/orchestration code |

Local diagnostic evidence: the user's `neworld/.molicoder/logs/run-4.jsonl`, corresponding `plans/plan-4.json`, and the matching rows in the local application database. Do not commit raw logs, full requests, local credentials, or user database copies. Create sanitized fixtures for tests.

## Proposed interaction and layout

Keep Chat for conversation and read-only planning, and Build for explicitly started execution. Switching modes must not execute a request or lose its draft. Preserve the optional Kandev board and task ownership choice.

The primary layout should read in this order:

1. **Header:** logo, Chat/Build navigation, a visible **New chat** button, explicit workspace/project selection, Theme, History, Settings.
2. **Current work summary:** a readable model/provider identity, the current project or “No project,” and a short activity/result description. Show only fields relevant to this conversation or run.
3. **Work area:** conversation and composer, or Build request and task/result view. No permanent legacy model dropdown, model-context badge, Chat token progress bar, or runtime connection slogan here.
4. **Details on demand:** Usage for token estimates and charges; Context details for sources actually included; Activity for logs; Settings for model/route selection; Performance for runtime health.
5. **Persistent run feedback:** while running, waiting, or failed, keep the status summary visible above scrolling content. A collapsed drawer must not hide the only failure indicator.

Examples are interface copy proposals, not claims about the user's accounts:

| State | Example main-screen information |
| --- | --- |
| New general chat | `New chat · No project` and `Next reply: [model name] · [provider]` |
| Local model response | `Reply from [verified model name or exact alias] · Local model` with exact runtime/ID in Details |
| External run with known configuration | `Planning · Codex · [model ID] requested` until a model is reported |
| External run without a reported model | `Planning · Codex · Model not reported` |
| Deterministic action | `Create folder · No AI model used · [confirmed target]` |
| Relevant context | `Included: 2 attachments and 1 approved decision` only if recorded for that request |
| Build activity | Animated indicator + `Creating plan · 00:18 · Last activity 2s ago` |
| Folder failure | `Folder was not created. This request targeted a location outside the selected project.` plus the applicable action and Details |
| Successful work | `Completed · 2 files changed · Validation passed` only when the recorded outcome supports each claim |

**Defaults proposed for implementation:** start in general Chat with no project unless the user explicitly enables restoration; show recently used projects as choices. Opening an existing project conversation shows its saved project association. Keep Matrix as the default theme and retain saved Light choices. New chat preserves the explicitly selected scope and labels it; choosing No project fully clears that scope. Model changes belong in Settings, reachable directly from the identity area's Change action.

## Execution order and completion rule

Implement UX01 → UX02 → UX03 → UX04 → UX05 → UX06 → UX07 → UX08 → UX09 → UX10 → UX11 → UX12 → UX13. This resolves incorrect state before adding new presentations of it. UX09–UX11 share one settings shell; do not build competing dialogs for them.

An implementation phase is complete only when every task in that phase is checked and its Evidence entry links the relevant revision and verification. Evidence is currently pending for all UX phases.

## UX01 — Define one source of current work state

Prerequisite: planning complete. Primary files: UI state, storage, workers, orchestrator.

- [ ] UX01-01 Define the shared work snapshot fields: mode, project, conversation/run/task IDs, phase, outcome, active call, last completed call, and last event time. **Done when:** each displayed field has a named authoritative source.
- [ ] UX01-02 Specify the lifecycle transitions for idle, preparing, planning, executing, validating, applying, waiting, cancellation requested, cancelled, succeeded, failed, and interrupted. **Done when:** waiting or terminal states cannot be rendered as active execution.
- [ ] UX01-03 Define which component owns each transition for internal Build, local actions, Chat, and Kandev. **Done when:** worker cleanup cannot independently overwrite a terminal outcome with Idle.
- [ ] UX01-04 Add a stable operation identity to callbacks and UI snapshots. **Done when:** an event from an older request cannot update a newly selected conversation or run.
- [ ] UX01-05 Define backward-compatible storage changes for outcome reasons, route provenance, and context receipts where existing records are insufficient. **Done when:** existing histories load with explicit unknowns and database backup/migration behavior is documented.
- [ ] UX01-06 Implement snapshot loading for the selected conversation/run rather than using global last-metric fields. **Done when:** switching history entries shows their own state and cannot reuse another entry's counters.
- [ ] UX01-07 Define stale-run reconciliation rules for startup and reopen. **Done when:** local process exit is distinguishable from an active external Kandev session; elapsed time alone is insufficient to mark external work failed.
- [ ] UX01-08 Add focused transition regressions for completion, exception, cancellation, and late callbacks. **Done when:** all UI consumers agree on the same operation's terminal state.

Evidence: pending.

## UX02 — Make failures durable and visible

Prerequisite: UX01. Primary files: orchestrator, workers, storage, Build callbacks.

- [ ] UX02-01 Enclose handoff construction in a lifecycle error boundary with run/task identity. **Done when:** an invalid expected path reaches failure finalization rather than leaving the task running.
- [ ] UX02-02 Finalize the active task on an unexpected exception. **Done when:** it stores the terminal state, reason, and relevant worktree evidence exactly once.
- [ ] UX02-03 Finalize the parent run on that same exception. **Done when:** its outcome and end timestamp agree with the task's outcome.
- [ ] UX02-04 Mark dependent work that was never attempted as blocked or skipped with the cause. **Done when:** untouched tasks are not falsely described as failed executions or completed work.
- [ ] UX02-05 Write a structured terminal event to the run log and emit it to the UI. **Done when:** a reopened history entry explains the same failure as the live screen.
- [ ] UX02-06 Handle exceptions before a run ID is allocated. **Done when:** a visible request-level failure appears without inventing a run or leaving an indefinite busy state.
- [ ] UX02-07 Add a persistent failure panel above Build's scrolling content. **Done when:** it states what failed, the effect on the requested action, and an appropriate next step without requiring Activity to be opened.
- [ ] UX02-08 Map common failure categories to plain-language explanations. **Done when:** invalid target, unavailable provider, sign-in required, quota, permission denial, validation failure, and unexpected worker error have distinct useful copy; technical details remain accessible.
- [ ] UX02-09 Keep failure summaries visible across refresh, worker cleanup, and history reopening. **Done when:** periodic refresh cannot replace a failed run with Running, Idle, or a generic readiness message.
- [ ] UX02-10 Add contextual recovery actions. **Done when:** correcting a target, opening Settings, viewing details, or retrying is offered only when valid for that failure, and retry preserves the request without silently re-executing it.
- [ ] UX02-11 Reconcile orphaned local running records using UX01's ownership rules. **Done when:** a fixture matching run 4 becomes Interrupted/Needs attention with an explanation, while an active verified external session stays active.
- [ ] UX02-12 Reproduce the screenshot sequence with a sanitized invalid-path fixture. **Done when:** task, run, summary, animation, log, and reopened history all show the terminal failure; no original user record is silently deleted.

Evidence: pending.

## UX03 — Route simple folder requests correctly

Prerequisite: UX02. Primary files: local actions, controller, Build dispatch, plan validation.

- [ ] UX03-01 Capture a sanitized fixture for the exact reported folder sentence and currently accepted alternatives. **Done when:** the failing route can be reproduced without calling a real model or creating the user's folder.
- [ ] UX03-02 Separate local-action recognition from validation of its target. **Done when:** a recognized request with a missing parent or invalid name produces an actionable problem instead of silently becoming a coding request.
- [ ] UX03-03 Support explicit full-path and parent-plus-name folder requests, including paths with spaces and ordinary Windows separators. **Done when:** each accepted form resolves to one exact target; ambiguous forms remain unresolved.
- [ ] UX03-04 Add inline clarification for an ambiguous destination. **Done when:** a name-only request asks where to create it, instead of assuming the selected project or sending it to a coding agent.
- [ ] UX03-05 Share the action decision between Chat and Build submission. **Done when:** the same explicit simple request follows the same local-action route in both modes.
- [ ] UX03-06 Present the exact resolved target and action before the existing confirmation. **Done when:** a folder outside the project is clearly described as an explicit local filesystem action and is never disguised as a worktree edit.
- [ ] UX03-07 Record and render local-action outcomes. **Done when:** created, already exists, conflicting file, denied, cancelled, and failed are distinct results; all say no AI model was used.
- [ ] UX03-08 Verify folder existence/type before reporting success. **Done when:** a fixture with a permission error cannot generate a success message, and an existing directory's contents remain intact.
- [ ] UX03-09 Validate planner expected paths before marking any task running or creating its worktree. **Done when:** an absolute path outside project scope receives a clear route/scope rejection before task execution starts.
- [ ] UX03-10 Handle an absolute path that genuinely refers inside the chosen repository through validated path resolution. **Done when:** only proven contained paths can become repository-relative; outside paths, traversal, and link escapes remain rejected.
- [ ] UX03-11 Preserve the worktree safety boundary. **Done when:** the implementation does not fix this report by allowing arbitrary absolute paths, silently changing targets, or interpreting model output as filesystem authorization.
- [ ] UX03-12 Test folder actions in a temporary directory, including non-Git locations. **Done when:** an explicit local folder action works without a Build project, uses zero model calls, and has a visible verified result. Empty directories must not be misreported as committed Git changes.

Evidence: pending.

## UX04 — Make project and folder scope explicit

Prerequisite: UX01–UX03. Primary files: registry, workspace selector, project/conversation loading.

- [ ] UX04-01 Remove the automatic first-project fallback on fresh/default startup. **Done when:** a database with registered projects but no explicit restoration preference opens with No project.
- [ ] UX04-02 Add an optional Remember last workspace preference, off by default. **Done when:** restoring a project requires this preference, and a restored selection is visibly identified with its folder.
- [ ] UX04-03 Add a registry-level clear-selection operation. **Done when:** choosing No project clears both current UI scope and the saved selected-project setting.
- [ ] UX04-04 Unify selection consumers around the chosen scope. **Done when:** readiness, route identity, usage, knowledge, and execution cannot independently recover a stale saved project.
- [ ] UX04-05 Add a Build empty state with Open folder, Create project, and recent-project choices. **Done when:** these actions are visible without entering History, and neworld appears only as a real recent/selected project.
- [ ] UX04-06 Make folder identity readable before execution. **Done when:** Build shows the project's name and resolved path near the request, with a direct Change action.
- [ ] UX04-07 Allow drafting before project selection. **Done when:** the Build composer remains usable with no project; a repository operation explains the missing target while a valid local action can use its explicit path.
- [ ] UX04-08 Keep conversation association explicit. **Done when:** opening an old project chat displays that association, while opening a general chat clears unrelated project context.
- [ ] UX04-09 Preserve per-scope Chat/Build drafts and attachments. **Done when:** moving between No project and two different projects restores the matching draft without relinking existing messages silently.
- [ ] UX04-10 Keep active Build ownership fixed while making the reason visible. **Done when:** a disabled Change project action explains that the current run owns the target; Chat/New chat navigation remains available.
- [ ] UX04-11 Handle moved, missing, and archived folders with an explicit recovery state. **Done when:** the app never silently selects a different project as a substitute.
- [ ] UX04-12 Verify startup, restart, deselection, history reopening, and two-project switching. **Done when:** no operation reads or writes neworld unless that scope was explicitly chosen or visibly restored under the preference.

Evidence: pending.

## UX05 — Start a chat in one click

Prerequisite: UX01 and UX04. Primary files: header, chat lifecycle, draft state.

- [ ] UX05-01 Add a labeled New chat button to the main header. **Done when:** it is visible at the supported minimum window width in both Chat and Build.
- [ ] UX05-02 Wire the action to create and open a new conversation. **Done when:** one click opens an empty Chat composer and places keyboard focus there.
- [ ] UX05-03 Add a keyboard shortcut with the same behavior. **Done when:** Ctrl+N on Windows/Linux and the platform-equivalent shortcut on macOS invoke the shared action without conflicting with existing navigation.
- [ ] UX05-04 Preserve an unsent draft when New chat is used. **Done when:** returning to the previous conversation restores its text and attachments; a normal idle action requires no confirmation dialog.
- [ ] UX05-05 Handle New chat during an active reply. **Done when:** the old request is cancelled or safely completes against its original conversation, and late tokens cannot appear in the new one.
- [ ] UX05-06 Leave an active Build attached to its existing run while navigating to a new chat. **Done when:** work is neither restarted nor abandoned, and a persistent Build status affordance returns to it.
- [ ] UX05-07 Apply the explicit current scope and selected next-reply model to the new chat. **Done when:** the empty state names both and never implies that a reply has already used a model.
- [ ] UX05-08 Verify the one-click journey from initial launch, history, a completed chat, and Build. **Done when:** no History/Settings detour is required and all previous work remains recoverable.

Evidence: pending.

## UX06 — Show the model/provider actually associated with the work

Prerequisite: UX01, UX04, UX05. Primary files: route identity, provider events, message/run storage, transcript.

- [ ] UX06-01 Define a route identity record containing provider ID/display name, requested model, reported model, alias/display label, evidence source, and call identity. **Done when:** configured and reported values cannot overwrite each other's meaning.
- [ ] UX06-02 Resolve local model labels from available verified discovery metadata. **Done when:** a known model family can accompany its exact alias, while a user-created alias is never treated as proof of underlying weights.
- [ ] UX06-03 Capture model reports from supported provider events as soon as they arrive. **Done when:** the active identity updates before completion when evidence exists; unsupported CLI reports remain explicitly unknown.
- [ ] UX06-04 Persist route attribution with completed assistant responses. **Done when:** reopening a conversation identifies the route used for each response rather than the model currently selected in Settings.
- [ ] UX06-05 Persist route attribution for individual Build stages/attempts. **Done when:** planning, local execution, fallback, and review can show different providers/models without mislabeling the entire run as one model.
- [ ] UX06-06 Represent deterministic replies and actions explicitly. **Done when:** project-status answers and folder actions say No AI model used and are not attributed to the selected local model.
- [ ] UX06-07 Replace the small legacy banner with a readable identity component beside current work. **Done when:** provider and model can be identified at a glance in both themes and normal display scaling.
- [ ] UX06-08 Separate Next reply/Selected for Build from Working now and Last reply/Last call. **Done when:** changing settings does not relabel prior output, and worker cleanup retains the last actual identity.
- [ ] UX06-09 Add response attribution in the transcript. **Done when:** a reader can determine the origin of a prior response without selecting the original model again; legacy records with missing evidence are labeled unavailable.
- [ ] UX06-10 Show an active fallback or local handoff immediately. **Done when:** the old provider/model disappears from the active-call field and remains only in historical evidence.
- [ ] UX06-11 Handle Kandev and custom API tests explicitly. **Done when:** Kandev displays only identities supported by adapter evidence, and API tests show their own provider/model without altering Chat attribution.
- [ ] UX06-12 Add a compact Change action that opens Settings at the relevant model/route section. **Done when:** the main screen remains free of the legacy dropdown and the current model stays visible while settings are closed.
- [ ] UX06-13 Verify local alias, explicit external model, unknown CLI default, fallback, deterministic reply, and restarted history scenarios. **Done when:** no case derives the active route from an Ollama health indicator or labels an unknown model as GPT-4.

Evidence: pending.

## UX07 — Replace legacy controls with relevant context and results

Prerequisite: UX01, UX02, UX04, UX06. Primary files: workspace, Chat toolbar, request/context assembly, usage views.

- [ ] UX07-01 Remove the permanent Chat model dropdown and context-size badge from the main work area. **Done when:** model selection remains available through UX06's Change action and Settings.
- [ ] UX07-02 Move the global Chat token meter and reserved-output controls into Usage/advanced model controls. **Done when:** Build no longer displays a Chat estimate as though it describes the running task.
- [ ] UX07-03 Remove generic runtime/local-route slogans from the main-screen summary. **Done when:** runtime connection health appears in the relevant settings/Performance view, with an actionable alert only when it blocks the chosen operation.
- [ ] UX07-04 Define a context receipt for each submitted request. **Done when:** it records which conversation turns, attachments, project files, decisions, and tool results were actually included, with IDs and scope.
- [ ] UX07-05 Produce the receipt from request assembly. **Done when:** selected project metadata alone cannot imply that the full repository or all knowledge was sent to a model.
- [ ] UX07-06 Render a short Chat context summary from the receipt. **Done when:** users see relevant included sources and scope, and an empty chat has a useful empty state rather than stale counters.
- [ ] UX07-07 Render a Build summary from its current task and verified outcomes. **Done when:** current step, target, completed tasks, changed files, and validation appear only when known and relevant.
- [ ] UX07-08 Add a Context details action for inspectable source names and omissions. **Done when:** the summary can be checked against actual request evidence without exposing secret values or claiming unsupported retrieval.
- [ ] UX07-09 Retain useful warnings near submission. **Done when:** context overflow, missing required input, or a real configured spending limit is visible before sending, even with the routine meter moved out of the main screen.
- [ ] UX07-10 Scope Usage to the selected request/run and label missing measurements. **Done when:** unknown token counts/cost are not displayed as zero and prior conversation metrics cannot leak into a new chat.
- [ ] UX07-11 Keep current configuration distinct from verified work already done. **Done when:** a failed attempt cannot increase completed/changed/accepted counts without supporting evidence.
- [ ] UX07-12 Verify every summary against fixture receipts and outcomes. **Done when:** changing project, model, conversation, or run clears irrelevant information and preserves the correct historical record.

Evidence: pending.

## UX08 — Animate real activity and explain waiting

Prerequisite: UX01, UX02, UX06, UX07. Primary files: Build status widgets and lifecycle/event rendering.

- [ ] UX08-01 Add a graphical busy indicator independent of the status text. **Done when:** animation uses a dedicated Qt widget/timer and cannot be overwritten by a provider heartbeat string.
- [ ] UX08-02 Keep the activity summary above scrollable Build content. **Done when:** the indicator and current stage stay visible when the task list or drawer is scrolled.
- [ ] UX08-03 Start the indicator when the operation is accepted for work. **Done when:** preflight and planning have immediate feedback before the first provider output arrives.
- [ ] UX08-04 Derive the stage label from operation events. **Done when:** planning, editing, checking, applying, and reconciling have meaningful names and do not alternate with generic Working text.
- [ ] UX08-05 Show elapsed time and time since the last genuine event. **Done when:** repaint/animation ticks never reset the last-activity timestamp.
- [ ] UX08-06 Display waiting and possible stalls truthfully. **Done when:** an inactive stream says Waiting for provider/No recent update with elapsed time, without falsely proving progress or declaring failure solely from silence.
- [ ] UX08-07 Wire pause and cancellation states to the indicator. **Done when:** Pause requested, Paused, Cancelling, and Cancelled are distinct, and cancellation does not report completion before the worker stops.
- [ ] UX08-08 Stop animation on every terminal outcome. **Done when:** success, failure, interruption, and cancellation produce stable labeled outcomes rather than Idle.
- [ ] UX08-09 Add a compact background Build indicator when Chat is selected. **Done when:** the user can tell that Build is active or needs attention and return to that run in one click.
- [ ] UX08-10 Provide reduced-motion behavior. **Done when:** the preference uses a stable status icon plus text/time, without losing any state information.
- [ ] UX08-11 Verify animation with delayed fixture events and failure injection. **Done when:** the UI remains responsive, makes no fake percentage claims, and never shows an active spinner beside a terminal failed task.

Evidence: pending.

## UX09 — Create a navigable settings shell

Prerequisite: UX04–UX08. Primary files: settings entry point and existing dialogs.

- [ ] UX09-01 Define plain-language settings categories: General, Models & providers, Projects & Build, Usage & limits, Kandev, and Diagnostics. **Done when:** every existing setting/action has a destination and no required capability disappears.
- [ ] UX09-02 Implement one settings window with category navigation and a content pane. **Done when:** changing categories keeps the window open and avoids stacked modal dialogs for routine navigation.
- [ ] UX09-03 Add a short explanatory sentence to each category. **Done when:** a new user understands what can be changed before reading individual technical fields.
- [ ] UX09-04 Mark setting scope as App, Project, or Next request. **Done when:** project-specific changes name their target, and settings that need a project explain that requirement inline.
- [ ] UX09-05 Define Save/Cancel and immediate-preference behavior consistently. **Done when:** changes are not lost on accidental navigation, and immediate choices such as Theme are clearly shown as applied.
- [ ] UX09-06 Add direct destinations from Change model, missing provider, budget warning, and run error actions. **Done when:** each opens the matching category and focuses the relevant control.
- [ ] UX09-07 Move implementation terminology into optional details and improve primary labels. **Done when:** primary actions use Choose model, Connect account, Test connection, Set limit, or Open logs instead of requiring users to understand offerings, manifests, or handoff packets.
- [ ] UX09-08 Add inline validation and consistent pending/success/error feedback. **Done when:** field mistakes are explained next to the field and operations cannot be accidentally submitted twice.
- [ ] UX09-09 Verify keyboard navigation, scrolling, and return to work. **Done when:** Settings works at minimum window size and closing it returns focus to the originating action/composer.

Evidence: pending.

## UX10 — Make model and provider setup understandable

Prerequisite: UX06 and UX09. Primary files: provider settings, onboarding, environment discovery.

- [ ] UX10-01 Present installed/registered providers as cards with name, local/cloud type, readiness, and one relevant next action. **Done when:** Disconnected, Sign-in needed, Ready, and Unavailable are visually and textually distinct.
- [ ] UX10-02 Separate provider installation, account connection, and model selection steps. **Done when:** a successful installation does not imply that authentication or model availability is complete.
- [ ] UX10-03 Add a local-model chooser with friendly names and exact identifiers in details. **Done when:** the user's chosen local model is visible in the main identity component and persisted through the existing settings mechanism.
- [ ] UX10-04 Add external-agent model controls with a clearly labeled Provider default option. **Done when:** leaving an override empty is described as an unknown/provider-controlled model choice, not a known named model.
- [ ] UX10-05 Label supported uses for each connection. **Done when:** a route available only for Build or capped API tests cannot be presented as selectable for main Chat; this phase does not silently add hosted Chat execution.
- [ ] UX10-06 Guide supported provider setup through the existing installer/sign-in flows. **Done when:** the user sees the next required step, including any official browser sign-in, without being promised automatic account creation.
- [ ] UX10-07 Simplify custom-provider entry into essential fields with advanced options collapsed. **Done when:** endpoint, model, and credential requirements have examples and field validation; advanced facts remain available when needed.
- [ ] UX10-08 Distinguish Test connection from Send test message. **Done when:** a connectivity check cannot silently spend inference quota and a paid test exposes its existing cap/authorization.
- [ ] UX10-09 Show setup failures and platform limitations next to the affected provider. **Done when:** unsupported credential storage, missing binaries, or rejected authentication each has a concrete explanation and available next action.
- [ ] UX10-10 Verify local setup, known CLI default, expired sign-in, custom API configuration, and unavailable-platform fixtures. **Done when:** each flow reaches a clear usable or blocked state without leaking credential values into the UI, exports, or logs.

Evidence: pending.

## UX11 — Finish the remaining settings categories

Prerequisite: UX09–UX10. Primary files: settings pages and existing preference services.

- [ ] UX11-01 Implement General preferences for Matrix/Light, readable text size, reduced motion, and Remember last workspace. **Done when:** each setting persists and its scope/default is evident.
- [ ] UX11-02 Implement a project list with name, folder, and Open/Change/Remove-from-list actions. **Done when:** removing a registration is described accurately and does not delete the folder.
- [ ] UX11-03 Explain Build strategy choices in everyday language. **Done when:** Local offline, Hybrid, and External provider behavior, constraints, and possible provider handoffs are understandable before Run.
- [ ] UX11-04 Present project trust and execution ownership as explicit choices. **Done when:** Internal Build versus Kandev ownership and provider permission cannot be confused with a model selection.
- [ ] UX11-05 Present Usage & limits with measured, estimated, and unavailable values distinguished. **Done when:** subscription CLI limits and hard metered API limits are not described as equivalent spending controls.
- [ ] UX11-06 Provide Kandev setup/status and Open board in its category. **Done when:** disabled, installed/stopped, connected, and active-task states each have appropriate actions; optional Kandev remains easy to opt into or leave unused.
- [ ] UX11-07 Put environment checks, runtime health, logs, and evidence export under Diagnostics. **Done when:** technical detail remains reachable without dominating normal settings or the main workspace.
- [ ] UX11-08 Verify that the reorganized settings preserve existing authorization boundaries. **Done when:** opening a page, checking health, or choosing a model never silently launches an agent, installs a resource, starts a paid test, or applies project changes.

Evidence: pending.

## UX12 — Check readability and complete user journeys

Prerequisite: UX02–UX11. Primary files: layout/styles and behavioral UI tests.

- [ ] UX12-01 Check both themes at 1020 × 680 and larger desktop widths. **Done when:** New chat, model/provider identity, project scope, activity, and failure state stay visible without horizontal scrolling.
- [ ] UX12-02 Check display scaling and long names/paths. **Done when:** 100%, 150%, and 200% scaling keep text readable; essential identity/outcome text is not available only through a tooltip.
- [ ] UX12-03 Check semantic state styling and keyboard focus. **Done when:** errors and busy states remain understandable without relying on color or animation, and focus order follows visible controls.
- [ ] UX12-04 Check conversation density and empty states. **Done when:** short messages and the current summary do not consume disproportionate vertical space, and welcome/empty copy explains the immediate next action.
- [ ] UX12-05 Run the end-to-end acceptance scenarios below using fixtures. **Done when:** each scenario has a recorded outcome and any failure is fixed before release.
- [ ] UX12-06 Capture final native screenshots for Chat, Build active, Build failed, and Settings in both themes. **Done when:** screenshots show the actual implemented states at usable text size and fixture data is identified as such.

Evidence: pending.

## UX13 — Document and deliver the finished implementation

Prerequisite: all preceding implementation phases.

- [ ] UX13-01 Update README usage instructions and screenshots. **Done when:** New chat, provider/model identity, project scope, contextual details, failure recovery, and Settings match the implemented UI.
- [ ] UX13-02 Update the earlier UI tracker and main platform tracker to point to this phase's results. **Done when:** historical verification remains distinguishable from the new acceptance results.
- [ ] UX13-03 Run the focused regression suites for every changed behavior. **Done when:** results cover conversations, routing, state transitions, project selection, folder actions, usage receipts, settings, and Kandev ownership as applicable.
- [ ] UX13-04 Run the required broader release/CI checks. **Done when:** supported-platform results are recorded accurately, with any unavailable platform validation explicitly left pending.
- [ ] UX13-05 Build the release package with version, logo/icon assets, and migration support. **Done when:** the build includes the intended source revision and keeps the existing Windows branding.
- [ ] UX13-06 Run packaged desktop and embedded-browser smoke checks. **Done when:** both pass and startup with a copied legacy fixture database preserves history and reconciles stale local state correctly.
- [ ] UX13-07 Install the verified Windows build at the existing user location and preserve rollback. **Done when:** the installed executable matches the tested artifact; a running user session is handled without force-closing or losing work.
- [ ] UX13-08 Commit/push the implementation under the existing delivery workflow and record the revision. **Done when:** the remote contains the intended changes and the tracker records only tasks with completed evidence.

Evidence: pending.

## End-to-end acceptance scenarios

These are release acceptance criteria, not claims that implementation is complete. Record outcomes under UX12-05; do not create duplicate completion checkboxes here.

| Scenario | Required observable result |
| --- | --- |
| Launch with neworld already in the registry | General Chat opens with No project unless explicit workspace restoration is enabled; Build offers a target chooser. |
| Start a new conversation from Chat | One visible New chat click opens and focuses an empty composer; previous content/draft remains in history. |
| Start a new conversation while Build runs | Chat opens; the existing run remains attached to its target and is reachable from a visible active-status affordance. |
| Switch local model, then inspect an old answer | Next reply reflects the new choice; the old answer retains its recorded attribution. |
| External provider starts with an unknown CLI model | Provider name is prominent; model is shown as not reported until supported evidence arrives. |
| Hybrid switches from local execution to an external provider | Working now changes immediately; previous stages retain their own identities and known usage. |
| Ask for deterministic project status | Relevant project/run evidence appears; response says No AI model used. |
| Create an explicit folder outside a Git project | A local-action confirmation shows the exact path; verified result appears visibly; no coding agent/worktree is used. |
| Ask to create a folder without a destination | The UI asks for the missing target and preserves the request; it never guesses neworld. |
| Planner returns an outside-worktree path | The plan is rejected with a clear explanation before execution; guard remains enforced and no task remains falsely running. |
| Worker throws during task handoff | Run and active task become terminal, animation stops, failure panel remains visible, and history/log agree after reopening. |
| UI restarts after a local worker crash | Orphaned local work is marked Interrupted/Needs attention with preserved evidence; external session state is checked separately. |
| Provider stays silent while the process is alive | Animation indicates an active operation; last-activity age and Waiting status are honest and distinct from confirmed progress. |
| Inspect what was sent for a request | Context details list actual included sources for that request, without claiming that the entire selected repository was sent. |
| Open Settings to choose a model or connect a provider | The relevant page is reachable directly; labels explain readiness and supported modes, and save/cancel behavior is predictable. |
| Use the small window with either theme | New chat, identity, scope, busy/failure feedback, and primary actions are readable and reachable. |

## Scope boundaries and decisions for later implementation

- This request is planning only. No production UI changes, failed-run repairs, folder creation, package installation, or provider calls are part of this planning pass.
- The worktree boundary rejection is valid. The plan addresses intent routing and error finalization; it does not broaden filesystem permissions for coding agents.
- Keep the existing supported routes. Adding cloud models to the main Chat execution path is a separate feature; Settings must label present capabilities honestly.
- The exact base model behind an alias or opaque CLI may be unavailable. Display available evidence and retain an explicit unknown rather than substituting a guessed model name.
- Reconciliation of the user's existing stale run should happen only through the verified recovery behavior, preserving its history and any worktree. Planning does not mutate that record.
- Existing model/agent installation and paid-use approvals remain part of their respective actions. Ordinary navigation, New chat, mode changes, and settings browsing should not introduce extra approval prompts.
- Optional second-machine testing remains deferred under the user's earlier preference; these UI acceptance scenarios need no remote worker.
