import gzip
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tools.call_download import get_call


class DownloadTests(unittest.TestCase):
    def test_reads_compressed_and_plain_responses_without_disabling_tls(self):
        body = json.dumps({'data': {'result': {'call': {'id': 'synthetic-call'}}}}).encode()
        freya = SimpleNamespace(URL='https://synthetic.invalid', HEADERS={'Authorization': 'Bearer synthetic'})
        for encoding in ('gzip', ''):
            response = io.BytesIO(gzip.compress(body) if encoding else body)
            response.headers = {'Content-Encoding': encoding}
            with patch('tools.call_download.urllib.request.urlopen', return_value=response) as open_url:
                self.assertEqual(get_call(freya, 'synthetic-call'), {'id': 'synthetic-call'})
                request = open_url.call_args.args[0]
                self.assertEqual(request.get_header('Accept-encoding'), 'gzip')
                self.assertNotIn('context', open_url.call_args.kwargs)

    def test_trickling_response_cannot_extend_total_budget(self):
        response = io.BytesIO(b'incomplete')
        response.headers = {}
        freya = SimpleNamespace(URL='https://synthetic.invalid', HEADERS={})
        with patch('tools.call_download.urllib.request.urlopen', return_value=response), \
                patch('tools.call_download.time.monotonic', side_effect=[0, 0, 3]):
            with self.assertRaises(TimeoutError):
                get_call(freya, 'synthetic-call', budget=2)
