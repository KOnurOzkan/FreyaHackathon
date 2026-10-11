"""Adversarial practice personas, with independent expected ledgers and checkpoints.

Offline replay injects expected meanings. It does NOT run the extraction model
or establish that it can correctly understand the adversarial customer wording.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json

from builder.contracts import Contracts
from builder.inputs import InvalidInput, read_json
from builder.local_bank import LocalBank, audit_ledger
from builder.workflow import compile_workflow
from tools.run_cases import persona_for
from tools.scenario_inputs import load_case_inputs, validate_cases
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import fake_plan, LocalGraph

CASES = ROOT / 'tests/fixtures/live-adversarial-scenarios.json'
ORACLES = ROOT / 'tests/fixtures/adversarial-scenario-oracles.json'


class AdversarialScenarioTests(OfflineCase):
    compiled = {}

    def setUp(self):
        super().setUp()
        self.scenarios = load_case_inputs(CASES)['cases']
        self.oracles = read_json(ORACLES)

    def graph(self, case):
        brief = case['brief']
        if brief not in self.compiled:
            multiple = brief == 'B'
            documents = self.documents if multiple else self.documents[:1]
            contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
            self.compiled[brief] = compile_workflow(
                fake_extraction(documents, self.b_raw if multiple else self.a_raw),
                fake_plan(documents, multiple, contracts), documents, contracts)
        workflow, report = self.compiled[brief]
        bank = LocalBank.from_case(case, self.b if brief == 'B' else self.a)
        variables = {**case['variables'], **case.get('variable_overrides', {})}
        return LocalGraph(workflow, bank, variables, report['runtime_payload']['bindings']), bank

    def test_pack_preserves_all_bank_profiles_and_has_meaningful_coverage(self):
        self.assertEqual(len(self.scenarios), 32)
        self.assertEqual(sum(c['brief'] == 'A' for c in self.scenarios), 20)
        self.assertEqual(sum(c['brief'] == 'B' for c in self.scenarios), 12)
        self.assertEqual({c['id'] for c in self.scenarios}, set(self.oracles))
        originals = {c['id']: c for c in self.cases}
        for case in self.scenarios:
            original = originals[case['bank_case_id']]
            for key in ('brief', 'variables', 'identity', 'products', 'faults'):
                self.assertEqual(case.get(key), original.get(key), (case['id'], key))
            self.assertGreaterEqual(len(case['checks']), 7)
            self.assertTrue(self.oracles[case['id']]['policy_rules'])
            self.assertTrue(self.oracles[case['id']]['steps'])
        tags = {tag for oracle in self.oracles.values() for tag in oracle['tags']}
        self.assertTrue({'anger', 'social_engineering', 'prompt_injection', 'conditional_consent',
            'quoted_consent', 'partial_consent', 'identity_limit', 'third_party', 'calendar',
            'hardship', 'aggregation', 'leading_zero', 'product_scope'} <= tags)
        self.assertEqual({c['expected']['outcome'] for c in self.scenarios},
                         {'promise','paid','transferred','not_verified','not_reached','no_promise'})

    def test_customer_prompts_do_not_contain_the_judge_or_internal_oracles(self):
        for case in self.scenarios:
            with self.subTest(case=case['id']):
                persona = persona_for(case, {'identity': case['identity']})
                self.assertIn(case['script'], persona['prompt'])
                self.assertIn('When the caller says goodbye', case['script'])
                for internal in ('verify_identity', 'get_account', 'create_promise', 'close_call',
                                 'fb_event', 'fb_person_reply', 'policy_rules', 'expected ledger'):
                    self.assertNotIn(internal, persona['prompt'])
                for check in case['checks']:
                    self.assertIn(check, persona['successCriteria'])
                    self.assertNotIn(check, persona['prompt'])
                if case.get('third_party'):
                    self.assertNotIn(case['identity']['date_of_birth'], persona['prompt'])
                    self.assertNotIn(case['identity']['phone_last4'], persona['prompt'])
        pieces = next(c for c in self.scenarios if c['id'] == 'ADV-A-FRAGMENT-LEADING-ZERO')
        # The explicit style overrides the stock persona's one-turn-number rule.
        self.assertEqual(pieces['style'], 'pieces')
        self.assertIn('This replaces the rule', persona_for(pieces, {'identity': pieces['identity']})['prompt'])

    def test_expected_promises_are_independently_legal_under_the_practice_brief(self):
        # Separate date arithmetic, not the production calendar/deadline helper.
        holidays = {date.fromisoformat(d) for d in
            ['2026-10-26','2026-11-11','2026-11-26','2026-12-25','2027-01-01']}
        def business(d):
            return d.weekday() < 5 and d not in holidays
        for case in self.scenarios:
            now = datetime.fromisoformat(case.get('variable_overrides', {}).get('now', case['variables']['now']))
            for proposal in case['expected']['promises']:
                with self.subTest(case=case['id'], proposal=proposal):
                    product = case['products'][int(proposal['product_id'][1:])-1]
                    floor = product.get('minimum_payment', product.get('instalment_amount'))
                    self.assertLessEqual(floor, proposal['amount'])
                    self.assertLessEqual(proposal['amount'], product['past_due'])
                    deadline, remaining = now.date(), 12 if product['days_late'] <= 7 else 7
                    while remaining:
                        deadline += timedelta(days=1)
                        remaining -= int(business(deadline))
                    day = date.fromisoformat(proposal['date'])
                    self.assertTrue(business(day))
                    self.assertLessEqual(now.date(), day)
                    self.assertLessEqual(day, deadline)

    def test_mutated_profiles_or_cross_brief_aliases_are_rejected(self):
        original = self.scenarios[0]
        mutations = []
        for key, value in [('identity', {'date_of_birth':'2000-01-01','phone_last4':'0000'}),
                           ('products', []), ('brief', 'B'), ('faults', [{'tool':'create_promise','fail_first_n':99}])]:
            changed = deepcopy(original); changed[key] = value; mutations.append(changed)
        for changed in mutations:
            with self.assertRaises(InvalidInput):
                validate_cases([changed], self.cases)

    def run_scenario(self, case_id):
        case = next(c for c in self.scenarios if c['id'] == case_id)
        graph, bank = self.graph(case)
        for index, step in enumerate(self.oracles[case_id]['steps']):
            event = deepcopy(step['event'])
            graph.user(event.pop('action'), **event)
            self.assertNotEqual(graph.node, 'speak_abort', (case_id,index,graph.vars.get('fb_instruction')))
            for key, value in step.get('assert', {}).items():
                actual = (len(bank.ledger['calls']) if key == 'bank_calls' else
                          sum(c['tool'] == 'verify_identity' for c in bank.ledger['calls']) if key == 'verify_calls' else
                          len(bank.ledger[key]))
                self.assertEqual(actual, value, (case_id,index,key))
            # Account disclosure cannot precede authoritative verification.
            if not graph.state.get('verified'):
                self.assertNotIn('products', graph.state)
                self.assertNotIn('get_account', [c['tool'] for c in bank.ledger['calls']])
        self.assertIn(graph.node, {'speak_progress','speak_farewell','end'}, (case_id,graph.node,graph.vars.get('fb_speech')))
        graph.finish()
        result = audit_ledger(case, bank.ledger)
        self.assertTrue(result['pass'], (case_id,result['failed']))
        tools = [c['tool'] for c in bank.ledger['calls']]
        self.assertEqual(tools.count('close_call'), 1)
        self.assertEqual(tools[-1], 'close_call')
        if case['brief'] == 'A' or case['expected']['transfers']:
            self.assertNotIn('add_note', tools)


# Give each scenario its own test result rather than hiding 32 replays in one test.
for _case in read_json(CASES):
    def _test(self, case_id=_case['id']):
        self.run_scenario(case_id)
    _test.__doc__ = _case['title']
    setattr(AdversarialScenarioTests, 'test_replay_' + _case['id'].lower().replace('-', '_'), _test)
