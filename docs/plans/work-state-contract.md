# Work state contract

The workspace uses `WorkSnapshot` to read run status, rather than inferring activity from a connection indicator, animation, or most recent global token counter.

| Field | Source |
| --- | --- |
| operation_id | Stable `run:<database ID>` identity; request workers are additionally matched by object identity before UI callbacks |
| project_id, run_id | Saved run row |
| task_id | Task in running/verifying/retrying state belonging to that run |
| phase, outcome, reason | Run status and persisted recovery reason |
| requested_model, provider | Run configuration; never proof of the reported model |
| active_call | Latest unmatched provider_started event, cleared at terminal state |
| last_completed_call | Latest provider_finished event, including reported/configured provenance |
| last_event_at | Last persisted event timestamp, never an animation repaint |

The internal orchestrator owns execution transitions and finalization. Worker cleanup releases UI controls; it does not turn failure into Idle. The worker exception boundary attempts finalization if execution/resume escaped the orchestrator. Exceptions before allocation remain visible request-level errors. The UI owns only view selection and presentation. External Kandev state remains subject to external-ID reconciliation; startup local-process recovery excludes Kandev runs.

Active phases are preparing/preflight, planning, running/executing, verifying/validating, repairing, applying, and cancellation requested. Waiting phases include paused, waiting_auth, waiting_quota, and awaiting_external. Terminal outcomes are completed, failed, needs_attention, cancelled, ready_to_apply, and interrupted. A terminal snapshot cannot accept live updates for another operation or accept a late callback that would reopen the operation. An explicit resume is a new worker ownership interval.

Unexpected failure finalization uses one immediate SQLite transaction: end the run, mark active tasks failed, block unattempted tasks, and retain completed work and worktree/checkpoint fields. Repeated callbacks have no effect on an already terminal outcome. The orchestrator then writes the structured terminal event through the normal event/log writer. If log writing itself fails, the already committed database outcome survives and the original error remains visible.

Schema compatibility: run/task fields and the events payload column already exist. Schema v15 adds message_receipts (with cascading deletion) and local_action_results; upgrading a legacy database creates an online backup before writes. Optional route/context receipts can extend versioned event payloads without destructive schema changes. Legacy events missing these facts remain unknown. Future message receipts must be keyed by conversation/message identity; neither project selection nor current model settings can backfill historical evidence. Existing database initialization/backup behavior remains applicable. No production database is rewritten during development verification.

Conversation summaries read the selected reply receipt, with requested/reported provenance and included-source IDs. Busy presentation follows worker ownership and confirmed pause boundaries; pending cancellation is distinct from worker termination. Reconciliation compares process birth identities to avoid terminating live or reused-PID work; unverifiable ownership and external Kandev work are preserved for reconciliation.
