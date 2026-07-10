#!/usr/bin/env python3
"""Deterministic local secret scan for fixtures and generated run bundles."""

from __future__ import annotations

import re
import sys
from pathlib import Path


PATTERNS = (
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("openai_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("bearer_token", re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/-]{16,}\b", re.I)),
    (
        "literal_credential",
        re.compile(
            rb"(?i)[\"']?(?:api[_-]?key|access[_-]?token|password|secret)[\"']?\s*[:=]\s*[\"'](?!\*{3}REDACTED\*{3}[\"'])[^\"'\r\n]{8,}[\"']"
        ),
    ),
)
TEXT_SUFFIXES = {
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".txt", ".log",
    ".md", ".html", ".js", ".ts", ".diff", ".patch", ".env",
}
IGNORED_PARTS = {".git", "node_modules", ".venv", "dist", "build", "__pycache__"}


def _files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not IGNORED_PARTS.intersection(path.parts)
        and (path.suffix.lower() in TEXT_SUFFIXES or not path.suffix)
    )


def scan(roots: list[Path]) -> list[str]:
    findings: list[str] = []
    for root in sorted(path.resolve() for path in roots):
        if not root.exists():
            findings.append(f"missing_path:{root}")
            continue
        for path in _files(root):
            data = path.read_bytes()
            for name, pattern in PATTERNS:
                for match in pattern.finditer(data):
                    line = data.count(b"\n", 0, match.start()) + 1
                    findings.append(f"{path}:{line}:{name}")
    return findings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    roots = [Path(value) for value in args]
    if not roots:
        print("usage: check-secrets.py PATH [PATH ...]", file=sys.stderr)
        return 2
    findings = scan(roots)
    if findings:
        print("Secret scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print(f"Secret scan passed: {len(roots)} root(s), 0 findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
