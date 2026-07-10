from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-secrets.py"


def _scanner():
    spec = importlib.util.spec_from_file_location("villani_secret_scan", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_scan_accepts_redacted_bundle_and_rejects_literal_secret(
    tmp_path: Path,
) -> None:
    scan = _scanner().scan
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "event.jsonl").write_text(
        '{"api_key":"***REDACTED***","source":"OPENAI_API_KEY"}\n',
        encoding="utf-8",
    )
    assert scan([safe]) == []
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    (unsafe / "bundle.json").write_text(
        '{"api_key":"sk-this-is-a-deterministic-fake-secret"}',
        encoding="utf-8",
    )
    findings = scan([unsafe])
    assert len(findings) == 2
    assert [finding.rsplit(":", 1)[-1] for finding in findings] == [
        "openai_key",
        "literal_credential",
    ]
