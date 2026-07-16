from __future__ import annotations

import hashlib
import json
from pathlib import Path

from villani_code.tools import execute_tool


def test_patch_rejects_stale_preimage_with_structured_guidance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("one\ntwo\n", encoding="utf-8", newline="\n")
    stale = hashlib.sha256(b"old content\n").hexdigest()

    result = execute_tool(
        "Patch",
        {
            "file_path": "sample.txt",
            "expected_sha256": stale,
            "unified_diff": (
                "--- a/sample.txt\n"
                "+++ b/sample.txt\n"
                "@@ -1,2 +1,2 @@\n"
                " one\n"
                "-two\n"
                "+changed\n"
            ),
        },
        tmp_path,
    )

    assert result["is_error"] is True
    payload = json.loads(str(result["content"]))
    assert payload["file"] == "sample.txt"
    assert payload["expected_digest"] == stale
    assert payload["actual_digest"] == hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    assert payload["failed_hunk"]["old_start"] == 1
    assert payload["nearest_context"]
    assert "Re-read" in payload["retry_guidance"]
    assert path.read_bytes() == b"one\ntwo\n"


def test_patch_range_preserves_lf_and_crlf(tmp_path: Path) -> None:
    for name, newline in (("lf.txt", b"\n"), ("crlf.txt", b"\r\n")):
        path = tmp_path / name
        path.write_bytes(newline.join([b"one", b"two", b"three", b""]))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()

        result = execute_tool(
            "PatchRange",
            {
                "file_path": name,
                "start_line": 2,
                "end_line": 2,
                "replacement": "changed",
                "expected_sha256": digest,
            },
            tmp_path,
        )

        assert result["is_error"] is False
        assert path.read_bytes() == newline.join(
            [b"one", b"changed", b"three", b""]
        )

