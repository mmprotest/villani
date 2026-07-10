from app.serializer import dump_record, load_record

def test_roundtrip_preserves_alias_and_extra_fields():
    original = {'first_name': 'Ada', 'role': 'admin'}
    assert load_record(dump_record(original)) == original

def test_unaliased_fields_are_not_dropped_on_dump():
    assert dump_record({'first_name': 'Ada', 'role': 'admin'})['role'] == 'admin'
