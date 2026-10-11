"""P0 regression gates with varied inputs; no public-case lookup in the builder."""
from copy import deepcopy
from builder.contracts import Contracts
from builder.inputs import InvalidInput, read_json
from builder.local_bank import LocalBank
from builder.workflow import compile_workflow
from builder.workflow_runtime import normalize_birth_date, normalize_digits, normalize_last_four, normalize_year
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import fake_plan, LocalGraph


class StrictRegressions(OfflineCase):
    def test_unparsed_person_replies_respect_the_total_ask_limit(self):
        graph, bank = self.graph()
        graph.user('unparsed')
        self.assertEqual(graph.node, 'speak_person')
        graph.user('unparsed')
        self.assertEqual(graph.node, 'speak_farewell')
        graph.finish()
        self.assertEqual([call['tool'] for call in bank.ledger['calls']], ['close_call'])
        self.assertEqual((bank.ledger['outcome'], bank.ledger['reason']), ('not_reached', 'unclear'))

    def verified_graph(self, **options):
        g, bank = self.graph(**options)
        g.user('person_yes')
        g.user('identity_answer', answer='1980-02-02', answer_complete=True)
        g.user('identity_answer', answer='9042', answer_complete=True)
        return g, bank

    def test_every_conversation_stage_uses_prepared_entry_speech(self):
        g, _ = self.graph()
        for name, node in g.workflow['nodes'].items():
            if node['node_type'] == 'conversation':
                self.assertEqual(node['on_enter']['type'], 'tts', name)
                self.assertEqual(node['tools'], [], name)

    def test_adjusted_offer_acceptance_cannot_skip_readback_even_if_mislabelled(self):
        g, bank = self.verified_graph()
        self.assertIn('817.23', g.vars['fb_speech'])
        self.assertIn('3186', g.vars['fb_speech'])
        g.user('payment', day={'kind':'iso','value':'2027-04-10'})
        self.assertIn('10 April 2027 is not a business day', g.vars['fb_speech'])
        self.assertIn('12 April 2027', g.vars['fb_speech'])
        g.user('confirm', explicit_yes=True)
        self.assertEqual(g.node, 'speak_confirm')
        self.assertIn('817.23 on 12 April 2027', g.vars['fb_speech'])
        self.assertIn('Do you confirm?', g.vars['fb_speech'])
        self.assertNotIn('recorded', g.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])
        g.user('confirm', explicit_yes=True)
        self.assertIn('text message', g.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'][0]['date'], '2027-04-12')

    def test_changed_confirmation_is_revalidated_and_read_back_before_write(self):
        g, bank = self.verified_graph()
        g.user('accept_offer')
        g.user('confirm', explicit_yes=True, amount_kind='explicit', amount='333.17',
               day={'kind':'iso','value':'2027-04-08'})
        self.assertEqual(g.node, 'speak_confirm')
        self.assertIn('333.17 on 8 April 2027', g.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])
        g.user('confirm', explicit_yes=True)
        self.assertEqual(bank.ledger['promises'][0]['amount'], 333.17)

    def test_failed_write_has_no_success_speech(self):
        g, bank = self.verified_graph(faults=[{'tool':'create_promise','fail_first_n':2}])
        g.user('accept_offer')
        g.user('confirm', explicit_yes=True)
        self.assertIn('could not record', g.vars['fb_speech'])
        self.assertNotIn('text message', g.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])

    def test_unknown_question_does_not_invent_an_answer(self):
        g, _ = self.verified_graph()
        g.user('question', question_kind='other')
        self.assertTrue(g.vars['fb_speech'].startswith('I do not have that information.'))

    def test_preverification_speech_does_not_contain_account_facts(self):
        g, _ = self.graph()
        g.user('person_yes')
        g.user('question', question_kind='other')
        for forbidden in ('817.23','3186','collections','credit card'):
            self.assertNotIn(forbidden, g.vars['fb_speech'])

    def graph(self, limit=3, retry_limit=2, faults=(), adapter=None):
        raw = deepcopy(self.a_raw)
        raw.update(wrong_answer_limit=limit, promise_attempt_limit=retry_limit)
        contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
        plan = fake_plan(self.documents[:1], False, contracts)
        workflow, report = compile_workflow(fake_extraction(self.documents[:1], raw), plan,
                                           self.documents[:1], contracts)
        bank = LocalBank({'date_of_birth': '1980-02-02', 'phone_last4': '9042'},
                         [{'product_id': 'unseen-product-Z', 'type': 'credit_card', 'card_last4': '3186',
                           'past_due': 817.23, 'minimum_payment': 251.19, 'days_late': 11}],
                         wrong_answer_limit=limit, faults=faults)
        graph = LocalGraph(workflow, bank, {'now': '2027-04-06T12:15'},
                           report['runtime_payload']['bindings'], adapter)
        return graph, bank

    def test_complete_word_dates_normalize_without_inventing_parts(self):
        for text, expected in [('twenty one July nineteen ninety', '1990-07-21'),
                               ('the fifth of March two thousand and one', '2001-03-05'),
                               ('April twenty-third nineteen oh six', '1906-04-23'),
                               ('December thirty first nineteen ninety nine', '1999-12-31'),
                               ('twenty-ninth February two thousand', '2000-02-29')]:
            with self.subTest(text=text):
                self.assertEqual(normalize_birth_date(text, 'day_month_year'), expected)
        for text in ('twenty one July', 'July nineteen ninety', 'thirty first February nineteen ninety',
                     'twenty one July ninety', 'July', 'one two July nineteen ninety'):
            with self.subTest(text=text), self.assertRaises(InvalidInput):
                normalize_birth_date(text, 'day_month_year')

    def test_spoken_digit_sequences_preserve_zeros_and_reject_missing_digits(self):
        for phrase, expected in [('oh three one seven', '0317'), ('double eight four six', '8846'),
                                 ('three 0 nine two', '3092'), ('9042', '9042')]:
            self.assertEqual(normalize_last_four(phrase), expected)
        for phrase in ('zero three', 'double', 'double double five', 'one two three four five'):
            with self.assertRaises(InvalidInput):
                normalize_last_four(phrase)

    def test_year_and_digit_code_answers_normalize_without_guessing(self):
        for phrase, expected in [('1988', '1988'), ('nineteen eighty-eight', '1988'), ('one nine eight eight', '1988'),
                                 ('two thousand five', '2005'), ('twenty twenty-one', '2021'),
                                 ('nineteen oh five', '1905')]:
            with self.subTest(phrase=phrase):
                self.assertEqual(normalize_year(phrase), expected)
        for phrase in ('eighty-eight', 'nineteen', 'one nine eight', 'in the eighties', ''):
            with self.subTest(phrase=phrase), self.assertRaises(InvalidInput):
                normalize_year(phrase)
        for phrase, expected in [('zero four one zero nine', '04109'), ('04109', '04109'),
                                 ('double oh seven', '007'), ('1 2 3 4 5 6 7 8', '12345678')]:
            with self.subTest(phrase=phrase):
                self.assertEqual(normalize_digits(phrase), expected)
        for phrase, length in (('four one', 5), ('SW1A 1AA', None), ('', None), ('double', None)):
            with self.subTest(phrase=phrase), self.assertRaises(InvalidInput):
                normalize_digits(phrase, length)

    def test_exhaustion_obeys_each_supplied_limit_and_does_not_reopen(self):
        for limit in (1, 2, 3, 4, 5):
            with self.subTest(limit=limit):
                graph, bank = self.graph(limit)
                graph.user('person_yes')
                for index in range(limit):
                    graph.user('identity_answer', answer='twenty one July nineteen ninety', answer_complete=True)
                    self.assertEqual(graph.state['wrong_answers_left'], limit-index-1)
                    if index < limit-1:
                        self.assertEqual(graph.node, 'speak_identity')
                        self.assertIn('What is your date of birth?', graph.vars['fb_speech'])
                        self.assertNotIn('branch', graph.vars['fb_speech'])
                self.assertEqual(graph.node, 'speak_farewell')
                self.assertIn('branch', graph.vars['fb_speech'])
                self.assertNotIn('?', graph.vars['fb_speech'])
                graph.finish()
                self.assertEqual([e['tool'] for e in bank.ledger['calls']], ['verify_identity']*limit+['close_call'])
                self.assertEqual(bank.ledger['reason'], 'failed')
                graph.user('identity_answer', answer='1980-02-02', answer_complete=True)
                self.assertEqual(len(bank.ledger['calls']), limit+1)

    def test_unreadable_fragment_cannot_invent_a_wrong_answer_or_exhaustion(self):
        graph, bank = self.graph()
        graph.user('person_yes')
        graph.user('identity_answer', answer='July', answer_complete=True)
        self.assertEqual(bank.ledger['calls'], [])
        self.assertEqual(graph.state['wrong_answers_left'], 3)
        self.assertIn('complete answer', graph.vars['fb_speech'])
        self.assertNotIn('verified', graph.vars['fb_speech'])

    def test_support_decline_takes_priority_over_an_incidental_plan_question(self):
        for decision in ({'support_decision': 'decline'}, {'support_no': True}):
            with self.subTest(decision=decision):
                graph, bank = self.graph()
                graph.user('person_yes')
                graph.user('identity_answer', answer='1980-02-02', answer_complete=True)
                graph.user('identity_answer', answer='9042', answer_complete=True)
                graph.user('hardship')
                self.assertEqual(graph.node, 'speak_support')
                graph.user('restructure', **decision)
                self.assertEqual(graph.node, 'speak_farewell')
                graph.finish()
                self.assertEqual(bank.ledger['reason'], 'hardship')
                self.assertEqual(bank.ledger['transfers'], [])
                self.assertEqual(bank.ledger['promises'], [])

    def test_support_decline_with_voluntary_payment_keeps_the_proposal(self):
        graph, bank = self.graph()
        graph.user('person_yes')
        graph.user('identity_answer', answer='1980-02-02', answer_complete=True)
        graph.user('identity_answer', answer='9042', answer_complete=True)
        graph.user('hardship')
        graph.user('payment', support_decision='decline', amount_kind='explicit', amount='401.37', day={'kind':'today'})
        self.assertEqual(graph.node, 'speak_confirm')
        graph.user('confirm', explicit_yes=True)
        graph.finish()
        self.assertEqual(len(bank.ledger['promises']), 1)
        self.assertEqual(bank.ledger['transfers'], [])
        self.assertEqual(bank.ledger['outcome'], 'promise')

    def test_question_alongside_payment_survives_extraction_and_clears_next_turn(self):
        graph, bank = self.graph()
        graph.user('person_yes')
        graph.user('identity_answer', answer='1980-02-02', answer_complete=True)
        graph.user('identity_answer', answer='9042', answer_complete=True)
        graph.vars['fb_question_kind'] = 'ai'
        graph.user('accept_offer')
        self.assertEqual(graph.node, 'speak_confirm')
        self.assertIn('digital assistant', graph.vars['fb_notice'])
        self.assertEqual(graph.vars['fb_question_kind'], 'none')
        self.assertEqual(bank.ledger['promises'], [])
        graph.user('confirm', explicit_yes=True)
        self.assertEqual(graph.vars['fb_notice'], '')
        graph.finish()
        self.assertEqual(len(bank.ledger['promises']), 1)

    def test_documented_http_error_without_post_hook_obeys_retry_limits(self):
        for attempts in (1, 2, 3):
            graph, bank = self.graph(retry_limit=attempts,
                faults=[{'tool':'create_promise','fail_first_n':attempts-1}])
            graph.user('person_yes')
            graph.user('identity_answer', answer='1980-02-02', answer_complete=True)
            graph.user('identity_answer', answer='9042', answer_complete=True)
            graph.user('accept_offer')
            self.assertEqual(bank.ledger['promises'], [])
            graph.user('confirm', explicit_yes=True)
            graph.finish()
            self.assertEqual(sum(c['tool']=='create_promise' for c in bank.ledger['calls']), attempts)
            self.assertEqual(len(bank.ledger['promises']), 1)
            self.assertEqual(bank.ledger['outcome'], 'promise')

    def test_uncertain_error_cannot_reuse_a_previous_known_error_status(self):
        def uncertain(tool, response):
            if tool=='create_promise' and response['status_code']==200:
                return {'status_code':504,'data':'unknown commit'}
            return response
        graph, bank = self.graph(faults=[{'tool':'create_promise','fail_first_n':1}], adapter=uncertain)
        graph.user('person_yes')
        graph.user('identity_answer', answer='1980-02-02', answer_complete=True)
        graph.user('identity_answer', answer='9042', answer_complete=True)
        graph.user('accept_offer')
        graph.user('confirm', explicit_yes=True)
        self.assertEqual(graph.node, 'speak_abort')
        self.assertEqual(sum(c['tool']=='create_promise' for c in bank.ledger['calls']), 2)
        self.assertEqual(graph.state['recorded_products'], [])
        self.assertIsNone(bank.ledger['outcome'])

    def test_pre_hook_erases_stale_status_and_strict_speech_is_fixed(self):
        graph, _ = self.graph()
        node = graph.workflow['nodes']['api_create_promise']
        values={'api_create_promise_response.status_code':500,
                'api_create_promise_response.status':'error','fb_request_body':'{}'}
        exec(node['pre_actions'][0]['pre_process_code'], {'vars':values,'config':{}})
        self.assertEqual(values['api_create_promise_response.status_code'], '')
        self.assertEqual(values['api_create_promise_response.status'], '')
        for stage in ('identity','progress','farewell'):
            self.assertEqual(graph.workflow['nodes']['speak_'+stage]['on_enter'],
                             {'type':'tts','text':'{{fb_speech}}'})
