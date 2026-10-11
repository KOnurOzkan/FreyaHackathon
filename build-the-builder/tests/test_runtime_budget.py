"""Byte boundaries and a local replay stress test; no model or live bank calls."""
import json
from pathlib import Path
import unittest

from builder.runtime_budget import (RuntimeBudgetError, validate_runtime_budget,
    SANDBOX_INPUT_LIMIT_BYTES, PROTOCOL_RESERVE_BYTES, MINIMUM_VARIABLE_BUDGET_BYTES, SAFETY_SLACK_BYTES)


class RuntimeBudgetTests(unittest.TestCase):
    def test_exact_encoded_boundary_passes_and_next_byte_fails(self):
        maximum = SANDBOX_INPUT_LIMIT_BYTES - PROTOCOL_RESERVE_BYTES - MINIMUM_VARIABLE_BUDGET_BYTES - SAFETY_SLACK_BYTES
        self.assertEqual(maximum, 88064)
        # Evidence bounds (out/code-exec-size-observations-052.json): stay below
        # the 99,238-byte code that ran live, and keep the measured ~17,900-byte
        # overhead plus the full state allowance under the platform limit.
        self.assertLess(maximum, 99238)
        self.assertGreaterEqual(PROTOCOL_RESERVE_BYTES, 17900)
        self.assertLess(maximum + 17900 + MINIMUM_VARIABLE_BUDGET_BYTES, SANDBOX_INPUT_LIMIT_BYTES)
        report = validate_runtime_budget('x' * (maximum - 2))
        self.assertEqual(report['encoded_code_json_bytes'], maximum)
        self.assertEqual(report['remaining_reserved_headroom_bytes'], 0)
        self.assertEqual(report['reserved_total_bytes'], SANDBOX_INPUT_LIMIT_BYTES)
        self.assertFalse(report['protocol_overhead_exactly_known'])
        with self.assertRaises(RuntimeBudgetError):
            validate_runtime_budget('x' * (maximum - 1))

    def test_unicode_backslashes_quotes_and_newlines_count_json_escaping(self):
        code = 'value = "Istanbul: \u0130, symbol: \U0001f9ed"\npattern = "\\\\d"\n'
        report = validate_runtime_budget(code)
        self.assertEqual(report['encoded_code_json_bytes'], len(json.dumps(code, ensure_ascii=True).encode('utf-8')))
        self.assertGreater(report['encoded_code_json_bytes'], len(code.encode('utf-8')))
        with self.assertRaises(RuntimeBudgetError):
            validate_runtime_budget('\U0001f9ed' * 8000)

    def test_larger_runtime_state_reduces_source_room_and_cannot_be_understated(self):
        normal = validate_runtime_budget('pass')
        larger = validate_runtime_budget('pass', variable_budget_bytes=32768)
        self.assertEqual(normal['maximum_encoded_code_bytes'] - larger['maximum_encoded_code_bytes'], 12288)
        for budget in (None, True, 0, -1, 16384, 20479, 20480.0, '20480', 131072):
            with self.subTest(budget=budget), self.assertRaises(RuntimeBudgetError):
                validate_runtime_budget('pass', variable_budget_bytes=budget)

    def test_invalid_source_fails_closed_and_errors_never_include_source(self):
        for code in (None, b'pass', {}, '', '  \n', '\ud800'):
            with self.subTest(kind=type(code).__name__), self.assertRaises(RuntimeBudgetError):
                validate_runtime_budget(code)
        private = 'PRIVATE-SYNTHETIC-CREDENTIAL-MARKER'
        with self.assertRaises(RuntimeBudgetError) as caught:
            validate_runtime_budget(private * 4000)
        self.assertNotIn(private, str(caught.exception))
        self.assertIn('reduce compiled source', str(caught.exception))
        self.assertIn('overhead is unknown', str(caught.exception))


