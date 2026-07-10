def should_retry(status_code: int) -> bool:
    return status_code >= 500
