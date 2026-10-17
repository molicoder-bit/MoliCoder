# Task review and continuation — 1.5.3

Verified on Windows through the CLI and offscreen Qt, 2026-10-17.

## Workflow

Build now exposes **Review & continue** when a task is `review_required`.
The screen shows pending requirements, checks, the run diff, and execution
assignments. Its size is bounded by the available screen; details scroll.

- **Accept & continue** requires confirmation of every pending requirement.
  It reruns task checks, preserves the verified checkpoint, records human
  acceptance, and executes only remaining tasks. Final validation still runs.
- **Request changes & retry** requires feedback, preserves dirty failed
  output in Git, and sends the feedback to the assigned executor. A fresh
  attempt budget is explicitly authorized while cumulative attempt numbers
  remain intact. The revised result can require review again.
- **Review later** leaves the run unchanged. Saved reviews survive restart.
- **Apply ready result** remains a separate action after final checks pass.

The approval is bound to the saved plan revision, task states, completion
record, worktree branch, HEAD, and file contents. Changes during review reject
the decision. Missing checks, unknown commits, and unverified agent output
cannot be accepted as complete. Acceptance and the approved continuation
revision are committed in one database transaction.

Remaining tasks are evaluated independently using available, allowed execution
models. Completed task assignments are preserved. Accepting the final task
requires no model inference, including when the original planner is unavailable.

## Evidence

- 46 focused tests passed for review, exact plan approval/revision, interrupted
  recovery, and independent task routing before the final retry-history change.
- 81 final regression tests passed, including 18 review tests, sequential
  execution, runtime construction, Build usability, interrupted recovery,
  routing, worktree handling, and recovery classification.
- A separate desktop acceptance test passed through the real dialog, worker,
  next-task execution, and **Apply ready result**, verifying files in the
  isolated source checkout.
- Real Kilo execution with `kilo/kilo-auto/free` implemented a greeting function
  and pytest tests. It paused for a prose acceptance requirement. A fresh runtime
  loaded the saved review, reran checks, and reached `ready_to_apply` after
  acceptance. Provider attempts stayed at **1 before and 1 after review**.
  Planning for this focused check used a fixed fixture plan; no paid planner
  was invoked.
- Offscreen rendering was inspected at 760 × 730 within an 800 × 800 available
  screen, with the app theme and enlarged text.
- Packaged startup, appearance, WebEngine, and real Ollama conversation checks
  passed. Ollama answered the Python-list question normally.

Local evidence is under `artifacts/task-review-20261017/`, with regression
logs at `artifacts/task-review-regression.log` and
`artifacts/task-review-final-verbose.log`.

Package SHA-256:
`91506f49d69d3e067d74c92dc4556bf0592653a7d6558c6aa60c322663fd76a0`

## Limits

Human confirmation covers requirements automated checks could not establish.
It is not a substitute for successful validation. Unknown commits or external
side effects still need investigation; the dialog does not waive those
blockers. No native desktop control was used for these checks.
