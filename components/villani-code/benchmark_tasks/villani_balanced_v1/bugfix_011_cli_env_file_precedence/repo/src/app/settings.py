from __future__ import annotations
import json, os
from pathlib import Path

def load_file_settings(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))

def resolve_mode(*, cli_mode: str | None, env: dict[str, str], config_path: str | None) -> str:
    file_settings = load_file_settings(config_path)
    if env.get("APP_MODE"):
        return env["APP_MODE"]
    if cli_mode:
        return cli_mode
    return file_settings.get("mode", "standard")
