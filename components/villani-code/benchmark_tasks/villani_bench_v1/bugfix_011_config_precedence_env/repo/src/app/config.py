def resolve_timeout(default: int, file_value: int | None, env_value: int | None) -> int:
    if file_value is not None:
        return file_value
    if env_value is not None:
        return env_value
    return default
