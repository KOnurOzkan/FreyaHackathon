"""Validate evaluation fixtures; live transcripts still need independent review.

These checks do not assert that the phone agent follows the scenario. In
particular, counting the correction asks requires the eventual call transcript.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from tools.artifact_checks import canonical_digest
from tools.scenario_inputs import (ROOT, EDITABLE, load_case_inputs,
                                   recovery_case_inputs)
from tools.score import score_ledger


FIXTURE = ROOT / 'tests/fixtures/live-callback-scenarios.json'


class LiveCallbackScenarioTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = load_case_inputs(FIXTURE)
        self.cases = self.snapshot['cases']
        public = json.loads((ROOT / 'public-test-set.json').read_text(encoding='utf-8'))
        self.base = next(case for case in public if case['id'] == 'PUB-02')

    def test_all_three_are_evaluation_only_variants_of_the_same_relative(self):
        self.assertTrue(self.snapshot['evaluation_only'])
        self.assertEqual(len(self.cases), 3)
        self.assertEqual(len({case['id'] for case in self.cases}), 3)
        immutable = {key: value for key, value in self.base.items() if key not in EDITABLE}
        for case in self.cases:
            with self.subTest(case=case['id']):
                self.assertEqual(case['bank_case_id'], 'PUB-02')
                self.assertNotEqual(case['id'], 'PUB-02')
                self.assertEqual({key: value for key, value in case.items() if key not in EDITABLE}, immutable)
                self.assertEqual(case['family'], 'Third party')
                self.assertTrue(case['third_party'])
                self.assertNotIn('variable_overrides', case)

    def test_callback_oracles_are_distinct_and_reject_wrong_or_private_actions(self):
        self.assertEqual([case['expected']['reason'] for case in self.cases],
                         ['no_callback', 'no_callback', 'callback'])
        for case in self.cases:
            with self.subTest(case=case['id']):
                expected = deepcopy(case['expected'])
                self.assertEqual(expected['outcome'], 'not_reached')
                self.assertEqual((expected['promises'], expected['notes'], expected['transfers']), ([], [], []))
                calls = [{'tool': 'schedule_callback', 'result': {'ok': True}}] if expected['callbacks'] else []
                calls.append({'tool': 'close_call', 'result': {'ok': True}})
                ledger = {**expected, 'calls': calls}
                self.assertTrue(score_ledger(case, ledger)['pass'])
                bad_callback = deepcopy(ledger)
                if expected['callbacks']:
                    bad_callback['callbacks'][0]['date'] = case['variables']['now'][:10]
                else:
                    bad_callback['callbacks'] = [{'date': '2026-10-15', 'time': '11:00'}]
                self.assertFalse(score_ledger(case, bad_callback)['pass'])
                for forbidden in ('verify_identity', 'get_account'):
                    bad_private = deepcopy(ledger)
                    bad_private['calls'].insert(0, {'tool': forbidden, 'result': {'ok': True}})
                    self.assertFalse(score_ledger(case, bad_private)['pass'])
                unclosed = deepcopy(ledger)
                unclosed['calls'].pop()
                self.assertFalse(score_ledger(case, unclosed)['pass'])
        complete = self.cases[-1]
        now = datetime.fromisoformat(complete['variables']['now'])
        callback, = complete['expected']['callbacks']
        when = datetime.fromisoformat(callback['date'] + 'T' + callback['time'])
        self.assertEqual(when.date(), (now + timedelta(days=1)).date())
        self.assertLess(when.weekday(), 5)
        self.assertGreater(when, now)
        self.assertGreaterEqual(callback['time'], '10:00')
        self.assertLessEqual(callback['time'], '19:00')

    def test_selection_and_recovery_preserve_each_script_with_shared_bank_fixture(self):
        selected = [case['id'] for case in reversed(self.cases)]
        snapshot = load_case_inputs(FIXTURE, selected)
        self.assertEqual([case['id'] for case in snapshot['cases']], selected)
        started = {'cases': {case_id: 'synthetic-persona-' + str(index)
                             for index, case_id in enumerate(selected)},
                   'bank_cases': {case_id: 'PUB-02' for case_id in selected},
                   'case_inputs_sha256': canonical_digest(snapshot)}
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            (source / 'case-inputs.json').write_text(json.dumps(snapshot), encoding='utf-8')
            recovered = recovery_case_inputs(source, started)
        self.assertEqual(recovered, snapshot)
        self.assertEqual(len({case['script'] for case in recovered['cases']}), 3)


if __name__ == '__main__':
    unittest.main()
