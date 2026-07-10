def dump_record(record: dict[str, object]) -> dict[str, object]:
    out = {}
    if 'first_name' in record:
        out['firstName'] = record['first_name']
    return out

def load_record(payload: dict[str, object]) -> dict[str, object]:
    out = {}
    if 'firstName' in payload:
        out['first_name'] = payload['firstName']
    return out
