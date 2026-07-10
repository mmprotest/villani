from app.http.headers import normalize_headers

def build_outgoing_headers(headers: dict[str, str]) -> dict[str, str]:
    return normalize_headers(headers)
