from __future__ import annotations

from pathlib import Path

from villani_code.context_ledger import ContextLedger


def _tool_pair(tool_use_id: str, content: str) -> list[dict[str, object]]:
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Read",
                    "input": {"file_path": "large.txt"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }
            ],
        },
    ]


def test_context_projection_excludes_superseded_full_file_content(
    tmp_path: Path,
) -> None:
    ledger = ContextLedger(
        repo=tmp_path,
        task="Preserve this exact task",
        max_projection_chars=20_000,
    )
    full = "FULL-FILE-CONTENT\n" * 50
    excerpt = "relevant line\n"
    ledger.record_tool_result(
        tool_name="Read",
        arguments={
            "file_path": "large.txt",
            "start_line": None,
            "end_line": None,
        },
        tool_use_id="full",
        content=full,
        repository_state_digest="same-state",
        turn=1,
        result_id="tool-result-0001",
    )
    ledger.record_tool_result(
        tool_name="Read",
        arguments={
            "file_path": "large.txt",
            "start_line": 20,
            "end_line": 30,
        },
        tool_use_id="range",
        content=excerpt,
        repository_state_digest="same-state",
        turn=2,
        result_id="tool-result-0002",
    )
    ledger.add(
        source_type="candidate_patch",
        source_reference="git-diff",
        content="diff --git a/x b/x\n+latest patch\n",
        repository_state_digest="same-state",
        turn=2,
        relevance_score=1.0,
    )
    ledger.record_validation(
        content="latest validation passed",
        repository_state_digest="same-state",
        turn=2,
    )
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "Preserve this exact task"}],
        },
        *_tool_pair("full", full),
        *_tool_pair("range", excerpt),
    ]

    projected = ledger.project_messages(messages)
    rendered = str(projected)

    assert "Preserve this exact task" in rendered
    assert "FULL-FILE-CONTENT" not in rendered
    assert "context reference" in rendered
    assert "relevant line" in rendered
    active_types = {item.source_type for item in ledger.active_items()}
    assert {"candidate_patch", "validation", "file_excerpt"} <= active_types


def test_context_compaction_substantially_reduces_repeated_payloads(
    tmp_path: Path,
) -> None:
    ledger = ContextLedger(
        repo=tmp_path,
        task="Find target symbol",
        max_projection_chars=2_500,
    )
    messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "Find target symbol"}],
        }
    ]
    naive_chars = 0
    for index in range(8):
        content = (f"payload-{index}\n" + "x" * 2_000)
        tool_id = f"read-{index}"
        ledger.record_tool_result(
            tool_name="Read",
            arguments={
                "file_path": "large.txt",
                "start_line": index * 10 + 1,
                "end_line": index * 10 + 10,
            },
            tool_use_id=tool_id,
            content=content,
            repository_state_digest="stable",
            turn=index + 1,
            result_id=f"tool-result-{index:04d}",
        )
        messages.extend(_tool_pair(tool_id, content))
        naive_chars += len(content)

    projected = ledger.project_messages(messages)
    projected_chars = len(str(projected))

    assert projected_chars < naive_chars * 0.5
    telemetry = ledger.telemetry()
    assert telemetry["context_items_compacted"] > 0
    assert telemetry["tokens_removed_by_compaction"] > 0
    assert (
        telemetry["estimated_tokens_after_projection"]
        < telemetry["estimated_tokens_before_projection"]
    )

