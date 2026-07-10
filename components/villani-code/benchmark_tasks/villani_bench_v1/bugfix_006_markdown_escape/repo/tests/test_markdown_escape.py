from app.markdown import render_label

def test_escapes_underscores_in_plain_text():
    assert render_label('user_name') == 'user\_name'

def test_keeps_code_spans_readable():
    assert render_label('`user_name`') == '`user_name`'