def stress_saved_b12(root):
    """Exercise the actual saved B12 graph with enlarged synthetic local data.

    The observed platform table contributes only non-controller fields, allowing
    conservative measurement of platform counters/flattened envelopes missing
    from LocalGraph. No transcript text or customer secret is returned.
    """
    from builder.local_bank import LocalBank
    from tests.workflow_support import LocalGraph
    root = Path(root)
    workflow = json.loads((root / 'out/workflow-b-12/workflow.json').read_text(encoding='utf-8'))
    payload = json.loads((root / 'out/workflow-b-12/runtime-payload.json').read_text(encoding='utf-8'))
    observed_call = json.loads((root / 'out/overnight-b-051-baseline/call-PUB-12.json').read_text(encoding='utf-8'))
    largest = {}
    for message in observed_call['messages']:
        try:
            event = json.loads(message.get('message', ''))
        except ValueError:
            continue
        table = event.get('variable_table', {}) if event.get('event') == 'route_evaluated' else {}
        if len(json.dumps(table).encode()) > len(json.dumps(largest).encode()):
            largest = table
    platform = {k: v for k, v in largest.items() if not k.startswith('fb_')}
    measurements = []

    class MeasuredVariables(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            combined = {**platform, **self}
            measurements.append({'plain_vars_json_bytes': len(json.dumps(combined, ensure_ascii=True).encode()),
                'local_vars_json_bytes': len(json.dumps(self, ensure_ascii=True).encode()),
                'history_events': len(json.loads(self.get('fb_history') or '[]')),
                'history_value_bytes': len(str(self.get('fb_history', '')).encode()),
                'results_value_bytes': len(str(self.get('fb_results', '')).encode())})

    bank = LocalBank({'date_of_birth': '1982-07-21', 'phone_last4': '0368'}, [
        {'product_id':'stress-card','type':'credit_card','card_last4':'0368',
         'past_due':821.37,'minimum_payment':214.62,'days_late':6},
        {'product_id':'stress-personal','type':'personal_loan','past_due':1341.22,'instalment_amount':305.13,'days_late':21},
        {'product_id':'stress-car','type':'car_loan','past_due':480.17,'instalment_amount':129.04,'days_late':4}],
        wrong_answer_limit=3, faults=[{'tool':'create_promise','fail_first_n':1}])
    graph = LocalGraph(workflow, bank, {'now':'2026-10-14T11:00'}, payload['bindings'])
    graph.vars = MeasuredVariables(graph.vars)
    graph.user('person_yes')
    graph.user('identity_answer', answer='1981-07-21', answer_complete=True)
    graph.user('identity_answer', answer='1980-07-21', answer_complete=True)
    graph.user('identity_answer', answer='1982-07-21', answer_complete=True)
    graph.user('identity_answer', answer='0368', answer_complete=True)
    for i in range(40):
        graph.user('question', question_kind=('minimum','deadline','balance','other')[i % 4])
    graph.user('payment', scope='all', amount_kind='full', day={'kind':'iso','value':'2026-10-16'})
    for i in range(20):
        graph.user('correction', scope='all', amount_kind='full',
                   day={'kind':'iso','value':'2026-10-19' if i % 2 else '2026-10-16'})
    for i in range(10):
        graph.user('question', question_kind=('minimum','deadline','balance')[i % 3])
    if bank.ledger['promises'] or graph.node != 'speak_confirm':
        raise AssertionError('Stress setup did not preserve an unrecorded combined confirmation')
    graph.user('confirm', explicit_yes=True)
    graph.finish()
    tools = [row['tool'] for row in bank.ledger['calls']]
    if len(bank.ledger['promises']) != 3 or tools.count('create_promise') != 4 or tools.count('close_call') != 1:
        raise AssertionError('Stress replay lost bounded retries, promises or exactly-once close')
    maximum = max(measurements, key=lambda row: row['plain_vars_json_bytes'])
    return {'scope':'local_replay_with_observed_noncontroller_variable_overlay_not_exact_server_bytes',
            'maximum': maximum, 'measurements': len(measurements),
            'history_events_final': len(json.loads(graph.vars['fb_history'])),
            'successful_promises': len(bank.ledger['promises']), 'create_promise_calls': tools.count('create_promise'),
            'close_call_count': tools.count('close_call'), 'wrong_answers': 2,
            'repeated_questions': 50, 'readback_corrections': 20,
            'observed_platform_overlay_bytes':len(json.dumps(platform).encode()),
            'maximum_code_json_bytes':max(len(json.dumps(node['pre_actions'][0]['function']['parameters']['properties']['code']['default']).encode())
                for node in workflow['nodes'].values() if node['node_type']=='code')}


class SavedRuntimeStressTests(unittest.TestCase):
    def test_saved_b12_long_state_with_retries_and_reconfirmation(self):
        root = Path(__file__).resolve().parents[1]
        if not (root / 'out/workflow-b-12/runtime-payload.json').exists() or not (root / 'out/overnight-b-051-baseline/call-PUB-12.json').exists():
            self.skipTest('Saved local development artifacts are not available')
        report = stress_saved_b12(root)
        self.assertEqual(report['successful_promises'], 3)
        self.assertEqual(report['create_promise_calls'], 4)
        self.assertGreater(report['history_events_final'], 75)
        self.assertGreater(report['maximum']['plain_vars_json_bytes'], 10538)
        # This long scenario exceeded the earlier 16 KiB assumption. Round its
        # measured state up to the next 4 KiB instead of weakening the guard.
        measured = report['maximum']['plain_vars_json_bytes']
        allowance = ((measured + 4095) // 4096) * 4096
        self.assertEqual(allowance, MINIMUM_VARIABLE_BUDGET_BYTES)
        self.assertLessEqual(measured, MINIMUM_VARIABLE_BUDGET_BYTES)
        budget = validate_runtime_budget('pass', variable_budget_bytes=allowance)
        self.assertEqual(budget['maximum_encoded_code_bytes'], 88064)
