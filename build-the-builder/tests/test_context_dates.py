"""Resolve date references from an existing offer without inventing a week."""
from datetime import date, datetime

from builder.contracts import Contracts
from builder.inputs import InvalidInput, read_json
from builder.local_bank import LocalBank
from builder.rules import Calendar
from builder.workflow import compile_workflow
from builder.workflow_runtime import payment_day
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import fake_plan, LocalGraph


class ContextDateTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.calendar = Calendar(self.a)
        self.now = datetime(2031, 4, 9, 11, 0)  # Wednesday.
        self.prior = date(2031, 4, 18)  # Existing offer is next week's Friday.

    def resolve(self, spec, prior=None):
        return payment_day(spec, self.now, self.calendar, prior)

    def test_contextual_offer_references_keep_existing_date_not_nearest_weekday(self):
        for phrase in ('that same Friday', 'that Friday', 'same day', 'as we said'):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.resolve({'kind': 'offer_date', 'source_text': phrase}, self.prior), self.prior)

    def test_offer_reference_requires_an_existing_valid_date_and_quoted_context(self):
        for prior in (None, 'not-a-date', 7):
            with self.subTest(prior=prior), self.assertRaises(InvalidInput):
                self.resolve({'kind': 'offer_date', 'source_text': 'that same Friday'}, prior)
        for phrase in ('', 'when my salary arrives', 'April thirty-first'):
            with self.subTest(phrase=phrase), self.assertRaises(InvalidInput):
                self.resolve({'kind': 'offer_date', 'source_text': phrase}, self.prior)

    def test_quoted_weekday_must_match_the_existing_offer_date(self):
        for spec, prior in (({'kind': 'offer_date', 'source_text': 'that same Thursday'}, self.prior),
                            ({'kind': 'this_weekday', 'weekday': 4, 'source_text': 'Friday'}, date(2031, 4, 17))):
            with self.subTest(spec=spec), self.assertRaises(InvalidInput):
                self.resolve(spec, prior)

    def test_bare_weekday_can_resolve_existing_offer_but_cannot_invent_a_week(self):
        spec = {'kind': 'this_weekday', 'weekday': 4, 'source_text': 'Friday'}
        self.assertEqual(self.resolve(spec, self.prior), self.prior)
        with self.assertRaises(InvalidInput):
            self.resolve(spec)

    def test_reference_with_explicit_day_number_must_match_previous_date(self):
        for phrase in ('Friday the eighteenth', 'Friday the 18th'):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.resolve({'kind': 'this_weekday', 'weekday': 4, 'source_text': phrase},
                                              self.prior), self.prior)
        for phrase in ('Friday the eleventh', 'Friday the 25th'):
            with self.subTest(phrase=phrase), self.assertRaises(InvalidInput):
                self.resolve({'kind': 'this_weekday', 'weekday': 4, 'source_text': phrase}, self.prior)

    def test_this_and_next_are_computed_from_supplied_now_despite_wrong_model_enum(self):
        specs = [({'kind': 'next_weekday', 'weekday': 0, 'source_text': 'this Friday'}, date(2031, 4, 11)),
                 ({'kind': 'this_weekday', 'weekday': 0, 'source_text': 'next Friday'}, date(2031, 4, 18))]
        for spec, expected in specs:
            with self.subTest(spec=spec):
                self.assertEqual(self.resolve(spec, date(2031, 4, 25)), expected)

    def test_multiple_relative_weekday_phrases_require_clarification(self):
        with self.assertRaises(InvalidInput):
            self.resolve({'kind': 'offer_date', 'source_text': 'this Friday or next Monday'}, self.prior)

    def test_explicit_iso_date_is_never_replaced_with_the_previous_offer(self):
        self.assertEqual(self.resolve({'kind': 'iso', 'value': '2031-04-16', 'source_text': '16 April'}, self.prior),
                         date(2031, 4, 16))
        for value in ('2031-02-30', 'Friday', '', None):
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                self.resolve({'kind': 'iso', 'value': value, 'source_text': 'that same Friday'}, self.prior)

    def test_malformed_absolute_phrase_cannot_be_reinterpreted_as_bare_weekday(self):
        with self.assertRaises(InvalidInput):
            self.resolve({'kind': 'this_weekday', 'weekday': 4,
                          'source_text': 'Friday the thirty-first of February'}, self.prior)

    def test_today_and_tomorrow_do_not_inherit_previous_offer(self):
        self.assertEqual(self.resolve({'kind': 'today'}, self.prior), date(2031, 4, 9))
        self.assertEqual(self.resolve({'kind': 'tomorrow'}, self.prior), date(2031, 4, 10))

    def test_emitted_floor_correction_and_acceptance_preserve_contextual_day(self):
        contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
        workflow, report = compile_workflow(fake_extraction(self.documents[:1], self.a_raw),
            fake_plan(self.documents[:1], False, contracts), self.documents[:1], contracts)
        bank = LocalBank({'date_of_birth': '1984-05-12', 'phone_last4': '7063'},
            [{'product_id': 'synthetic-context-card', 'type': 'credit_card', 'card_last4': '5093',
              'past_due': 400, 'minimum_payment': 120, 'days_late': 4}], wrong_answer_limit=3)
        graph = LocalGraph(workflow, bank, {'now': self.now.isoformat()}, report['runtime_payload']['bindings'])
        graph.user('person_yes')
        graph.user('identity_answer', answer='1984-05-12', answer_complete=True)
        graph.user('identity_answer', answer='7063', answer_complete=True)
        graph.user('payment', amount_kind='full', day={'kind': 'this_weekday', 'weekday': 4, 'source_text': 'this Friday'})
        self.assertEqual(graph.node, 'speak_confirm')
        graph.user('correction', amount_kind='explicit', amount='90',
                   day={'kind': 'offer_date', 'source_text': 'that same Friday'})
        self.assertEqual(graph.node, 'speak_negotiate')
        self.assertEqual(graph.state['offers']['synthetic-context-card'], {'amount': '120.00', 'date': '2031-04-11'})
        self.assertIn('120.00', graph.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])
        graph.user('accept_offer', day={'kind': 'this_weekday', 'weekday': 4, 'source_text': 'Friday'})
        self.assertEqual(graph.node, 'speak_confirm')
        self.assertEqual(graph.state['promises_to_confirm'],
                         [{'product_id': 'synthetic-context-card', 'amount': '120.00', 'date': '2031-04-11'}])
        self.assertEqual(bank.ledger['promises'], [])
        graph.user('confirm', explicit_yes=True)
        graph.finish()
        self.assertEqual(bank.ledger['promises'],
                         [{'product_id': 'synthetic-context-card', 'amount': 120, 'date': '2031-04-11'}])

    def test_collective_same_day_reference_cannot_preserve_three_different_dates(self):
        contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
        workflow, report = compile_workflow(fake_extraction(self.documents, self.b_raw),
            fake_plan(self.documents, True, contracts), self.documents, contracts)
        bank = LocalBank({'date_of_birth': '1984-05-12', 'phone_last4': '7063'},
            [{'product_id': 'card-A', 'type': 'credit_card', 'card_last4': '5093',
              'past_due': 500, 'minimum_payment': 100, 'days_late': 4},
             {'product_id': 'loan-B', 'type': 'personal_loan', 'past_due': 700,
              'instalment_amount': 100, 'days_late': 4},
             {'product_id': 'loan-C', 'type': 'car_loan', 'past_due': 900,
              'instalment_amount': 100, 'days_late': 4}], wrong_answer_limit=3)
        graph = LocalGraph(workflow, bank, {'now': self.now.isoformat()}, report['runtime_payload']['bindings'])
        graph.user('person_yes')
        graph.user('identity_answer', answer='1984-05-12', answer_complete=True)
        graph.user('identity_answer', answer='7063', answer_complete=True)
        for product, label, day in (('card-A', 'credit card ending 5093', '2031-04-10'),
                                    ('loan-B', 'personal loan', '2031-04-11'),
                                    ('loan-C', 'car loan', '2031-04-14')):
            graph.user('payment', product_id=product, product_reference=label,
                       amount_kind='full', day={'kind': 'iso', 'value': day})
        self.assertEqual(graph.node, 'speak_confirm')
        self.assertEqual(len(graph.state['promises_to_confirm']), 3)
        graph.user('correction', scope='all', day={'kind': 'offer_date', 'source_text': 'the same day'})
        self.assertNotEqual(graph.node, 'speak_confirm')
        self.assertEqual(graph.state['promises_to_confirm'], [])
        self.assertEqual(bank.ledger['promises'], [])
        graph.user('confirm', explicit_yes=True)
        self.assertEqual(bank.ledger['promises'], [])
