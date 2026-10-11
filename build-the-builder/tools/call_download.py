"""Bounded, compressed download of large Freya call records; verified TLS."""
import gzip
import json
import time
import urllib.request


def get_call(freya, call_id, budget=120):
    request = urllib.request.Request(freya.URL + '/api/v2/call/' + call_id,
                                     headers={**freya.HEADERS, 'Accept-Encoding': 'gzip'})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=15) as response:
        encoding = response.headers.get('Content-Encoding', '').lower()
        chunks, total = [], 0
        while time.monotonic() - started < budget:
            chunk = response.read1(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 64 * 1024 * 1024:
                raise ValueError('Call download exceeded 64 MiB')
            chunks.append(chunk)
        else:
            raise TimeoutError('Call download exceeded its total time budget')
    raw = b''.join(chunks)
    if encoding == 'gzip':
        raw = gzip.decompress(raw)
    elif encoding not in ('', 'identity'):
        raise ValueError('Unsupported call response encoding')
    result = json.loads(raw)
    result = result.get('data', {}).get('result', result)
    return result['call']
