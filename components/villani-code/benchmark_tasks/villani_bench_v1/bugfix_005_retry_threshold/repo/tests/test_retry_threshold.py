from app.retry import should_retry

def test_retries_429_and_500s():
    assert should_retry(429) is True
    assert should_retry(500) is True

def test_does_not_retry_regular_client_errors():
    assert should_retry(404) is False
    assert should_retry(418) is False
