"""Spoken account references must identify exactly one authoritative product."""
from copy import deepcopy
import json

from builder.contracts import Contracts
from builder.inputs import read_json
from builder.local_bank import LocalBank
from builder.workflow import compile_workflow
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import LocalGraph, fake_plan


class ProductReferenceTests(OfflineCase):
    @classmethod
    def setUpClass(cls):
        cls.compiled = None

    def graph(self, suffix, other_suffix=None):
        if self.compiled is None:
            contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
            type(self).compiled = compile_workflow(fake_extraction(self.documents, self.b_raw),
                fake_plan(self.documents, True, contracts), self.documents, contracts)
        workflow, report = self.compiled
        product = dict(product_id='selected-card', type='credit_card', card_last4=suffix,
                       past_due=753.42, minimum_payment=219.17, days_late=6)
        other = (dict(product, product_id='other-card', card_last4=other_suffix) if other_suffix is not None else
                 dict(product_id='other-loan', type='personal_loan', past_due=1084.71,
                      instalment_amount=357.83, days_late=17))
        bank = LocalBank({'date_of_birth':'1987-09-23', 'phone_last4':'6082'}, [product, other],
                         wrong_answer_limit=self.b_raw['wrong_answer_limit'])
        graph = LocalGraph(workflow, bank, {'now':'2028-02-16T12:35'}, report['runtime_payload']['bindings'])
        graph.user('person_yes')
        graph.user('identity_answer', answer='1987-09-23', answer_complete=True)
        graph.user('identity_answer', answer='6082', answer_complete=True)
        return graph, bank

    def test_spoken_suffixes_preserve_exact_product_and_require_fresh_confirmation(self):
        cases = [
            ('4570', None, 'credit card ending 4570', True),
            ('4570', None, 'credit card', True),
            ('4570', None, 'credit card ending four five seven zero', True),
            ('0475', None, 'credit card ending oh four seven five', True),
            ('4407', None, 'credit card ending double four zero seven', True),
            ('7770', None, 'credit card ending triple seven zero', True),
            ('4570', None, 'credit card ending four five seven one', False),
            ('4407', None, 'credit card ending double four zero eight', False),
            ('4570', None, 'that account', False),
            ('7740', '7704', 'credit card ending 7740', True),
            ('7740', '7704', 'credit card ending seven seven four zero', True),
            ('7740', '7704', 'credit card ending double seven four zero', True),
            ('7740', '7704', 'credit card ending seven seven zero four', False),
            ('7740', '7704', 'credit card ending seven seven', False),
            ('7740', '7704', 'credit card', False),
            ('7740', '7740', 'credit card ending seven seven four zero', False),
            ('04750', None, 'credit card ending zero four seven five zero', True),
            ('4570', None, 'credit card ending 4 5 7 0', True),
            ('4570', None, 'credit card ending 4570 or 9999', False),
            ('4570', None, 'personal loan ending four five seven zero', False),
            ('4570', None, 'credit card ending 14570', False),
            ('4570', None, 'credit card ending 4570 or personal loan', False),
            ('4570', None, 'personal loan and credit card ending four five seven zero', False),
        ]
        for suffix, other, reference, expected in cases:
            with self.subTest(suffix=suffix, other=other, reference=reference):
                graph, bank = self.graph(suffix, other)
                before = deepcopy(bank.ledger)
                graph.user('payment', product_id='selected-card', product_reference=reference,
                           amount_kind='full', day={'kind':'tomorrow'})
                drafts = graph.state['promises_to_confirm']
                self.assertEqual(any(p['product_id']=='selected-card' for p in drafts), expected)
                self.assertFalse(any(p['product_id']!='selected-card' for p in drafts))
                self.assertEqual(bank.ledger, before)
                if not expected:
                    self.assertIn('Which product', graph.vars['fb_speech'])

    def test_spoken_fact_question_target_cannot_retarget_an_existing_promise(self):
        graph, bank = self.graph('0475')
        graph.user('payment', product_id='other-loan', amount_kind='full', day={'kind':'tomorrow'})
        before = deepcopy(graph.state['promises_to_confirm'])
        ledger = deepcopy(bank.ledger)
        graph.user('question', question_kind='minimum', question_product_id='selected-card',
                   question_product_reference='credit card ending zero four seven five')
        self.assertIn('219.17', graph.vars['fb_speech'])
        self.assertNotIn('357.83', graph.vars['fb_speech'])
        self.assertEqual(graph.state['promises_to_confirm'], before)
        self.assertEqual(bank.ledger, ledger)

    def test_unique_reference_without_an_id_selects_that_product(self):
        # Live B055 PUB-12 extraction: {"action":"payment","product_reference":"credit card"}.
        graph, bank = self.graph('4570')
        graph.user('payment', product_reference='credit card', amount_kind='full', day={'kind': 'tomorrow'})
        self.assertEqual([p['product_id'] for p in graph.state['promises_to_confirm']], ['selected-card'])
        self.assertNotIn('Which product', graph.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])

    def test_ambiguous_or_ungrounded_reference_without_an_id_still_clarifies(self):
        for suffix, other, reference in (('7740', '7704', 'credit card'), ('4570', None, 'that account'),
                                         ('4570', None, 'credit card ending 9999')):
            with self.subTest(reference=reference, other=other):
                graph, bank = self.graph(suffix, other)
                graph.user('payment', product_reference=reference, amount_kind='full', day={'kind': 'tomorrow'})
                self.assertEqual(graph.state['promises_to_confirm'], [])
                self.assertIn('Which product', graph.vars['fb_speech'])

    def test_yes_naming_one_product_does_not_confirm_a_combined_readback(self):
        # Live B055 DEV-B-AMBIGUOUS-KNOWN-FACTS: "Yes, I confirm the credit card"
        # recorded all three promises.
        graph, bank = self.graph('4570')
        graph.user('accept_offer', scope='all', amount_kind='full')
        self.assertEqual(graph.node, 'speak_confirm')
        self.assertEqual(len(graph.state['promises_to_confirm']), 2)
        for named in ({'product_reference': 'credit card'}, {'product_id': 'selected-card'}):
            with self.subTest(named=named):
                graph.user('confirm', explicit_yes=True, **named)
                self.assertEqual(graph.node, 'speak_confirm')
                self.assertEqual(bank.ledger['promises'], [])
                self.assertIn('personal loan', graph.vars['fb_speech'])
        graph.user('confirm', explicit_yes=True, scope='all')
        graph.finish()
        self.assertEqual(sorted(p['product_id'] for p in bank.ledger['promises']), ['other-loan', 'selected-card'])

    def test_combined_readback_writes_only_on_an_all_products_confirmation(self):
        # Live B057 DEV-B-AMBIGUOUS-KNOWN-FACTS: "Yes, I confirm the credit card two
        # hundred and twenty" arrived as a bare confirm with no product named.
        graph, bank = self.graph('4570')
        graph.user('accept_offer', scope='all', amount_kind='full')
        self.assertEqual(graph.node, 'speak_confirm')
        for scope in ('some', 'none'):
            with self.subTest(scope=scope):
                graph.user('confirm', explicit_yes=True, confirm_scope=scope)
                self.assertEqual(graph.node, 'speak_confirm')
                self.assertEqual(bank.ledger['promises'], [])
        # A failed or missing scope extraction is not a confirmation either.
        graph.vars.pop('fb_confirm_scope', None)
        graph.vars['fb_event'] = json.dumps({'action': 'confirm', 'explicit_yes': True})
        graph.advance('user_turn')
        self.assertEqual(graph.node, 'speak_confirm')
        self.assertEqual(bank.ledger['promises'], [])
        graph.user('confirm', explicit_yes=True, confirm_scope='all')
        graph.finish()
        self.assertEqual(sorted(p['product_id'] for p in bank.ledger['promises']), ['other-loan', 'selected-card'])

    def test_stale_all_scope_cannot_confirm_a_later_readback(self):
        graph, bank = self.graph('4570')
        graph.user('accept_offer', scope='all', amount_kind='full')
        graph.user('confirm', explicit_yes=True, confirm_scope='some')
        self.assertEqual(graph.vars['fb_confirm_scope'], 'none')
        graph.vars['fb_event'] = json.dumps({'action': 'confirm', 'explicit_yes': True})
        graph.advance('user_turn')
        self.assertEqual(bank.ledger['promises'], [])

    def test_multi_product_decision_phases_require_a_reference_with_an_id(self):
        import re
        self.graph('4570')
        contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
        docs = self.documents[:1]
        single, _ = compile_workflow(fake_extraction(docs, self.a_raw), fake_plan(docs, False, contracts), docs, contracts)
        multi = self.compiled[0]

        def accepts(workflow, node, value):
            schema = next(s for s in workflow['nodes'][node]['extraction_schema'] if s['name'] == 'fb_event')
            return re.search(schema['validation_pattern'], value) is not None

        bare = '{"action":"payment","product_id":"p1","day":{"kind":"tomorrow","source_text":"tomorrow"}}'
        always_valid = ['{"action":"payment","product_id":"p1","product_reference":"credit card"}',
                        '{"action": "unparsed"}', '{"action":"confirm","explicit_yes":true}',
                        '{"action":"accept_offer","scope":"all","amount_kind":"full"}',
                        '{"action":"question","question_kind":"minimum","question_product_id":"p1"}']
        for workflow, node, required in ((multi, 'speak_all_today', True), (multi, 'speak_confirm', True),
                                         (multi, 'speak_negotiate', False), (single, 'speak_confirm', False),
                                         (single, 'speak_negotiate', False)):
            with self.subTest(node=node, required=required):
                self.assertEqual(accepts(workflow, node, bare), not required)
                for value in always_valid:
                    self.assertTrue(accepts(workflow, node, value), value)
                self.assertFalse(accepts(workflow, node, '{"action":"identity_answer","answer":"x"}'))
