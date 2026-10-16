"""Run the currently implemented deterministic Build curriculum cases.

The catalog comes from docs/BUILD_SCENARIOS.md. Unmapped IDs are reported as
NOT_RUN, never as a synthetic pass. External providers are never invoked here.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "BUILD_SCENARIOS.md"
ARTIFACTS = ROOT / "artifacts" / "build-scenarios"

# An entry is added only after its test exercises production workflow code and
# asserts objective results. The shared S001/S002 case checks both variations.
CASES = {
    "S001": "tests/test_build_plan_approval.py::test_s001_s002_plan_approval_and_exact_output",
    "S002": "tests/test_build_plan_approval.py::test_s001_s002_plan_approval_and_exact_output",
    "S003": ("tests/test_build_level0_scenarios.py", "S003"),
    "S004": ("tests/test_build_level0_scenarios.py", "S004"),
    "S005": ("tests/test_build_level0_scenarios.py", "S005"),
    "S006": ("tests/test_build_level0_scenarios.py", "S006"),
    "S007": "tests/test_build_level0_scenarios.py::test_s007_empty_directory_survives_review_and_apply",
    "S008": "tests/test_build_level0_scenarios.py::test_s008_independent_files_have_no_dependency_edge",
    "S009": ("tests/test_build_level0_scenarios.py", "S009"),
    "S010": "tests/test_build_level0_scenarios.py::test_s010_exact_already_satisfied_request_has_verified_no_change",
    "S011": "tests/test_build_level1_preflight.py::test_s011_build_without_selected_project_stays_disabled",
    "S012": "tests/test_build_level1_preflight.py::test_s012_valid_project_is_selected_for_build_preflight",
    "S013": "tests/test_build_level1_preflight.py::test_s013_missing_project_folder_blocks_build",
    "S014": "tests/test_build_level1_preflight.py::test_s014_read_only_project_blocks_before_planning",
    "S015": "tests/test_build_level1_preflight.py::test_s015_non_git_project_is_explicitly_blocked",
    "S016": ("tests/test_build_level1_preflight.py", "S016"),
    "S017": ("tests/test_build_level1_preflight.py", "S017"),
    "S018": ("tests/test_build_level1_preflight.py", "S018"),
    "S019": "tests/test_build_level1_preflight.py::test_s019_pending_plan_cannot_be_approved_from_another_project",
    "S020": "tests/test_build_level1_preflight.py::test_s020_chat_status_answer_reads_persisted_state_without_starting_build",
    "S021": "tests/test_build_plan_approval.py::test_s001_s002_plan_approval_and_exact_output",
    "S022": "tests/test_build_level8_parallel.py::test_s022_config_creation_precedes_importing_code",
    "S023": "tests/test_build_level8_parallel.py::test_s081_s086_independent_tasks_overlap_and_integrate_distinct_worktrees",
    "S024": "tests/test_build_level8_parallel.py::test_s024_three_step_chain_waits_for_each_verified_parent",
    "S025": "tests/test_build_level8_parallel.py::test_s083_s087_diamond_waits_for_verified_parent_artifacts",
    "S026": "tests/test_build_level8_parallel.py::test_s083_s087_diamond_waits_for_verified_parent_artifacts",
    "S027": "tests/test_build_level8_parallel.py::test_s027_explicit_user_order_is_recorded_and_enforced",
    "S028": ("tests/test_build_level2_graph.py", "S028"),
    "S029": ("tests/test_build_level2_graph.py", "S029"),
    "S030": "tests/test_build_level2_graph.py::test_s030_fragmented_trivial_plan_is_flagged_for_review_without_losing_checks",
    "S031": "tests/test_build_plan_approval.py::test_s031_s039_stale_plan_revision_cannot_execute",
    "S032": "tests/test_build_plan_approval.py::test_s001_s002_plan_approval_and_exact_output",
    "S033": "tests/test_build_plan_approval.py::test_s033_rejected_plan_is_persisted_without_execution",
    "S034": "tests/test_build_plan_revision.py::test_s034_add_task_through_review_requires_new_approval",
    "S035": "tests/test_build_plan_revision.py::test_s035_removing_parent_rejects_dangling_edge_then_saves_valid_revision",
    "S036": "tests/test_build_plan_revision.py::test_s036_edited_dependency_controls_execution_order",
    "S037": "tests/test_build_plan_revision.py::test_s037_compatible_task_model_override_is_approved_recorded_and_executed",
    "S038": "tests/test_build_plan_revision.py::test_s038_unsupported_runner_override_is_explained_without_execution",
    "S039": "tests/test_build_plan_approval.py::test_s031_s039_stale_plan_revision_cannot_execute",
    "S040": "tests/test_build_level6_repair.py::test_s069_scope_expansion_reapproves_and_preserves_completed_checkpoint",
    "S041": "tests/test_build_level4_routing.py::test_s041_s043_simple_existing_file_uses_local_executor",
    "S043": "tests/test_build_level4_routing.py::test_s041_s043_simple_existing_file_uses_local_executor",
    "S044": ("tests/test_build_level4_routing.py", "S044"),
    "S045": ("tests/test_build_level4_routing.py", "S045"),
    "S042": "tests/test_build_level4_constraints.py::test_s042_complex_task_excludes_model_without_required_capability",
    "S046": "tests/test_build_level4_constraints.py::test_s046_insufficient_hard_budget_prevents_metered_dispatch",
    "S047": "tests/test_build_level4_constraints.py::test_s047_explicit_provider_model_override_is_used_and_recorded",
    "S048": "tests/test_build_level4_constraints.py::test_s048_incompatible_explicit_model_is_rejected_before_execution",
    "S049": "tests/test_build_level4_routing.py::test_s049_recoverable_failure_uses_approved_fallback_within_attempt_cap",
    "S050": "tests/test_build_level4_routing.py::test_s050_opaque_cli_usage_keeps_unknown_cost",
    "S051": "tests/test_build_level5_execution.py::test_s051_implement_arithmetic_against_independent_tests",
    "S052": "tests/test_build_level5_execution.py::test_s052_fix_incorrect_function_and_pass_existing_tests",
    "S053": "tests/test_build_level5_execution.py::test_s053_added_unit_test_runs_and_rejects_mutant",
    "S054": "tests/test_build_level5_execution.py::test_s054_syntax_error_fails_verification",
    "S055": "tests/test_build_level5_execution.py::test_s055_type_error_fails_configured_static_check",
    "S056": "tests/test_build_level5_execution.py::test_s056_success_text_without_change_is_not_completion",
    "S057": "tests/test_build_level5_execution.py::test_s057_out_of_scope_file_is_rejected",
    "S058": "tests/test_build_level5_execution.py::test_s058_long_validation_reports_progress_times_out_and_cancels",
    "S059": "tests/test_build_level5_execution.py::test_s059_cancel_running_task_keeps_partial_work_isolated",
    "S060": "tests/test_build_level5_execution.py::test_s060_final_project_regression_blocks_apply",
    "S061": "tests/test_build_level6_repair.py::test_s061_one_failed_implementation_then_verified_repair",
    "S062": "tests/test_build_level6_repair.py::test_s062_second_bad_attempt_is_rejected_before_third_repair",
    "S063": "tests/test_build_level6_repair.py::test_s063_identical_validation_failures_stop_without_fourth_attempt",
    "S064": "tests/test_build_level6_repair.py::test_s064_missing_import_is_repaired_with_test_evidence",
    "S065": "tests/test_build_level6_repair.py::test_s065_wrong_return_type_is_corrected",
    "S066": "tests/test_build_level6_repair.py::test_s066_regression_from_repair_requires_third_verified_attempt",
    "S067": "tests/test_build_level6_repair.py::test_s067_broken_oracle_is_not_changed_to_make_bad_result_pass",
    "S068": "tests/test_build_level6_repair.py::test_s068_transient_outage_is_classified_and_uses_available_route",
    "S069": "tests/test_build_level6_repair.py::test_s069_scope_expansion_reapproves_and_preserves_completed_checkpoint",
    "S070": "tests/test_build_level6_repair.py::test_s070_retry_ceiling_preserves_failure_without_claiming_success",
    "S071": "tests/test_build_level7_kandev.py::test_s071_disabled_kandev_executes_internal_build_without_adapter_call",
    "S072": "tests/test_build_level7_kandev.py::test_s072_enabled_without_adapter_gives_setup_guidance_and_no_run",
    "S073": "tests/test_build_level7_kandev.py::test_s073_disconnected_or_unauthorized_kandev_is_not_ready",
    "S074": "tests/test_build_level7_kandev.py::test_s074_successful_read_only_probe_discovers_capabilities",
    "S075": "tests/test_kandev_run.py::test_selected_build_is_bound_to_kandev_before_dispatch_and_recovers",
    "S076": "tests/test_build_level7_kandev_board.py::test_s076_every_approved_task_has_stable_remote_identity_and_project",
    "S077": "tests/test_build_level7_kandev_board.py::test_s077_remote_dependency_graph_matches_approved_diamond",
    "S078": "tests/test_build_level7_kandev_board.py::test_s078_each_remote_task_uses_approved_profile_and_observed_model",
    "S079": "tests/test_build_level7_kandev_board.py::test_s079_remote_dependents_activate_after_parent_without_manual_move",
    "S080": "tests/test_build_level7_9_kandev_recovery.py::test_s080_remote_failure_requires_reconciliation_without_internal_fallback",
    "S081": "tests/test_build_level8_parallel.py::test_s081_s086_independent_tasks_overlap_and_integrate_distinct_worktrees",
    "S082": "tests/test_build_level8_dependencies.py::test_s082_successor_starts_after_parent_verification",
    "S083": "tests/test_build_level8_parallel.py::test_s083_s087_diamond_waits_for_verified_parent_artifacts",
    "S084": "tests/test_build_level8_parallel.py::test_s084_failed_parent_blocks_child_while_unrelated_sibling_finishes",
    "S085": "tests/test_build_level8_parallel.py::test_s085_conflicting_same_file_edits_do_not_silently_overwrite",
    "S086": "tests/test_build_level8_parallel.py::test_s081_s086_independent_tasks_overlap_and_integrate_distinct_worktrees",
    "S087": "tests/test_build_level8_parallel.py::test_s083_s087_diamond_waits_for_verified_parent_artifacts",
    "S088": "tests/test_build_level8_parallel.py::test_s088_s089_ready_child_starts_while_slow_branch_remains_running",
    "S089": "tests/test_build_level8_parallel.py::test_s088_s089_ready_child_starts_while_slow_branch_remains_running",
    "S090": "tests/test_build_level8_parallel.py::test_s090_twenty_task_mixed_graph_runs_each_task_once_after_prerequisites",
    "S091": "tests/test_build_level9_recovery.py::test_s091_pending_approval_survives_restart_without_launch",
    "S092": "tests/test_build_level9_recovery.py::test_s092_interrupted_task_reconciles_dirty_worktree_without_claiming_success",
    "S093": "tests/test_build_level9_recovery.py::test_s093_restart_preserves_first_checkpoint_and_finishes_five_task_plan",
    "S094": "tests/test_build_level7_9_kandev_recovery.py::test_s094_disconnect_mid_build_preserves_remote_identity_and_recovers",
    "S095": "tests/test_build_level7_9_kandev_recovery.py::test_s095_duplicate_completion_and_event_cursors_do_not_duplicate_acceptance",
    "S096": "tests/test_build_level7_9_kandev_recovery.py::test_s096_delayed_running_status_cannot_revert_accepted_completion",
    "S097": "tests/test_build_level9_recovery.py::test_s097_interrupted_repair_preserves_attempt_history_and_requires_approval",
    "S098": "tests/test_build_level9_recovery.py::test_s098_unverified_checkpoint_blocks_replay_and_preserves_evidence",
    "S099": "tests/test_build_level9_recovery.py::test_s099_discard_removes_only_selected_managed_worktree",
    "S100": "tests/test_build_level9_recovery.py::test_s093_restart_preserves_first_checkpoint_and_finishes_five_task_plan",
    "S101": "tests/test_build_level10_security.py::test_s101_path_traversal_write_cannot_escape_approved_worktree",
    "S102": "tests/test_build_level10_security.py::test_s102_symlink_escape_is_denied_for_reads_and_writes",
    "S103": "tests/test_build_level10_security.py::test_s103_repository_instruction_cannot_grant_external_tool_permission",
    "S104": "tests/test_build_level10_security.py::test_s104_credential_paths_are_not_readable_or_searchable",
    "S105": "tests/test_build_level10_security.py::test_s105_runner_credential_error_is_redacted_from_persisted_status_and_logs",
    "S106": "tests/test_build_level10_security.py::test_s106_unapproved_external_action_never_dispatches",
    "S108": "tests/test_build_level10_ui.py::test_s108_s109_pause_resume_and_chat_switch_keep_completed_work",
    "S109": "tests/test_build_level10_ui.py::test_s108_s109_pause_resume_and_chat_switch_keep_completed_work",
    "S110": "tests/test_build_level10_ui.py::test_s110_out_of_order_rapid_events_follow_authoritative_task_state",
    "S111": "tests/test_build_level11_projects.py::test_s111_add_small_feature_to_existing_application",
    "S112": "tests/test_build_level11_projects.py::test_s112_api_endpoint_validates_inputs_and_preserves_health_route",
    "S113": "tests/test_build_level11_projects.py::test_s113_frontend_form_calls_existing_api_and_displays_result",
    "S114": "tests/test_build_level11_projects.py::test_s114_database_migration_and_application_query_agree",
    "S115": "tests/test_build_level11_projects.py::test_s115_refactor_preserves_public_pricing_behavior",
    "S116": "tests/test_build_level11_projects.py::test_s116_full_stack_sum_feature_passes_backend_and_browser_contracts",
    "S117": "tests/test_build_level11_projects.py::test_s117_cross_module_timezone_bug_requires_feedback_guided_repair",
    "S118": "tests/test_build_level11_projects.py::test_s118_conflicting_requirement_during_execution_stops_unapproved_successor",
    "S119": "tests/test_build_level7_kandev_board.py::test_s119_large_kandev_board_repairs_defect_and_verifies_integrated_project",
    "S120": "tests/test_build_level7_kandev_board.py::test_s120_adversarial_board_recovers_and_verifies_feature_without_duplicate_launch",
    "S107": ("tests/test_build_plan_approval.py", "S107"),
}


def catalog() -> dict[str, dict]:
    level = None
    items: dict[str, dict] = {}
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        match = re.match(r"## LEVEL (\d+) — (.+)", line)
        if match:
            level = int(match.group(1))
        match = re.match(r"\*\*(S\d{3}) — (.+?)\.\*\*", line)
        if match:
            items[match.group(1)] = {
                "id": match.group(1), "level": level, "title": match.group(2),
                "category": "kandev" if 71 <= int(match.group(1)[1:]) <= 80 else
                            "repair" if 61 <= int(match.group(1)[1:]) <= 70 else "general",
            }
    if len(items) != 120:
        raise ValueError(f"Scenario specification contains {len(items)} IDs, expected 120")
    return items


def selected_ids(args, items: dict[str, dict]) -> list[str]:
    if args.id:
        if args.id not in items:
            raise ValueError(f"Unknown scenario ID: {args.id}")
        return [args.id]
    if args.level is not None:
        return [key for key, item in items.items() if item["level"] == args.level]
    if args.through_level is not None:
        return [key for key, item in items.items() if item["level"] <= args.through_level]
    if args.kandev:
        return [key for key, item in items.items() if item["category"] == "kandev"]
    if args.repair:
        return [key for key, item in items.items() if item["category"] == "repair"]
    if args.failed:
        latest = ARTIFACTS / "latest.json"
        if not latest.is_file():
            raise ValueError("No previous scenario report exists")
        previous = json.loads(latest.read_text(encoding="utf-8"))
        return [item["id"] for item in previous["scenarios"] if item["outcome"] == "FAIL"]
    if args.live:
        return []  # No live cases are registered or silently substituted.
    return list(items)


def run_case(scenario_id: str, item: dict, directory: Path) -> dict:
    record = dict(item)
    record.update({
        "test": CASES.get(scenario_id), "outcome": "NOT_RUN", "elapsed_seconds": 0,
        "resource_usage": "unknown", "approval_revision": "asserted by test when applicable",
        "evidence": "", "reason": "No executable scenario case is registered",
    })
    node = CASES.get(scenario_id)
    if node is None:
        return record
    pytest_args = [node[0], "-k", node[1]] if isinstance(node, tuple) else [node]
    xml_path = directory / f"{scenario_id}.xml"
    log_path = directory / f"{scenario_id}.txt"
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *pytest_args, f"--junitxml={xml_path}"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=180,
        )
        output = completed.stdout + completed.stderr
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        output = stdout + stderr + "\nScenario timed out after 180 seconds\n"
        exit_code = 124
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    log_path.write_text(output, encoding="utf-8")
    record["evidence"] = str(log_path.relative_to(ROOT))
    if xml_path.is_file():
        suite = ET.parse(xml_path).getroot()
        tests = list(suite.iter("testcase"))
        record["assertion_tests"] = [test.attrib.get("name", "") for test in tests]
        record["assertion_count"] = len(tests)
        record["junit"] = str(xml_path.relative_to(ROOT))
    record["outcome"] = "PASS" if exit_code == 0 and record.get("assertion_count", 0) > 0 else "FAIL"
    record["reason"] = "Assertions passed" if record["outcome"] == "PASS" else f"pytest exit {exit_code}; inspect evidence"
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--id")
    selection.add_argument("--level", type=int)
    selection.add_argument("--through-level", type=int)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--failed", action="store_true")
    selection.add_argument("--kandev", action="store_true")
    selection.add_argument("--repair", action="store_true")
    selection.add_argument("--live", action="store_true")
    args = parser.parse_args()
    items = catalog()
    try:
        selected = selected_ids(args, items)
    except ValueError as exc:
        parser.error(str(exc))
    directory = ARTIFACTS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True, exist_ok=False)
    results = [run_case(scenario_id, items[scenario_id], directory) for scenario_id in selected]
    counts = {status: sum(item["outcome"] == status for item in results)
              for status in ("PASS", "FAIL", "BLOCKED", "NOT_RUN")}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selection": vars(args), "counts": counts, "scenarios": results,
    }
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    (directory / "report.json").write_text(payload, encoding="utf-8")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "latest.json").write_text(payload, encoding="utf-8")
    lines = ["# Build scenario run", "", f"Generated: {report['generated_at']}", "",
             "| PASS | FAIL | BLOCKED | NOT_RUN |", "| ---: | ---: | ---: | ---: |",
             f"| {counts['PASS']} | {counts['FAIL']} | {counts['BLOCKED']} | {counts['NOT_RUN']} |", "",
             "| ID | Level | Result | Evidence |", "| --- | ---: | --- | --- |"]
    lines.extend(f"| {item['id']} | {item['level']} | {item['outcome']} | {item['evidence'] or item['reason']} |"
                 for item in results)
    (directory / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Build scenarios: {counts}; report: {directory / 'report.json'}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
