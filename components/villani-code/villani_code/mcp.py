from __future__ import annotations

import json
import os
import re
import warnings
from pathlib import Path
from typing import Any


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            expr = match.group(1)
            if ':-' in expr:
                var, default = expr.split(':-', 1)
                return os.getenv(var, default)
            return os.getenv(expr, '')
        return re.sub(r"\$\{([^}]+)\}", repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def load_mcp_config(repo: Path, managed_path: Path | None = None) -> dict[str, Any]:
    managed = _load_json(managed_path) if managed_path else {}
    user = _load_json(Path.home() / ".villani.json")
    project = _load_json(repo / ".mcp.json")
    local = _load_json(Path.home() / ".villani.local.json")
    merged = {}
    for layer in (managed, user, project, local):
        merged = _deep_merge(merged, _expand_env(layer))
    return merged


def _load_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        warnings.warn(f"Ignoring invalid JSON in {path}", RuntimeWarning, stacklevel=2)
        return {}
    if not isinstance(parsed, dict):
        warnings.warn(f"Ignoring non-object JSON in {path}", RuntimeWarning, stacklevel=2)
        return {}
    return parsed


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
