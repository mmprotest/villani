from app.http.request import build_outgoing_headers

def test_preserves_authorization_value():
    headers = build_outgoing_headers({'Authorization': 'Bearer AbC123'})
    assert headers['authorization'] == 'Bearer AbC123'

def test_normalizes_lookup_case_insensitively():
    headers = build_outgoing_headers({'X-Trace-ID': 'abc'})
    assert headers['x-trace-id'] == 'abc'
