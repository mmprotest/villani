from __future__ import annotations

from pathlib import Path

from villani_code.tools import execute_tool


def test_patch_failure_emits_debug_failure_event(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []

    def debug_callback(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    result = execute_tool(
        "Patch",
        {
            "unified_diff": "--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-old\n+new\n",
        },
        tmp_path,
        debug_callback=debug_callback,
        tool_call_id="patch-1",
    )

    assert result["is_error"] is True
    assert any(e == "patch_applied" and payload.get("ok") is False for e, payload in events)
    failure_event = next(payload for e, payload in events if e == "patch_applied" and payload.get("ok") is False)
    assert failure_event["tool_call_id"] == "patch-1"
    assert "failure_reason" in failure_event
