from app.cli import validation_exit_code

def test_invalid_payload_exits_non_zero():
    assert validation_exit_code(['missing email']) == 1

def test_valid_payload_exits_zero():
    assert validation_exit_code([]) == 0
