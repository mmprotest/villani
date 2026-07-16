from __future__ import annotations

import json
from pathlib import Path

from villani_code.context_ledger import ContextLedger
from villani_code.progress_telemetry import UsefulProgressTracker
from villani_code.state_tooling import execute_tool_with_lifecycle
from villani_code.tool_result_ledger import ToolResultLedger
from villani_code.tools import execute_tool, tool_specs


def _decoded(result: dict[str, object]) -> dict[str, object]:
    assert result["is_error"] is False
    return json.loads(str(result["content"]))


def test_line_range_read_returns_numbered_unicode_lines(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text(
        "first\ncafé\n東京\nlast\n",
        encoding="utf-8",
        newline="\n",
    )

    payload = _decoded(
        execute_tool(
            "Read",
            {
                "file_path": "sample.txt",
                "start_line": 2,
                "end_line": 3,
            },
            tmp_path,
        )
    )

    assert payload["start_line"] == 2
    assert payload["end_line"] == 3
    assert payload["total_lines"] == 4
    assert payload["lines"] == [
        {"line": 2, "text": "café"},
        {"line": 3, "text": "東京"},
    ]
    assert len(str(payload["content_sha256"])) == 64
    assert payload["truncated"] is False


def test_grep_context_ranges_do_not_repeat_overlapping_lines(
    tmp_path: Path,
) -> None:
    (tmp_path / "sample.txt").write_text(
        "\n".join(
            [
                "zero",
                "before",
                "needle one",
                "shared",
                "needle two",
                "after",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = _decoded(
        execute_tool(
            "Grep",
            {
                "pattern": "needle",
                "path": ".",
                "before_context": 2,
                "after_context": 2,
                "max_results": 10,
                "max_output_chars": 20_000,
            },
            tmp_path,
        )
    )
    matches = payload["matches"]
    assert isinstance(matches, list)
    assert [item["line"] for item in matches] == [3, 5]
    context_lines = [
        context["line"]
        for item in matches
        for key in ("context_before", "context_after")
        for context in item[key]
    ]
    assert len(context_lines) == len(set(context_lines))
    assert all(len(item["file_sha256"]) == 64 for item in matches)


def test_symbol_search_uses_index_and_reference_search_is_lexical(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text(
        "def target_symbol():\n    return 1\n\nvalue = target_symbol()\n",
        encoding="utf-8",
    )
    (tmp_path / "notes.txt").write_text(
        "target_symbol is mentioned here\n",
        encoding="utf-8",
    )

    symbol = _decoded(
        execute_tool(
            "FindSymbol",
            {"symbol": "target_symbol", "path": ".", "limit": 5},
            tmp_path,
        )
    )
    references = _decoded(
        execute_tool(
            "FindReferences",
            {
                "symbol": "target_symbol",
                "path": ".",
                "limit": 20,
                "context_lines": 1,
            },
            tmp_path,
        )
    )

    assert symbol["results"][0]["path"] == "module.py"
    assert symbol["results"][0]["index_evidence"]["indexed_symbols"] == [
        "target_symbol"
    ]
    reference_types = {
        (item["path"], item["reference_type"])
        for item in references["references"]
    }
    assert ("module.py", "exact_definition") in reference_types
    assert ("notes.txt", "lexical_reference") in reference_types


def test_search_returns_bm25_reason_snippet_and_digest(tmp_path: Path) -> None:
    (tmp_path / "alpha.txt").write_text(
        "boring content\n",
        encoding="utf-8",
    )
    (tmp_path / "target.py").write_text(
        "def unusual_widget():\n    return 'bounded snippet'\n",
        encoding="utf-8",
    )

    payload = _decoded(
        execute_tool(
            "Search",
            {"query": "unusual_widget", "path": ".", "limit": 5},
            tmp_path,
        )
    )

    assert payload["results"][0]["path"] == "target.py"
    assert "symbol match" in payload["results"][0]["reason"]
    assert "bounded snippet" in payload["results"][0]["snippet"]
    assert len(payload["results"][0]["file_sha256"]) == 64


class _LifecycleRunner:
    def __init__(self, repo: Path):
        self.repo = repo
        self.unsafe = False
        self._debug_recorder = None
        self._task_memory = None
        self._tool_result_ledger = ToolResultLedger(repo)
        self._command_failure_ledger = None
        self._context_ledger = ContextLedger(repo=repo, task="inspect target")
        self._progress_tracker = UsefulProgressTracker(
            repo=repo,
            objective="inspect target",
        )
        self.events: list[dict[str, object]] = []

    def event_callback(self, event: dict[str, object]) -> None:
        self.events.append(event)

    def _build_tool_result_event_payload(
        self,
        tool_name: str,
        tool_use_id: str,
        result: dict[str, object],
    ) -> dict[str, object]:
        return {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            **result,
        }


def test_repeated_read_and_grep_reuse_results_until_file_changes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("needle\nsecond\n", encoding="utf-8")
    runner = _LifecycleRunner(tmp_path)

    first = execute_tool_with_lifecycle(
        runner=runner,
        tool_name="Read",
        tool_input={"file_path": "target.txt", "start_line": 1, "end_line": 1},
        tool_use_id="read-1",
        turn_index=1,
    )
    second = execute_tool_with_lifecycle(
        runner=runner,
        tool_name="Read",
        tool_input={"file_path": "target.txt", "start_line": 1, "end_line": 1},
        tool_use_id="read-2",
        turn_index=2,
    )
    grep_first = execute_tool_with_lifecycle(
        runner=runner,
        tool_name="Grep",
        tool_input={"pattern": "needle", "path": "."},
        tool_use_id="grep-1",
        turn_index=3,
    )
    grep_second = execute_tool_with_lifecycle(
        runner=runner,
        tool_name="Grep",
        tool_input={"pattern": "needle", "path": "."},
        tool_use_id="grep-2",
        turn_index=4,
    )

    assert "content_sha256" in str(first["content"])
    assert json.loads(str(second["content"]))["unchanged"] is True
    assert json.loads(str(grep_second["content"]))["unchanged"] is True
    assert "matches" in str(grep_first["content"])

    target.write_text("changed\nsecond\n", encoding="utf-8")
    fresh = execute_tool_with_lifecycle(
        runner=runner,
        tool_name="Read",
        tool_input={"file_path": "target.txt", "start_line": 1, "end_line": 1},
        tool_use_id="read-3",
        turn_index=5,
    )
    assert json.loads(str(fresh["content"]))["lines"][0]["text"] == "changed"
    assert runner._tool_result_ledger.telemetry() == {
        "duplicate_tool_results": 2,
        "duplicate_file_reads": 1,
        "duplicate_searches": 1,
        "unique_files_read": 1,
    }


def test_existing_tool_inputs_remain_valid_and_refresh_is_explicit() -> None:
    schemas = {item["name"]: item["input_schema"] for item in tool_specs()}

    assert "file_path" in schemas["Read"]["required"]
    assert "start_line" not in schemas["Read"]["required"]
    assert schemas["Read"]["properties"]["include_line_numbers"]["default"] is True
    assert schemas["Read"]["properties"]["refresh"]["default"] is False
    assert "FindSymbol" in schemas
    assert "FindReferences" in schemas
    assert "PatchRange" in schemas


def test_useful_progress_telemetry_is_deterministic(tmp_path: Path) -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    tracker = UsefulProgressTracker(
        repo=tmp_path,
        objective="Change src/target.txt",
        clock=clock,
    )
    tracker.set_known_relevant(["src/target.txt"])
    tracker.start_tool_call()
    tracker.record_turn(1)
    now[0] = 1.25
    tracker.observe_read("src/target.txt")
    tracker.record_tokens(80)
    tracker.start_tool_call()
    tracker.record_turn(2)
    now[0] = 2.5
    tracker.observe_patch(
        "diff --git a/src/target.txt b/src/target.txt\n+changed\n",
        ["src/target.txt"],
    )
    tracker.record_tokens(20)
    tracker.record_turn(3)

    assert tracker.telemetry() == {
        "time_to_first_relevant_file": 1.25,
        "tool_calls_to_first_relevant_file": 1,
        "time_to_first_relevant_patch": 2.5,
        "tool_calls_to_first_relevant_patch": 2,
        "tokens_to_first_relevant_patch": 80,
        "unique_files_read": 1,
        "unique_relevant_files_read": 1,
        "files_read": ["src/target.txt"],
        "relevant_files_read": ["src/target.txt"],
        "tokens_after_last_relevant_progress": 20,
        "turns_after_last_relevant_progress": 1,
        "relevant_patch_revisions": 1,
        "validation_improvement_count": 0,
    }


def test_line_ending_only_patch_is_not_relevant_progress(
    tmp_path: Path,
) -> None:
    tracker = UsefulProgressTracker(
        repo=tmp_path,
        objective="Change src/target.txt",
    )
    tracker.set_known_relevant(["src/target.txt"])
    tracker.start_tool_call()
    tracker.observe_patch(
        "diff --git a/src/target.txt b/src/target.txt\n"
        "@@ -1 +1 @@\n"
        "-same\r\n"
        "+same\n",
        ["src/target.txt"],
    )
    tracker.observe_validation(["repository_validation_timeout"])
    tracker.observe_validation(["test_failure"])

    telemetry = tracker.telemetry()
    assert telemetry["relevant_patch_revisions"] == 0
    assert telemetry["validation_improvement_count"] == 1
