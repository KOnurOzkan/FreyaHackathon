"""Replay real wire failure shapes with unrelated account values and dates.

These execute compiled code and do not assume the speech model always follows
its prompt. No practice case IDs or customer-specific values enter the builder.
"""
from copy import deepcopy
import json
import re

from builder.contracts import Contracts
from builder.inputs import read_json
from builder.local_bank import LocalBank
from builder.workflow import compile_workflow, extraction_schema
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import fake_plan, LocalGraph


class ExtractionBoundaryTests(OfflineCase):
    compiled = {}

    def graph(self, multiple=False, verified=True):
        if multiple not in self.compiled:
            docs = self.documents if multiple else self.documents[:1]
            contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
            self.compiled[multiple] = compile_workflow(
                fake_extraction(docs, self.b_raw if multiple else self.a_raw),
                fake_plan(docs, multiple, contracts), docs, contracts)
        workflow, report = self.compiled[multiple]
        products = [{'product_id':'wire-card','type':'credit_card','card_last4':'8034',
                     'past_due':987.64,'minimum_payment':296.29,'days_late':12}]
        if multiple:
            products.append({'product_id':'wire-loan','type':'personal_loan',
                             'past_due':1245.80,'instalment_amount':411.23,'days_late':4})
        bank = LocalBank({'date_of_birth':'1983-02-15','phone_last4':'0461'}, products, wrong_answer_limit=3)
        graph = LocalGraph(workflow, bank, {'now':'2028-05-09T13:25'}, report['runtime_payload']['bindings'])
        if verified:
            graph.user('person_yes')
            graph.user('identity_answer', answer='1983-02-15', answer_complete=True)
            graph.user('identity_answer', answer='0461', answer_complete=True)
        return graph, bank

    def wire(self, graph, event, question='none', scope='none'):
        # Deliberately bypass LocalGraph.user's inferred defaults and references.
        graph.vars['fb_event'] = json.dumps(event)
        graph.vars['fb_question_kind'] = question
        graph.vars['fb_confirm_scope'] = scope
        graph.advance('user_turn')

    def state(self, graph):
        return {k:v for k,v in graph.state.items() if k != 'instruction'}

    def test_minimum_alias_reads_back_bank_floor_without_recording_until_confirmation(self):
        g, bank = self.graph()
        g.user('too_much')
        self.wire(g, {'action':'payment','amount_kind':'minimum','day':{'kind':'tomorrow'}})
        self.assertEqual(g.node, 'speak_confirm')
        self.assertIn('296.29 on 10 May 2028', g.vars['fb_speech'])
        self.assertNotIn('987.64', g.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])
        # The floor offer after "too much" is persuasion attempt 1 (be-04, as
        # the multi-product path already did); the proposal itself uses none.
        self.assertEqual(g.state['products'][0]['attempts_used'], 1)
        self.wire(g, {'action':'confirm','explicit_yes':True})
        g.finish()
        self.assertEqual([(p['amount'],p['date']) for p in bank.ledger['promises']], [(296.29,'2028-05-10')])
        self.assertEqual([c['tool'] for c in bank.ledger['calls']].count('create_promise'), 1)

    def test_minimum_alias_uses_each_product_floor_for_combined_readback(self):
        g, bank = self.graph(True)
        self.wire(g, {'action':'payment','scope':'all','amount_kind':'minimum','day':{'kind':'tomorrow'}})
        self.assertEqual(g.node, 'speak_confirm')
        self.assertIn('296.29', g.vars['fb_speech'])
        self.assertIn('411.23', g.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])
        self.wire(g, {'action':'confirm','explicit_yes':True}, scope='all')
        g.finish()
        self.assertEqual([p['amount'] for p in bank.ledger['promises']], [296.29,411.23])

    def test_unknown_or_malformed_amount_labels_never_default_to_an_amount(self):
        for value in ('minimumm','discount','maximum','',None,True,0,[],{}):
            with self.subTest(value=value):
                g, bank = self.graph()
                before = deepcopy(bank.ledger)
                self.wire(g, {'action':'payment','amount_kind':value,'day':{'kind':'tomorrow'}})
                self.assertEqual(g.node, 'speak_negotiate')
                self.assertIn('clarify', g.vars['fb_speech'].lower())
                self.assertEqual(bank.ledger, before)
                self.assertEqual(g.state['products'][0]['attempts_used'], 0)

    def test_invalid_correction_cannot_leave_a_stale_promise_writable(self):
        for value in ('discount', [], None):
            with self.subTest(value=value):
                g, bank = self.graph()
                g.user('payment', amount_kind='explicit', amount='500.17', day={'kind':'tomorrow'})
                self.wire(g, {'action':'correction','amount_kind':value,'day':{'kind':'tomorrow'}})
                self.wire(g, {'action':'confirm','explicit_yes':True})
                self.assertEqual(bank.ledger['promises'], [])
                self.assertNotEqual(g.node, 'speak_abort')

    def test_label_with_conflicting_numeric_amount_needs_clarification(self):
        for value in ('minimum','floor','full','offer'):
            with self.subTest(value=value):
                g, bank = self.graph()
                self.wire(g, {'action':'payment','amount_kind':value,'amount':'123.45','day':{'kind':'tomorrow'}})
                self.assertEqual(g.node, 'speak_negotiate')
                self.assertIn('clarify', g.vars['fb_speech'].lower())
                self.assertEqual(bank.ledger['promises'], [])

    def test_platform_amount_label_constraint_accepts_alias_and_rejects_unknown_values(self):
        for phase in ('negotiate','all_today','confirm'):
            schema = next(s for s in extraction_schema(phase=phase, multi=True) if s['name']=='fb_event')
            for kind in ('full','floor','minimum','offer','explicit'):
                for compact in (True, False):
                    event = json.dumps({'action':'payment','amount_kind':kind,'day':{'kind':'tomorrow'}},
                                       separators=(',',':') if compact else None)
                    self.assertIsNotNone(re.fullmatch(schema['validation_pattern'], event), event)
            for kind in ('discount','minimumm',True,None,[],4):
                self.assertIsNone(re.fullmatch(schema['validation_pattern'], json.dumps({'action':'payment','amount_kind':kind})))
            self.assertIsNotNone(re.fullmatch(schema['validation_pattern'], '{"action":"unparsed"}'))

    def test_conflicting_fact_questions_clarify_without_changing_offer_or_counters(self):
        for first,second in [('minimum','balance'),('balance','minimum'),('deadline','minimum'),('minimum','other')]:
            with self.subTest(first=first,second=second):
                g, bank = self.graph()
                g.user('too_much')
                state, ledger = self.state(g), deepcopy(bank.ledger)
                self.wire(g, {'action':'question','question_kind':first}, second)
                self.assertEqual(g.vars['fb_speech'], 'Could you clarify your answer?')
                self.assertEqual(self.state(g), state)
                self.assertEqual(bank.ledger, ledger)
                self.assertEqual(g.vars['fb_question_kind'], 'none')
                self.wire(g, {'action':'question','question_kind':'minimum'})
                self.assertIn('296.29', g.vars['fb_speech'])
                self.assertNotIn('987.64', g.vars['fb_speech'])

    def test_question_conflict_during_confirmation_does_not_authorize_a_write(self):
        g, bank = self.graph()
        g.user('payment', amount_kind='explicit', amount='500.17', day={'kind':'tomorrow'})
        self.wire(g, {'action':'confirm','explicit_yes':True,'question_kind':'minimum'}, 'balance')
        self.assertEqual(g.node, 'speak_confirm')
        self.assertEqual(g.vars['fb_speech'], 'Could you clarify your answer?')
        self.assertEqual(bank.ledger['promises'], [])
        self.wire(g, {'action':'question','question_kind':'minimum'})
        self.assertIn('296.29', g.vars['fb_speech'])
        self.wire(g, {'action':'confirm','explicit_yes':True})
        self.assertEqual(bank.ledger['promises'][0]['amount'], 500.17)

    def test_conflict_preserves_a_separate_payment_proposal_and_product_target(self):
        g, bank = self.graph(True)
        self.wire(g, {'action':'payment','product_id':'wire-loan','product_reference':'personal loan',
                     'amount_kind':'explicit','amount':'600.37','day':{'kind':'tomorrow'},
                     'question_kind':'minimum','question_product_id':'wire-card',
                     'question_product_reference':'credit card ending 8034'}, 'balance')
        self.assertIn('Could you clarify your answer?', g.vars['fb_speech'])
        self.assertEqual(g.state['promises_to_confirm'][0]['product_id'], 'wire-loan')
        self.assertEqual(g.state['promises_to_confirm'][0]['amount'], '600.37')
        self.assertEqual(bank.ledger['promises'], [])

    def test_conflict_never_discloses_account_facts_before_verification(self):
        g, bank = self.graph(verified=False)
        g.user('person_yes')
        self.wire(g, {'action':'question','question_kind':'minimum'}, 'balance')
        self.assertEqual(g.vars['fb_speech'], 'Could you clarify your answer?')
        self.assertEqual(g.node, 'speak_identity')
        self.assertEqual(bank.ledger['calls'], [])

    def test_conflict_does_not_bypass_repeat_privacy_refusal_limit(self):
        g, bank = self.graph(verified=False)
        g.user('person_yes')
        for _ in range(2):
            self.wire(g, {'action':'question','question_kind':'purpose'}, 'minimum')
        self.assertEqual(g.node, 'speak_farewell')
        g.finish()
        self.assertEqual((bank.ledger['outcome'],bank.ledger['reason']), ('not_verified','declined'))

    def test_agreement_and_absent_question_flag_keep_correct_fact_answer(self):
        g, bank = self.graph()
        for flag in ('none','minimum'):
            self.wire(g, {'action':'question','question_kind':'minimum'}, flag)
            self.assertIn('296.29', g.vars['fb_speech'])
        self.wire(g, {'action':'unparsed'}, 'balance')
        self.assertIn('987.64', g.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])

    def test_malformed_question_kind_does_not_crash_or_authorize_confirmation(self):
        g, bank = self.graph()
        for kind in ([],{},True,'invented_fact'):
            self.wire(g, {'action':'question','question_kind':kind})
            self.assertEqual(g.vars['fb_speech'], 'Could you clarify your answer?')
            self.assertNotEqual(g.node, 'speak_abort')
        self.assertEqual(bank.ledger['promises'], [])
