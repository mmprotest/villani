# M0 Measured Baseline

## Capture environment

- UTC timestamp: `2026-07-10T00:58:42Z`
- Operating system: Microsoft Windows `10.0.26200.8655`, X64
- `python --version`: `Python 3.10.11`
- Root virtual environment: `Python 3.12.10` at `.venv\Scripts\python.exe`
- Interpreter selection: Python 3.11 was not installed, so the permitted Python 3.12 fallback was used.
- `node --version`: `v24.13.0`
- `npm --version`: `11.6.2`
- Windows command note: `npm.cmd` is the executable equivalent used for npm commands because the PowerShell `npm.ps1` shim was blocked by the current execution policy.

## Installation

Commands were run from the repository root unless a working directory is stated.

- `py -3.12 -m venv .venv`: exit code 0.
- `.\.venv\Scripts\python.exe -m pip install -e ".\components\villani-code[dev]" -e ".\components\villani-ops[test]"`: exit code 0; both projects installed in editable mode with their development or test extras.
- In `components/villani-flight-recorder`, `npm.cmd ci`: exit code 0; 110 packages installed from `package-lock.json`.

## Verification results

### Villani Code

- Working directory: `components/villani-code`
- Command: `..\..\.venv\Scripts\python.exe -m pytest -q`
- Exit code: 1
- Counts: 604 passed, 66 failed, 0 errors, 1 skipped; 27 warnings.
- Observed failure: 66 tests failed across adapter telemetry and patch behavior, command-environment handling, planning and memory behavior, and TUI/controller contracts.

Failing test node IDs:

- `tests/test_agents_claude_adapter_run.py::test_claude_adapter_detects_workspace_edit_without_stdout_diff`
- `tests/test_agents_claude_adapter_run.py::test_claude_adapter_uses_hook_events_for_telemetry`
- `tests/test_agents_claude_adapter_run.py::test_claude_adapter_stdout_diff_fallback_applies_patch`
- `tests/test_agents_claude_adapter_run.py::test_claude_adapter_noop_keeps_no_patch_signals`
- `tests/test_agents_claude_adapter_run.py::test_claude_adapter_writes_stdout_result_summary`
- `tests/test_agents_claude_adapter_run.py::test_claude_adapter_captures_failed_bash_hook`
- `tests/test_benchmark_agents.py::test_opencode_run_agent_writes_project_config_for_base_url`
- `tests/test_command_environment.py::test_discovers_private_root_from_runner_owned_directory_variable`
- `tests/test_command_environment.py::test_discovers_private_root_from_runner_owned_executable_variable`
- `tests/test_command_environment.py::test_private_path_discovered_from_environment_is_removed`
- `tests/test_command_environment.py::test_direct_private_path_variable_discovered_from_environment_is_removed`
- `tests/test_command_environment.py::test_building_child_environment_does_not_modify_runner_environment`
- `tests/test_command_environment.py::test_command_environment_diagnostics_are_artifact_only`
- `tests/test_command_environment.py::test_runner_runtime_path_is_removed_while_system_toolchains_remain`
- `tests/test_interactive_approval_dialog.py::test_approval_keys_are_contained_and_resolve_selection`
- `tests/test_interactive_approval_dialog.py::test_enter_submits_normally_after_approval_resolution`
- `tests/test_interactive_approval_dialog.py::test_escape_denies_and_restores_focus`
- `tests/test_interactive_tokens.py::test_interactive_shell_is_textual_wrapper`
- `tests/test_loop.py::test_tool_result_followup_is_pure_tool_result_message`
- `tests/test_loop.py::test_anthropic_message_order_after_tool_use_with_pending_verification`
- `tests/test_loop.py::test_loop_does_not_append_duplicate_validation_summary_when_dedup_returns_empty`
- `tests/test_mcp.py::test_mcp_precedence_and_env_expansion`
- `tests/test_mission_state_runtime.py::test_new_mission_id_unique_for_rapid_calls`
- `tests/test_mission_state_runtime.py::test_autonomous_summary_mirrors_to_mission_state`
- `tests/test_plan_workflow.py::test_runner_plan_summary_does_not_include_planning_boilerplate`
- `tests/test_plan_workflow.py::test_runner_plan_for_repo_review_has_concrete_steps_without_generic_questions`
- `tests/test_plan_workflow.py::test_runner_plan_records_real_file_evidence`
- `tests/test_plan_workflow.py::test_plan_uses_runtime_loop_for_recovered_artifact`
- `tests/test_plan_workflow.py::test_plan_runtime_prompt_contains_multi_file_evidence`
- `tests/test_plan_workflow.py::test_plan_inline_prompt_starts_planning_immediately`
- `tests/test_plan_workflow.py::test_bare_plan_enters_prompt_awaiting_mode`
- `tests/test_plan_workflow.py::test_ready_plan_does_not_hijack_future_normal_prompts`
- `tests/test_plan_workflow.py::test_execute_runs_last_ready_plan`
- `tests/test_plan_workflow.py::test_execute_fails_cleanly_when_plan_unresolved`
- `tests/test_plan_workflow.py::test_clarification_options_are_logged_to_transcript`
- `tests/test_plan_workflow.py::test_plan_payload_dicts_are_normalized_for_clean_rendering`
- `tests/test_plan_workflow.py::test_repo_review_prompt_defaults_to_ready_plan_without_clarification`
- `tests/test_plan_workflow.py::test_runner_plan_inspects_real_repo_files_not_only_repo_map`
- `tests/test_plan_workflow.py::test_apply_plan_result_renders_plain_english_without_raw_json`
- `tests/test_plan_workflow.py::test_apply_plan_result_for_non_ready_plan_does_not_log_execute_hint`
- `tests/test_plan_workflow.py::test_plan_flow_from_mixed_narrative_json_renders_only_final_plan`
- `tests/test_plan_workflow.py::test_greenfield_plan_with_single_candidate_file_is_accepted`
- `tests/test_planning.py::test_planning_uses_submit_plan_artifact_not_text_json`
- `tests/test_planning.py::test_generic_plan_artifact_is_rejected`
- `tests/test_runner_controller_contract.py::test_plan_and_execute_paths_use_canonical_runner_contract`
- `tests/test_runner_controller_contract.py::test_missing_required_runner_method_fails_early`
- `tests/test_task_memory.py::test_current_state_search_and_recent_retrieval_tools`
- `tests/test_task_memory.py::test_memory_tool_result_is_the_only_retrieved_memory_added_to_messages`
- `tests/test_ui_integration.py::test_tui_constructs_with_runner`
- `tests/test_ui_integration.py::test_tui_uses_textual_css_file`
- `tests/test_ui_integration.py::test_enter_submit_path_calls_controller_without_global_enter_binding`
- `tests/test_ui_integration.py::test_ai_stream_starts_on_fresh_line`
- `tests/test_ui_integration.py::test_transcript_preserves_markup_like_stream_text_literal`
- `tests/test_ui_integration.py::test_transcript_uses_wrapping_scroll_container_not_log_widget`
- `tests/test_ui_integration.py::test_space_key_inserts_space_in_input`
- `tests/test_ui_integration.py::test_copy_console_binding_copies_current_console_text`
- `tests/test_ui_integration.py::test_copy_console_preserves_multiline_content`
- `tests/test_ui_integration.py::test_copy_console_success_posts_status_update`
- `tests/test_ui_integration.py::test_copy_console_failure_is_handled_without_crash`
- `tests/test_ui_slash_commands.py::test_slash_commands_are_intercepted_and_unknown_is_local`
- `tests/test_ui_slash_commands.py::test_help_output_lists_supported_slash_commands`
- `tests/test_ui_slash_commands.py::test_normal_prompt_flow_still_calls_run_prompt`
- `tests/test_ui_slash_commands.py::test_slash_popup_visibility_and_filtering`
- `tests/test_ui_slash_commands.py::test_slash_popup_keyboard_controls`
- `tests/test_villani_hygiene.py::test_low_authority_paths_are_blocked_from_mutation`
- `tests/test_villani_mode.py::test_villani_mode_startup_without_prompt`

