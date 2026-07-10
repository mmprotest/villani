import json
from app.settings import resolve_mode

def test_cli_overrides_env_and_file(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'mode': 'file'}), encoding='utf-8')
    result = resolve_mode(cli_mode='cli', env={'APP_MODE': 'env'}, config_path=str(config))
    assert result == 'cli'

def test_env_overrides_file(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'mode': 'file'}), encoding='utf-8')
    result = resolve_mode(cli_mode=None, env={'APP_MODE': 'env'}, config_path=str(config))
    assert result == 'env'

def test_default_used_when_missing(tmp_path):
    result = resolve_mode(cli_mode=None, env={}, config_path=str(tmp_path / 'missing.json'))
    assert result == 'standard'
