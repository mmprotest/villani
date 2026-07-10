_cache = {}

def format_value(value: str) -> str:
    return value.strip().upper()

def get_value(store: dict[str, str], key: str) -> str | None:
    if key in _cache:
        return _cache[key]
    value = store.get(key)
    _cache[key] = value
    return value

def update_value(store: dict[str, str], key: str, value: str) -> None:
    store[key] = value
