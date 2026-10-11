"""Ancillary questions must survive the primary action without relaxing privacy."""
import re

from builder.contracts import Contracts
from builder.inputs import read_json
from builder.local_bank import LocalBank
from builder.workflow import compile_workflow
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import LocalGraph, fake_plan


class QuestionInterruptTests(OfflineCase):
    def graph(self, verified=False):
        documents = self.documents[:1]
        contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
        workflow, report = compile_workflow(fake_extraction(documents, self.a_raw),
            fake_plan(documents, False, contracts), documents, contracts)
        bank = LocalBank({'date_of_birth': '1985-08-19', 'phone_last4': '0276'}, [
            {'product_id': 'question-review-card', 'type': 'credit_card', 'card_last4': '7492',
             'past_due': 693.81, 'minimum_payment': 208.14, 'days_late': 15}], wrong_answer_limit=3)
        graph = LocalGraph(workflow, bank, {'now': '2028-05-09T13:25'}, report['runtime_payload']['bindings'])
        if verified:
            graph.user('person_yes')
            graph.user('identity_answer', answer='1985-08-19', answer_complete=True)
            graph.user('identity_answer', answer='0276', answer_complete=True)
        return graph, bank

    def assert_private(self, speech):
        for fact in ('693.81', '208.14', '7492', 'credit card', 'collections', 'past due'):
            self.assertNotIn(fact, speech)

    def test_independent_unknown_question_survives_hardship_and_support_decline(self):
        graph, bank = self.graph(True)
        graph.vars['fb_question_kind'] = 'other'
        graph.user('hardship')
        self.assertEqual(graph.node, 'speak_support')
        self.assertEqual(graph.vars['fb_speech'].count('I do not have that information.'), 1)
        self.assertIn('customer support', graph.vars['fb_speech'])
        self.assertEqual(graph.state['products'][0]['attempts_used'], 0)
        graph.vars['fb_question_kind'] = 'other'
        graph.user('support_no')
        self.assertEqual(graph.node, 'speak_farewell')
        self.assertTrue(graph.vars['fb_speech'].startswith('I do not have that information.'))
        graph.finish()
        self.assertEqual((bank.ledger['outcome'], bank.ledger['reason']), ('no_promise', 'hardship'))
        self.assertEqual(bank.ledger['transfers'], [])
        self.assertEqual(bank.ledger['promises'], [])

    def test_other_question_keeps_payment_readback_and_clears_before_next_turn(self):
        graph, bank = self.graph(True)
        graph.vars['fb_question_kind'] = 'other'
        graph.user('payment', amount_kind='explicit', amount='341.27', day={'kind': 'tomorrow'})
        self.assertEqual(graph.node, 'speak_confirm')
        self.assertTrue(graph.vars['fb_speech'].startswith('I do not have that information.'))
        self.assertIn('341.27 on 10 May 2028', graph.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])
        self.assertEqual(graph.vars['fb_question_kind'], 'none')
        graph.user('confirm', explicit_yes=True)
        self.assertNotIn('I do not have that information.', graph.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'][0]['amount'], 341.27)

    def test_standalone_other_question_does_not_duplicate_its_reply(self):
        graph, bank = self.graph(True)
        graph.vars['fb_question_kind'] = 'other'
        graph.user('question', question_kind='other')
        self.assertEqual(graph.vars['fb_speech'].count('I do not have that information.'), 1)
        self.assertEqual(bank.ledger['promises'], [])

    def test_purpose_question_instead_of_security_answer_retains_privacy_refusal_limit(self):
        graph, bank = self.graph()
        graph.user('person_yes')
        graph.vars['fb_question_kind'] = 'purpose'
        graph.user('question')
        self.assertEqual(graph.node, 'speak_identity')
        self.assertIn('These questions protect your account.', graph.vars['fb_speech'])
        self.assert_private(graph.vars['fb_speech'])
        graph.vars['fb_question_kind'] = 'purpose'
        graph.user('question')
        self.assertEqual(graph.node, 'speak_farewell')
        graph.finish()
        self.assertEqual((bank.ledger['outcome'], bank.ledger['reason']), ('not_verified', 'declined'))
        self.assertEqual([row['tool'] for row in bank.ledger['calls']], ['close_call'])

    def test_purpose_alongside_complete_security_answer_does_not_become_a_refusal(self):
        graph, bank = self.graph()
        graph.user('person_yes')
        graph.vars['fb_question_kind'] = 'purpose'
        graph.user('identity_answer', answer='1985-08-19', answer_complete=True)
        self.assertEqual(graph.node, 'speak_identity')
        self.assertIn('last four', graph.vars['fb_speech'])
        self.assert_private(graph.vars['fb_speech'])
        graph.user('question', question_kind='purpose')
        self.assertEqual(graph.node, 'speak_identity')
        self.assertIn('These questions protect your account.', graph.vars['fb_speech'])
        self.assertEqual([row['tool'] for row in bank.ledger['calls']], ['verify_identity'])

    def test_relative_purpose_question_uses_a_private_callback_path_without_scam_claim(self):
        graph, bank = self.graph()
        graph.vars['fb_question_kind'] = 'purpose'
        graph.user('other_person')
        self.assertEqual(graph.node, 'speak_callback')
        self.assertIn('When can the customer be reached?', graph.vars['fb_speech'])
        self.assertNotIn('verify this call', graph.vars['fb_speech'])
        self.assert_private(graph.vars['fb_speech'])
        self.assertEqual(bank.ledger['calls'], [])

    def test_question_extractor_accepts_other_and_purpose_without_changing_action_schema(self):
        graph, _ = self.graph()
        schemas = [entry for node in graph.workflow['nodes'].values()
                   for entry in node.get('extraction_schema', []) if entry.get('name') == 'fb_question_kind']
        self.assertTrue(schemas)
        for schema in schemas:
            for value in ('none', 'ai', 'scam', 'other', 'purpose'):
                self.assertIsNotNone(re.fullmatch(schema['validation_pattern'], value), value)
