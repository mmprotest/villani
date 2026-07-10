from app.config import resolve_timeout

def test_env_overrides_file():
    assert resolve_timeout(10, file_value=20, env_value=30) == 30

def test_file_overrides_default_when_env_missing():
    assert resolve_timeout(10, file_value=20, env_value=None) == 20