### Villani Ops

- Working directory: `components/villani-ops`
- Command: `..\..\.venv\Scripts\python.exe -m pytest -q`
- Exit code: 1
- Counts: 539 passed, 32 failed, 0 errors, 0 skipped, 114 deselected.
- Observed failure: 32 tests failed across classification context, lifecycle and materialization, validation command parsing, viewer rendering, and Villani Code runner behavior.

Failing test node IDs:

- `villani_ops/tests/test_classification_context.py::test_classifier_context_includes_relevant_file_snippets`
- `villani_ops/tests/test_controller_lifecycle_hardening.py::test_happy_path_full_lifecycle_and_report`
- `villani_ops/tests/test_controller_lifecycle_hardening.py::test_retry_lifecycle_records_retrying`
- `villani_ops/tests/test_controller_lifecycle_hardening.py::test_escalation_lifecycle_records_escalating`
- `villani_ops/tests/test_controller_lifecycle_hardening.py::test_human_accept_requested_by_reviewer_overrides_nonzero`
- `villani_ops/tests/test_final_hardening.py::test_valid_human_override_accepts_nonzero_and_uncertain`
- `villani_ops/tests/test_final_hardening.py::test_pr_prepare_success_push_and_gh_failures_branch_dirty_patch`
- `villani_ops/tests/test_v02_hardening.py::test_villani_code_receives_key_and_saves_redacted_command`
- `villani_ops/tests/test_validation_command_parsing.py::test_quoted_command_parses_correctly`
- `villani_ops/tests/test_validation_command_parsing.py::test_validation_plan_required_optional_and_artifacts`
- `villani_ops/tests/test_validation_command_parsing.py::test_required_command_failure_fails_aggregate`
- `villani_ops/tests/test_verifier_orchestrator_materialization.py::test_worktree_cleanup_preserves_selected_patch_and_materializes`
- `villani_ops/tests/test_verifier_orchestrator_materialization.py::test_trace_fields_are_distinct`
- `villani_ops/tests/test_viewer.py::test_offline_viewer_embeds_self_contained_snapshot`
- `villani_ops/tests/test_viewer.py::test_offline_viewer_contains_required_ui_without_external_deps`
- `villani_ops/tests/test_viewer.py::test_decision_summary_and_warnings_render_in_offline_html`
- `villani_ops/tests/test_viewer.py::test_provider_failure_graph_and_timeline_truthful`
- `villani_ops/tests/test_viewer.py::test_header_uses_decision_and_cost_reasons_visible`
- `villani_ops/tests/test_viewer.py::test_candidate_evidence_patch_aliases_and_changed_file_aliases`
- `villani_ops/tests/test_viewer.py::test_execution_graph_demoted_below_evidence_sections`
- `villani_ops/tests/test_viewer.py::test_decision_summary_labels_are_humanized_and_raw_state_preserved`
- `villani_ops/tests/test_viewer.py::test_usage_duplicate_summaries_are_not_double_counted`
- `villani_ops/tests/test_viewer.py::test_candidate_lane_graph_contains_statuses_and_hooks`
- `villani_ops/tests/test_viewer.py::test_graph_detail_card_readable_before_raw_json`
- `villani_ops/tests/test_viewer.py::test_decision_economics_and_why_winner_for_candidate_run`
- `villani_ops/tests/test_viewer.py::test_decision_economics_unavailable_for_no_candidate_failure_and_duration_reason`
- `villani_ops/tests/test_villani_code_prompt_file.py::test_long_villani_code_prompt_uses_task_file_not_argv`
- `villani_ops/tests/test_villani_code_runner.py::test_openai_compatible_maps_to_openai_for_villani_code_cli`
- `villani_ops/tests/test_villani_code_runner.py::test_openai_remains_openai_for_villani_code_cli`
- `villani_ops/tests/test_villani_code_runner.py::test_anthropic_remains_anthropic_for_villani_code_cli`
- `villani_ops/tests/test_villani_code_runner.py::test_debug_flags_and_api_key_redaction_remain_present`
- `villani_ops/tests/test_villani_code_runner.py::test_villani_code_runner_timeout_kills_child_process_group`

