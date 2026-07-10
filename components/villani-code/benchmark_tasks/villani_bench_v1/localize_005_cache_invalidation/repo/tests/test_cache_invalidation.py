from app.cache import get_value, update_value, format_value

def test_refresh_after_update():
    store = {'token': 'old'}
    assert get_value(store, 'token') == 'old'
    update_value(store, 'token', 'new')
    assert get_value(store, 'token') == 'new'

def test_unrelated_formatting_helper_still_works():
    assert format_value(' a ') == 'A'
