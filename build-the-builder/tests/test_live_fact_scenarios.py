"""Calibrate evaluation-only fact questions against the brief and frozen bank fixtures.

These scripted semantic turns exercise emitted code, not Freya's customer/agent
models. Expected monetary/date facts come from the source brief and bank profile.
"""
from copy import deepcopy
from datetime import date, timedelta
import re

from builder.contracts import Contracts
from builder.inputs import read_json
from builder.local_bank import LocalBank, audit_ledger
from builder.workflow import compile_workflow
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import fake_plan, LocalGraph
from tools.scenario_inputs import load_case_inputs


FIXTURE = ROOT / 'tests/fixtures/live-fact-scenarios.json'
MONTHS = 'January February March April May June July August September October November December'.split()


def independent_account_facts(case):
    """Practice-brief oracle; deliberately does not call production date/money helpers."""
    brief = (ROOT / 'briefs/brief-a.md').read_text(encoding='utf-8')
    calendar_line = next(line for line in brief.splitlines() if line.startswith('Business days are'))
    assert 'Monday to Friday' in calendar_line
    holidays = {date.fromisoformat(day) for day in re.findall(r'\d{4}-\d{2}-\d{2}', calendar_line)}
    bands = [tuple(map(int, row)) for row in re.findall(
        r'^\|\s*(\d+) to (\d+)\s*\|[^|]+\|\s*the (\d+)(?:st|nd|rd|th) business day after today\s*\|',
        brief, re.MULTILINE)]
    assert len(bands) == 2
    floor_rule = next(line for line in brief.splitlines() if line.startswith('25.'))
    card_field, loan_field = re.findall(r'`([^`]+)`', floor_rule)
    supplied = case.get('variable_overrides', {}).get('now', case['variables']['now'])
    today = date.fromisoformat(supplied[:10])
    facts = []
    for index, product in enumerate(case['products'], 1):
        matches = [n for low, high, n in bands if low <= product['days_late'] <= high]
        assert len(matches) == 1
        cursor, remaining = today, matches[0]
        while remaining:
            cursor += timedelta(days=1)
            if cursor.weekday() < 5 and cursor not in holidays:
                remaining -= 1
        label = product['type'].replace('_', ' ')
        if product['type'] == 'credit_card':
            label += ' ending ' + product['card_last4']
        field = card_field if product['type'] == 'credit_card' else loan_field
        facts.append({'product_id': 'p' + str(index), 'label': label,
                      'floor': f"{product[field]:.2f}", 'deadline': cursor.isoformat(),
                      'spoken_deadline': f'{cursor.day} {MONTHS[cursor.month - 1]} {cursor.year}'})
    return facts


class LiveFactScenarioTests(OfflineCase):
    def scenarios(self):
        return load_case_inputs(FIXTURE)['cases']

    def test_cases_are_separate_bank_grounded_variants_with_exact_selection(self):
        scenarios = self.scenarios()
        ids = ['DEV-A-KNOWN-FACTS', 'DEV-B-AMBIGUOUS-KNOWN-FACTS']
        self.assertEqual([case['id'] for case in scenarios], ids)
        old = load_case_inputs(ROOT / 'tests/fixtures/live-scenarios.json')['cases']
        self.assertEqual(len(old), 9)
        self.assertTrue(set(ids).isdisjoint(case['id'] for case in old))
        self.assertEqual([case['id'] for case in load_case_inputs(FIXTURE, ids[::-1])['cases']], ids[::-1])
        public = {case['id']: case for case in self.cases}
        for case in scenarios:
            baseline = public[case['bank_case_id']]
            for key in ('identity', 'products', 'variables', 'faults'):
                self.assertEqual(case.get(key), baseline.get(key))

    def test_expected_facts_are_independently_derived_from_brief_calendar_and_account(self):
        a, b = map(independent_account_facts, self.scenarios())
        self.assertEqual([(f['floor'], f['deadline']) for f in a], [('126.00', '2026-11-02')])
        self.assertEqual([(f['floor'], f['deadline']) for f in b],
                         [('66.00', '2026-10-23'), ('213.00', '2026-10-23'), ('25.00', '2026-11-02')])

    def graph(self, case):
        multiple = case['brief'] == 'B'
        documents, raw, policy = ((self.documents, self.b_raw, self.b) if multiple else
                                  (self.documents[:1], self.a_raw, self.a))
        contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
        workflow, report = compile_workflow(fake_extraction(documents, raw),
            fake_plan(documents, multiple, contracts), documents, contracts)
        bank = LocalBank.from_case(case, policy)
        graph = LocalGraph(workflow, bank, case['variables'], report['runtime_payload']['bindings'])
        graph.user('person_yes')
        for field in policy.identity_fields:
            graph.user('identity_answer', answer=case['identity'][field], answer_complete=True)
        return graph, bank

    def assert_fact_questions_preserve_financial_state(self, case, graph, bank, ambiguous=False):
        facts = independent_account_facts(case)
        initial = {key: value for key, value in graph.state.items() if key != 'instruction'}
        ledger = deepcopy(bank.ledger)
        for kind in ('minimum', 'deadline'):
            fields = {'question_kind': kind}
            if ambiguous:
                # Even a guessed valid ID cannot override the ambiguous quote.
                fields.update(question_product_id='p2', question_product_reference='that account')
            graph.user('question', **fields)
            self.assertEqual({key: value for key, value in graph.state.items() if key != 'instruction'}, initial)
            self.assertEqual(bank.ledger, ledger)
            speech = graph.vars['fb_speech']
            self.assertNotIn('?', speech)
            self.assertNotIn('I do not have', speech)
            positions = []
            for fact in facts:
                positions.append(speech.index(fact['label']))
                self.assertIn(fact['floor'] if kind == 'minimum' else fact['spoken_deadline'], speech)
            self.assertEqual(positions, sorted(positions))

    def test_a_questions_then_voluntary_full_friday_promise(self):
        case = self.scenarios()[0]
        graph, bank = self.graph(case)
        self.assert_fact_questions_preserve_financial_state(case, graph, bank)
        graph.user('payment', amount_kind='full', day={'kind': 'this_weekday', 'weekday': 4,
                                                     'source_text': 'this Friday'})
        self.assertEqual(graph.node, 'speak_confirm')
        self.assertEqual(bank.ledger['promises'], [])
        self.assertIn('420.00', graph.vars['fb_speech'])
        self.assertIn('16 October 2026', graph.vars['fb_speech'])
        graph.user('confirm', explicit_yes=True)
        graph.finish()
        result = audit_ledger(case, bank.ledger)
        self.assertTrue(result['pass'], result['failed'])

    def test_b_ambiguous_questions_then_plain_acceptance_means_full_all_today(self):
        case = self.scenarios()[1]
        graph, bank = self.graph(case)
        self.assert_fact_questions_preserve_financial_state(case, graph, bank, ambiguous=True)
        graph.user('accept_offer')
        self.assertEqual(graph.node, 'speak_confirm')
        self.assertEqual(bank.ledger['promises'], [])
        self.assertEqual([p['amount'] for p in graph.state['promises_to_confirm']], ['220.00', '640.00', '75.00'])
        self.assertEqual({p['date'] for p in graph.state['promises_to_confirm']}, {'2026-10-14'})
        graph.user('confirm', explicit_yes=True)
        graph.finish()
        result = audit_ledger(case, bank.ledger)
        self.assertTrue(result['pass'], result['failed'])
