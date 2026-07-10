def forward_headers(headers: dict[str, str]) -> dict[str, str]:
    forwarded = {'x-request-id': headers.get('x-request-id', '')}
    return forwarded
