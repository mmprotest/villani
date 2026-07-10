from pathlib import Path

def resolve_config_path() -> str:
    return str(Path('settings.toml'))
