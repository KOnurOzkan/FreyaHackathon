"""Incremental, redacted call evidence independent of a later batch failure."""
import hashlib
import json
from pathlib import Path
import re


def customer_secrets(value):
    found = set()

    def walk(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower().replace('_', '').replace('-', '') == 'customerid':
                    candidate = child.get('value') if isinstance(child, dict) else child
                    if isinstance(candidate, str) and candidate and candidate != '[REDACTED]':
                        found.add(candidate)
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str) and item.strip().startswith(('{', '[')):
            try:
                walk(json.loads(item))
            except (ValueError, RecursionError):
                pass

    walk(value)
    return found


def persist_call_snapshot(folder, call_id, call, secrets, redactor, *, case_id=None):
    """Persist a collected call before requesting the next one; never score it.

    Finished calls with known case mappings use their final artifact name. Other
    snapshots get a content-addressed provisional name, so later completion does
    not overwrite earlier evidence. Repeated identical saves are idempotent.
    """
    if not isinstance(call_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', call_id):
        raise ValueError('Invalid call artifact identifier')
    if case_id is not None and (not isinstance(case_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', case_id)):
        raise ValueError('Invalid case artifact identifier')
    if not isinstance(call, dict) or call.get('id', call_id) != call_id:
        raise ValueError('Downloaded call does not match its requested identifier')
    # Discover per-call secrets before redacting nested JSON event strings, even
    # if a recovery process did not create the customer session itself.
    secrets.extend(sorted(customer_secrets(call) - set(secrets)))
    safe = redactor(call, secrets)
    text = json.dumps(safe, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    folder = Path(folder)
    if case_id is not None and call.get('endedAt'):
        path = folder / ('call-' + case_id + '.json')
    else:
        path = folder / 'call-observations' / (call_id + '-' + digest[:16] + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8')) != safe:
            raise ValueError('An immutable call snapshot already exists with different evidence')
        return {'path': str(path), 'created': False, 'call_ended': bool(call.get('endedAt'))}
    with path.open('x', encoding='utf-8') as stream:
        json.dump(safe, stream, indent=2, ensure_ascii=False)
    return {'path': str(path), 'created': True, 'call_ended': bool(call.get('endedAt'))}
