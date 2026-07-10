def build_url(path: str) -> str:
    return f"https://service.local/{path.lstrip('/')}"

def normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for key, value in headers.items():
        if key.lower() == 'authorization':
            continue
        out[key.lower()] = value
    return out
