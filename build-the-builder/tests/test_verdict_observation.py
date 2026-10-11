import copy
import unittest
import urllib.error
from unittest.mock import Mock, patch

from tools.live_smoke import (SmokeError, bounded_urlopen, observe_iteration,
                              persona_cache_key, read_retry, scored_row)


class VerdictObservationTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.files = {}

    def sleep(self, seconds):
        self.now += seconds

    def snapshot(self, outcome, reason=None):
        return {'runs': [{'personaId': 'persona', 'callId': 'call', 'outcome': outcome, 'reason': reason}]}

    def observe(self, snapshots, seconds=8):
        values = iter(snapshots)
        final = snapshots[-1]
        with patch('tools.live_smoke.time.monotonic', side_effect=lambda: self.now), \
                patch('tools.live_smoke.time.sleep', side_effect=self.sleep):
            return observe_iteration(lambda: copy.deepcopy(next(values, final)), ['persona'],
                lambda name, value: self.files.update({name: copy.deepcopy(value)}), Mock(),
                deadline=100, seconds=seconds)

    def row(self, iteration, evidence, ended=True, ledger_failures=()):
        return scored_row({'id': 'case'}, {'calls': []}, iteration['runs'][0],
            {'endedAt': 'done' if ended else None},
            lambda *_: {'failed': list(ledger_failures)}, evidence)

    def test_delayed_verdict_waits_and_keeps_the_missing_snapshot(self):
        iteration, evidence = self.observe([self.snapshot(None), self.snapshot('pass'), self.snapshot('pass')])
        self.assertTrue(evidence['stable'])
        self.assertEqual(evidence['snapshots'], 3)
        self.assertIsNone(self.files['iteration-observation-001.json']['runs'][0]['outcome'])
        self.assertTrue(self.row(iteration, evidence)['pass'])

    def test_transient_missing_verdict_does_not_erase_the_next_official_result(self):
        iteration, evidence = self.observe([self.snapshot('pass'), self.snapshot(None), self.snapshot('pass'), self.snapshot('pass')])
        self.assertEqual(evidence['snapshots'], 4)
        self.assertTrue(self.row(iteration, evidence)['pass'])

    def test_fail_then_pass_remains_blocked_with_both_official_outcomes(self):
        iteration, evidence = self.observe([self.snapshot('fail', 'Violation'), self.snapshot('pass'), self.snapshot('pass')])
        row = self.row(iteration, evidence)
        self.assertEqual(row['official_outcome'], 'pass')
        self.assertEqual(row['official_observed_outcomes'], ['fail', 'pass'])
        self.assertTrue(row['ledger_pass'])
        self.assertFalse(row['pass'])
        self.assertTrue(row['evaluation_issues'])

    def test_pass_then_inconclusive_is_not_promoted_to_the_preferred_snapshot(self):
        iteration, evidence = self.observe([self.snapshot('pass'), self.snapshot('inconclusive'), self.snapshot('inconclusive')])
        row = self.row(iteration, evidence)
        self.assertEqual(row['official_outcome'], 'inconclusive')
        self.assertFalse(row['pass'])
        self.assertIn('official outcome inconclusive', row['failed'][0])

    def test_perpetually_missing_verdict_stops_with_incomplete_evidence(self):
        iteration, evidence = self.observe([self.snapshot(None)], seconds=5)
        self.assertFalse(evidence['stable'])
        self.assertEqual(evidence['snapshots'], 3)
        self.assertLess(self.now, 5)
        self.assertFalse(self.row(iteration, evidence)['pass'])

    def test_two_duplicate_calls_or_wrong_personas_cannot_stabilize(self):
        snap = self.snapshot('pass')
        snap['runs'].append(dict(snap['runs'][0]))
        _, evidence = self.observe([snap], seconds=3)
        self.assertFalse(evidence['stable'])

    def test_bank_or_unfinished_call_still_blocks_an_official_pass(self):
        iteration, evidence = self.observe([self.snapshot('pass')])
        self.assertFalse(self.row(iteration, evidence, ended=False)['pass'])
        self.assertFalse(self.row(iteration, evidence, ledger_failures=['Wrong date'])['pass'])

    def test_persona_cache_separates_styles_by_the_full_supplied_spec(self):
        normal = {'name': 'case', 'prompt': 'customer facts'}
        pieces = {'name': 'case [pieces]', 'prompt': 'customer facts\nspeak in pieces'}
        self.assertNotEqual(persona_cache_key('case', normal), persona_cache_key('case', pieces))
        self.assertEqual(persona_cache_key('case', normal), persona_cache_key('case', dict(normal)))

    def test_unauthorized_http_reads_are_not_retried(self):
        operation = Mock(side_effect=urllib.error.HTTPError('https://example.invalid', 401, 'Unauthorized', {}, None))
        with patch('tools.live_smoke.time.sleep') as sleep, self.assertRaises(urllib.error.HTTPError):
            read_retry(operation, Mock())
        self.assertEqual(operation.call_count, 1)
        sleep.assert_not_called()

    def test_retry_and_request_timeout_honor_remaining_budget(self):
        with patch('tools.live_smoke.time.monotonic', return_value=99), patch('tools.live_smoke.time.sleep') as sleep:
            operation = Mock(side_effect=ConnectionResetError())
            with self.assertRaises(SmokeError):
                read_retry(operation, Mock(), deadline=100)
            self.assertEqual(operation.call_count, 1)
            sleep.assert_not_called()
            request = Mock(return_value='response')
            self.assertEqual(bounded_urlopen(request, 100)('url', timeout=120), 'response')
            request.assert_called_once_with('url', timeout=1)


if __name__ == '__main__':
    unittest.main()
