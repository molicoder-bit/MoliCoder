# Independent Build task routing — 1.5.2

## Correction

The previous runner inherited the planning provider/model whenever a task failed a narrow local/free shortcut. New files were excluded from local execution, and incidental words such as “dependency” also excluded tasks even when the instruction was to leave dependencies unchanged. One regression test explicitly required the planner model to be used for execution; that expectation has been corrected.

The planner now supplies a separate low/medium/high execution complexity for every task. The shared capability/cost evaluator supplies ranked candidates for each level. Host policy raises requirements for higher risk or wider scope. The runtime freezes the available, project-allowed execution pool for plan review. It ignores planning-only model overrides when selecting executors.

Hybrid selects suitable local, then free, then subscription capacity. Codex model selection uses advertised descriptions and supported effort levels; exact subscription prices remain unknown. Frontier-only excludes local/free execution; Local offline excludes external execution. The bounded local executor supports new files within the approved task scope. The review records model, effort, complexity, reason and fallback. Execution dispatches that assignment and restores provider effort afterward so it cannot mutate the planner or approval policy.

Unrelated ignored outputs still block automatic retries. Generated pytest/interpreter caches are preserved in the failed-attempt stash and removed from the retry workspace, while run logs remain available.

## Verification

All interaction used CLI commands and offscreen Qt; no desktop control.

- 98 routing, planning, approval/revision, provider, repair and execution regression tests passed.
- A final 42-test run covering UI review, approvals, task evaluation, worktree preservation and recovery passed. These runs overlap; counts are not a unique-test total.
- Real calculator plan: Codex/gpt-6.1-sol planned; Kilo's guarded free profile implemented addition, CLI and tests; pytest passed and the isolated result reached `ready_to_apply` in 42.49 seconds. No Codex implementation call.
- Real replay of the same generated task after removing the incidental-word exclusion: Ollama exhausted its bounded attempt; the first test exposed a cache-induced handoff failure. After the recovery fix, Ollama handed off to the reviewed Kilo free profile, pytest passed, and the result reached `ready_to_apply` in 72.89 seconds. No Codex implementation call. This verifies recovery, not that Ollama can reliably complete this task.
- Current local Codex catalog, with only Codex allowed: low complexity selected gpt-6-luna/low, medium selected gpt-6-luna/medium, high selected gpt-6.1-sol/high. This was a selection check without inference, not a live quality benchmark of those Codex models.
- Saved plan overview and task detail rendered and inspected offscreen.
- Frozen package passed startup, appearance, WebEngine and real Ollama conversation checks. The conversation returned a coherent definition of a Python list.

Detailed reports, scripts, logs and images are retained locally in `artifacts/task-routing-20261017/`. Package SHA-256: `ae4234b3d554126856ea687b31bf17ad443bb0dcb5e1bf9b5ce8fd470cd38288`.

## Use

Generate a fresh plan; older approved plans retain their original assignments. Free external providers must be allowed in the project's access settings. Task execution continues to verify output and preserve work in isolated worktrees before apply.
