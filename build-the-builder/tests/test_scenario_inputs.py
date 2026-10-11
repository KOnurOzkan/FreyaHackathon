from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from builder.inputs import InvalidInput
from tools.artifact_checks import canonical_digest
from tools.scenario_inputs import (ROOT, apply_session_overrides, bank_case_ids,
                                  load_case_inputs, recovery_case_inputs, validate_cases)


class ScenarioInputTests(unittest.TestCase):
    def setUp(self):
        self.public = json.loads((ROOT / 'public-test-set.json').read_text())
        self.base = deepcopy(self.public[0])
        self.variant = {**deepcopy(self.base), 'id': 'DEV-UNSEEN-STYLE', 'bank_case_id': self.base['id'],
                        'script': 'Answer the same security questions, then correct the proposed day.'}

    def test_scenarios_keep_the_bank_fixture_and_separate_the_oracle(self):
        self.variant['expected']['promises'][0].update(amount=200, date='2026-10-16')
        case = validate_cases([self.variant], self.public)[0]
        self.assertEqual(case['products'], self.base['products'])
        self.assertEqual(case['identity'], self.base['identity'])
        self.assertEqual(case['bank_case_id'], 'PUB-01')
        self.assertEqual(case['expected']['promises'][0]['amount'], 200)
        self.assertEqual(bank_case_ids([case, {**case, 'id': 'DEV-ANOTHER'}]), ['PUB-01'])

    def test_changed_identity_products_faults_or_implicit_clock_are_rejected(self):
        for key, value in [('identity', {'phone_last4': '9999'}), ('products', []),
                           ('faults', [{'tool': 'create_promise', 'fail_first_n': 2}]),
                           ('variables', {**self.base['variables'], 'now': '2026-10-16T11:00'}),
                           ('brief', 'B'), ('hidden_override', True)]:
            with self.subTest(key=key), self.assertRaises(InvalidInput):
                validate_cases([{**self.variant, key: value}], self.public)

    def test_public_case_ids_cannot_hide_a_changed_script_or_expected_result(self):
        for case in [{**self.variant, 'id': 'PUB-01'}, {**self.variant, 'id': 'PUB-02'},
                     {**self.variant, 'id': '../outside'}, {**self.variant, 'bank_case_id': 'PUB-99'}]:
            with self.subTest(case=case['id']), self.assertRaises(InvalidInput):
                validate_cases([case], self.public)
        with self.assertRaises(InvalidInput):
            validate_cases([self.variant, self.variant], self.public)

    def test_only_explicit_valid_now_override_reaches_session_variables(self):
        case = validate_cases([{**self.variant, 'variable_overrides': {'now': '2026-10-16T11:00'}}], self.public)[0]
        session = {'variables': {'customer_id': 'synthetic-secret', **self.base['variables']}, 'ok': True}
        updated = apply_session_overrides(case, session)
        self.assertEqual(updated['variables']['now'], '2026-10-16T11:00')
        self.assertEqual(updated['variables']['customer_id'], 'synthetic-secret')
        self.assertEqual(session['variables']['now'], '2026-10-14T11:00')
        for overrides in [{'now': '2026-02-30T11:00'}, {'now': 'tomorrow'}, {'now': '2026-10-16'},
                          {'customer_id': 'replacement'}, {'first_name': 'replacement'}]:
            with self.subTest(overrides=overrides), self.assertRaises(InvalidInput):
                validate_cases([{**self.variant, 'variable_overrides': overrides}], self.public)

    def test_malformed_expectations_fail_before_any_remote_call(self):
        for change in [{'amount': float('nan')}, {'amount': True}, {'amount': -1}, {'amount': 421},
                       {'product_id': 'p9'}, {'date': '2026-02-30'}]:
            case = deepcopy(self.variant)
            case['expected']['promises'][0].update(change)
            with self.subTest(change=change), self.assertRaises(InvalidInput):
                validate_cases([case], self.public)

    def test_recovery_uses_exact_snapshot_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            fixture = source / 'variants.json'
            fixture.write_text(json.dumps([self.variant]), encoding='utf-8')
            snapshot = load_case_inputs(fixture)
            started = {'cases': {self.variant['id']: 'persona'},
                       'bank_cases': {self.variant['id']: 'PUB-01'}, 'case_inputs_sha256': canonical_digest(snapshot)}
            path = source / 'case-inputs.json'
            path.write_text(json.dumps(snapshot), encoding='utf-8')
            fixture.write_text('[]', encoding='utf-8')
            self.assertEqual(recovery_case_inputs(source, started)['cases'][0]['script'], self.variant['script'])
            snapshot['cases'][0]['expected']['promises'][0]['amount'] = 201
            path.write_text(json.dumps(snapshot), encoding='utf-8')
            with self.assertRaises(InvalidInput):
                recovery_case_inputs(source, started)
            path.unlink()
            with self.assertRaises(InvalidInput):
                recovery_case_inputs(source, started)

    def test_legacy_public_recovery_remains_available_without_custom_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = recovery_case_inputs(temp, {'cases': {'PUB-01': 'p1', 'PUB-10': 'p2'}, 'case': 'PUB-01,PUB-10'})
            self.assertEqual(bank_case_ids(snapshot['cases']), ['PUB-01', 'PUB-10'])

    def test_shipped_variants_are_all_grounded_and_selection_is_exact(self):
        source = ROOT / 'tests/fixtures/live-scenarios.json'
        snapshot = load_case_inputs(source)
        self.assertEqual(len(snapshot['cases']), 9)
        self.assertEqual(sum(c['brief'] == 'A' for c in snapshot['cases']), 5)
        self.assertEqual(sum(c['brief'] == 'B' for c in snapshot['cases']), 4)
        selected = ['DEV-A-FRIDAY-NOW', 'DEV-A-HARDSHIP-SUPPORT']
        self.assertEqual([c['id'] for c in load_case_inputs(source, selected)['cases']], selected)
        added = ['DEV-B-SHARED-WEEKEND-CORRECTION', 'DEV-B-MIXED-DEADLINE-CORRECTION']
        self.assertEqual([c['id'] for c in load_case_inputs(source, added)['cases']], added)
        with self.assertRaises(InvalidInput):
            load_case_inputs(source, ['PUB-01'])


if __name__ == '__main__':
    unittest.main()
