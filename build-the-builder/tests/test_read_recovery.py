import unittest
from unittest.mock import patch
from tools.live_smoke import read_retry


class ReadRecoveryTests(unittest.TestCase):
    @patch('tools.live_smoke.time.sleep')
    def test_transient_wrapped_http_read_recovers_without_restarting_an_operation(self, sleep):
        replies=iter([RuntimeError('GET /status -> 504: timeout'), RuntimeError('GET /status -> 503: temporarily unavailable'), {'status':'completed'}])
        events=[]
        def read():
            result=next(replies)
            if isinstance(result,Exception): raise result
            return result
        self.assertEqual(read_retry(read,lambda *a,**k: events.append(k)),{'status':'completed'})
        self.assertEqual(len(events),2)

    @patch('tools.live_smoke.time.sleep')
    def test_authentication_and_unclassified_errors_are_not_retried(self, sleep):
        for message in ('GET /status -> 401: invalid key','Malformed response'):
            with self.subTest(message=message), self.assertRaises(RuntimeError):
                read_retry(lambda: (_ for _ in ()).throw(RuntimeError(message)),lambda *a,**k: None)
        sleep.assert_not_called()
