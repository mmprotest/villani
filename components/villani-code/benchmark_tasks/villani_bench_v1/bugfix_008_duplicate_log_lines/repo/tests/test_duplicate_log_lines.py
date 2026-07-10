from app.logger import dedupe_lines

def test_removes_immediate_duplicates():
    assert dedupe_lines(['a', 'a', 'b']) == ['a', 'b']

def test_preserves_first_seen_order_for_distinct_lines():
    assert dedupe_lines(['db', 'api', 'db', 'worker']) == ['db', 'api', 'worker']
