from app.devscript import resolve_config_path

def test_uses_project_config_directory():
    assert resolve_config_path().replace('\\', '/') == 'config/settings.toml'

def test_returns_relative_project_path():
    assert resolve_config_path().startswith('config/')
