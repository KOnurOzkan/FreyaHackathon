"""Known financial facts are deterministic answers, not new payment decisions."""
from copy import deepcopy
import re

from builder.contracts import Contracts
from builder.dialogue_plan import PlanStructureError, validate_plan
from builder.inputs import InvalidInput, read_json
from builder.local_bank import LocalBank
from builder.workflow import compile_workflow
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import LocalGraph, fake_plan


class KnownAccountQuestionTests(OfflineCase):
    def graph(self, multiple=False, verified=True):
        documents = self.documents if multiple else self.documents[:1]
        raw = self.b_raw if multiple else self.a_raw
        contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
        workflow, report = compile_workflow(fake_extraction(documents, raw),
            fake_plan(documents, multiple, contracts), documents, contracts)
        products = [{'product_id': 'facts-card', 'type': 'credit_card', 'card_last4': '7492',
                     'past_due': 693.81, 'minimum_payment': 208.14, 'days_late': 15}]
        if multiple:
            products += [{'product_id': 'facts-loan', 'type': 'personal_loan',
                          'past_due': 814.29, 'instalment_amount': 179.03, 'days_late': 4}]
        bank = LocalBank({'date_of_birth': '1985-08-19', 'phone_last4': '0276'}, products, wrong_answer_limit=3)
        graph = LocalGraph(workflow, bank, {'now': '2028-05-09T13:25'}, report['runtime_payload']['bindings'])
        if verified:
            graph.user('person_yes')
            graph.user('identity_answer', answer='1985-08-19', answer_complete=True)
            graph.user('identity_answer', answer='0276', answer_complete=True)
        return graph, bank

    def stable_state(self, graph):
        return {key: value for key, value in graph.state.items() if key != 'instruction'}

    def test_floor_deadline_and_arrears_queries_do_not_change_payment_state(self):
        graph, bank = self.graph()
        before = self.stable_state(graph)
        ledger = deepcopy(bank.ledger)
        for kind, exact in [('minimum', '208.14'), ('deadline', '18 May 2028'), ('balance', '693.81')]:
            with self.subTest(kind=kind):
                graph.user('question', question_kind=kind)
                self.assertEqual(self.stable_state(graph), before)
                self.assertEqual(bank.ledger, ledger)
                self.assertIn(exact, graph.vars['fb_speech'])
                self.assertNotIn('?', graph.vars['fb_speech'])
                self.assertNotIn('I do not have', graph.vars['fb_speech'])
                self.assertNotIn('Can you pay', graph.vars['fb_speech'])
        graph.user('too_much')
        self.assertIn('208.14', graph.vars['fb_speech'])
        self.assertIn('The rest stays overdue.', graph.vars['fb_speech'])

    def test_question_preserves_refusal_attempt_count_and_pending_readback(self):
        graph, bank = self.graph()
        graph.user('refuse_payment')
        self.assertEqual(graph.state['products'][0]['attempts_used'], 1)
        before = self.stable_state(graph)
        graph.user('question', question_kind='minimum')
        self.assertEqual(self.stable_state(graph), before)
        graph.user('payment', amount_kind='explicit', amount='341.27', day={'kind':'tomorrow'})
        before = self.stable_state(graph)
        graph.user('question', question_kind='deadline')
        self.assertEqual(self.stable_state(graph), before)
        self.assertEqual(graph.node, 'speak_confirm')
        self.assertEqual(bank.ledger['promises'], [])
        graph.user('confirm', explicit_yes=True)
        self.assertEqual(bank.ledger['promises'][0]['amount'], 341.27)

    def test_unclear_multi_product_question_answers_every_product_without_guessing(self):
        graph, bank = self.graph(multiple=True)
        before = self.stable_state(graph)
        for kind in ['minimum', 'deadline', 'balance']:
            graph.user('question', question_kind=kind, product_id='not-authoritative')
            speech = graph.vars['fb_speech']
            self.assertIn('credit card ending 7492', speech)
            self.assertIn('personal loan', speech)
            self.assertLess(speech.index('credit card'), speech.index('personal loan'))
            self.assertNotIn('all these amounts today', speech)
            self.assertEqual(self.stable_state(graph), before)
        self.assertEqual(bank.ledger['promises'], [])

    def test_explicit_question_target_is_separate_from_primary_decision_target(self):
        graph, bank = self.graph(multiple=True)
        graph.vars['fb_question_kind'] = 'minimum'
        graph.user('payment', product_id='facts-card', amount_kind='explicit', amount='341.27',
                   day={'kind':'tomorrow'}, question_product_id='facts-loan',
                   question_product_reference='personal loan')
        speech = graph.vars['fb_speech']
        self.assertTrue(speech.startswith('The smallest payment I can record for your personal loan is 179.03.'))
        self.assertEqual(graph.state['promises_to_confirm'][0]['amount'], '341.27')
        self.assertEqual(bank.ledger['promises'], [])
        self.assertEqual(graph.vars['fb_question_kind'], 'none')

    def test_valid_id_with_missing_or_contradictory_question_reference_answers_all(self):
        graph, bank = self.graph(multiple=True)
        before = self.stable_state(graph)
        for reference in (None, '', 'that account', 'personal loan', 'credit card ending 1111'):
            with self.subTest(reference=reference):
                fields = {'question_kind':'minimum', 'question_product_id':'facts-card'}
                if reference is not None:
                    fields['question_product_reference'] = reference
                graph.user('question', **fields)
                speech = graph.vars['fb_speech']
                self.assertIn('credit card ending 7492', speech)
                self.assertIn('208.14', speech)
                self.assertIn('personal loan', speech)
                self.assertIn('179.03', speech)
                self.assertEqual(self.stable_state(graph), before)
        self.assertEqual(bank.ledger['promises'], [])
        graph.user('question', question_kind='minimum', question_product_id='facts-card',
                   question_product_reference='credit card ending 7492')
        self.assertIn('208.14', graph.vars['fb_speech'])
        self.assertNotIn('personal loan', graph.vars['fb_speech'])
        self.assertEqual(self.stable_state(graph), before)

    def test_compound_question_retains_hardship_and_support_decline(self):
        graph, bank = self.graph()
        graph.vars['fb_question_kind'] = 'minimum'
        graph.user('hardship')
        self.assertEqual(graph.node, 'speak_support')
        self.assertIn('208.14', graph.vars['fb_speech'])
        self.assertIn('customer support', graph.vars['fb_speech'])
        self.assertEqual(graph.state['products'][0]['attempts_used'], 0)
        graph.vars['fb_question_kind'] = 'balance'
        graph.user('support_no')
        self.assertEqual(graph.node, 'speak_farewell')
        self.assertIn('693.81', graph.vars['fb_speech'])
        graph.finish()
        self.assertEqual(bank.ledger['outcome'], 'no_promise')
        self.assertEqual(bank.ledger['reason'], 'hardship')

    def test_unverified_fact_queries_never_disclose_accounts(self):
        graph, bank = self.graph(verified=False)
        graph.user('person_yes')
        for kind in ('minimum', 'deadline', 'balance'):
            graph.user('question', question_kind=kind)
            self.assertEqual(graph.vars['fb_speech'], 'I do not have that information.')
            self.assertEqual(graph.node, 'speak_identity')
        self.assertEqual(bank.ledger['calls'], [])

    def test_independent_known_question_does_not_become_unparsed_or_stale(self):
        graph, bank = self.graph()
        graph.vars['fb_question_kind'] = 'minimum'
        graph.user('unparsed')
        self.assertIn('208.14', graph.vars['fb_speech'])
        self.assertNotIn('clarify', graph.vars['fb_speech'])
        self.assertEqual(graph.vars['fb_question_kind'], 'none')
        graph.user('payment', amount_kind='explicit', amount='341.27', day={'kind':'tomorrow'})
        self.assertNotIn('smallest payment', graph.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])

    def test_new_fact_templates_are_required_without_baked_values(self):
        contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
        for name in ('floor_answer', 'deadline_answer'):
            plan = fake_plan(self.documents, True, contracts)
            del plan['plans'][0]['speech_templates'][name]
            with self.subTest(name=name), self.assertRaises(PlanStructureError):
                validate_plan(plan, self.documents, self.b, contracts)
        plan = fake_plan(self.documents, True, contracts)
        plan['plans'][0]['speech_templates']['floor_answer'] = 'For [[product]], the smallest payment is 100 and [[amount]].'
        with self.assertRaises(InvalidInput):
            validate_plan(plan, self.documents, self.b, contracts)
        for template in ('Can you pay [[amount]] for [[product]]?',
                         'Your [[amount]] for [[product]] is confirmed.'):
            plan = fake_plan(self.documents, True, contracts)
            plan['plans'][0]['speech_templates']['floor_answer'] = template
            with self.subTest(template=template), self.assertRaises(InvalidInput):
                validate_plan(plan, self.documents, self.b, contracts)
        graph, _ = self.graph()
        schemas = [e for node in graph.workflow['nodes'].values() for e in node.get('extraction_schema', [])
                   if e.get('name') == 'fb_question_kind']
        self.assertTrue(schemas)
        for schema in schemas:
            for kind in ('minimum', 'deadline', 'balance'):
                self.assertIsNotNone(re.fullmatch(schema['validation_pattern'], kind))
