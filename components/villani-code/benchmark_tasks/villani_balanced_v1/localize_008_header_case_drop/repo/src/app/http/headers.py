def normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    normalized = {}
    for key, value in headers.items():
        normalized[key.lower()] = value.strip().lower()
    return normalized