### Villani Flight Recorder tests

- Working directory: `components/villani-flight-recorder`
- Command: `npm.cmd test`
- Exit code: 1
- Counts: 56 passed, 6 failed, 0 errors, 0 skipped; 15 test files passed and 2 test files failed.
- Observed failure: three CLI replay tests reported `spawn npm ENOENT`, and three strict-provider scanner tests did not find the expected provider path segment.

Failing test IDs:

- `test/cliReplay.test.ts > CLI replay output workflow > replay --session --out <dir> writes index.html to the requested directory`
- `test/cliReplay.test.ts > CLI replay output workflow > replay --latest --root searches the custom root`
- `test/cliReplay.test.ts > CLI replay output workflow > replay --latest --root strictly filters provider sessions`
- `test/scanner.test.ts > strict provider scanning > scan --provider codex only returns confident codex sessions`
- `test/scanner.test.ts > strict provider scanning > scan --provider claude only returns confident claude sessions`
- `test/scanner.test.ts > strict provider scanning > scan --provider pi only returns confident pi sessions`

### Villani Flight Recorder typecheck

- Working directory: `components/villani-flight-recorder`
- Command: `npm.cmd run typecheck`
- Exit code: 0
- Result: PASS; `tsc --noEmit` reported no diagnostics.
- Pass, fail, error, and skip test counts: not applicable because this command is not a test runner.

### Villani Flight Recorder build

- Working directory: `components/villani-flight-recorder`
- Command: `npm.cmd run build`
- Exit code: 0
- Result: PASS; `tsc` completed without diagnostics.
- Pass, fail, error, and skip test counts: not applicable because this command is not a test runner.

### Villani Flight Recorder format check

- Working directory: `components/villani-flight-recorder`
- Command: `npm.cmd run format:check`
- Exit code: 0
- Result: PASS; Prettier reported `All matched files use Prettier code style!`.
- Pass, fail, error, and skip test counts: not applicable because this command is not a test runner.

## Current CLI entry points

- `components/villani-code/pyproject.toml`: `villani-code = "villani_code.cli:app"`.
- `components/villani-ops/pyproject.toml`: `villani-ops = "villani_ops.cli.main:app"`.
- `components/villani-flight-recorder/package.json`: `villani-flight-recorder = "dist/cli.js"` and `vfr = "dist/cli.js"`.

## Current run storage directories

- Villani Ops defaults its workspace to `.villani-ops` relative to the invoking process and stores runs under `.villani-ops/runs/<run_id>`.
- Flight Recorder stores its session index at `$VFR_HOME/index.json` when `VFR_HOME` is set, otherwise at `~/.villani-flight-recorder/index.json`; indexed replay cache files are under the same base directory in `replays/`, with `session-browser.html` beside the index.
- Flight Recorder's standalone project-local replay output default is `.villani-flight-recorder/replays` relative to the invoking process.
- Flight Recorder currently scans Claude sessions under `~/.claude` and `~/.claude/projects`, Codex sessions under `$CODEX_HOME/sessions` when set plus `~/.codex` and `~/.codex/sessions`, and Pi sessions under `~/.pi` and `~/.pi/agent/sessions`.
