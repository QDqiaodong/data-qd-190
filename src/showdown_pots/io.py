"""Strict JSON input and atomic create-only output publication."""
import json
import os
from pathlib import Path
import tempfile

def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result

def reject_constant(value):
    raise ValueError(f'non-finite JSON constant: {value}')

def load(path):
    with open(path, 'rb') as source:
        data = source.read(8_000_001)
    if len(data) > 8_000_000:
        raise ValueError('input exceeds 8 MB')
    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=reject_constant)

def publish(path, result):
    payload = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.report-', delete=False) as f:
            temporary = Path(f.name)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
