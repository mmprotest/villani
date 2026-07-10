from app.headers import normalize_headers, build_url

def test_preserves_authorization_header():
    out = normalize_headers({'Authorization': 'Bearer x', 'X-Trace': '1'})
    assert out['authorization'] == 'Bearer x'

def test_unrelated_url_helper_still_works():
    assert build_url('/health') == 'https://service.local/health'
