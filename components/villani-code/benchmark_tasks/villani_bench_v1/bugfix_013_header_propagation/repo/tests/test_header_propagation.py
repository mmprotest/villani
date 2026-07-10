from app.headers import forward_headers

def test_preserves_authorization_header():
    out = forward_headers({'authorization': 'Bearer abc', 'x-request-id': '1'})
    assert out['authorization'] == 'Bearer abc'

def test_keeps_existing_request_id():
    out = forward_headers({'authorization': 'Bearer abc', 'x-request-id': '1'})
    assert out['x-request-id'] == '1'
