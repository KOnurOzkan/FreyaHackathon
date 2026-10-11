"""Exercise the opening wire contract through actual emitted runtime code.

These tests inject extraction outputs, not speech: live repetitions must still
verify the model's classification of names, relatives, and ambiguous responses.
"""
import json
import re

from builder.contracts import Contracts
from builder.inputs import read_json
from builder.workflow import compile_workflow
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import LocalGraph, fake_plan


class PersonExtractionTests(OfflineCase):
    compiled = None

    def setUp(self):
        super().setUp()
        if type(self).compiled is None:
            contracts = Contracts(read_json(ROOT / 'tools/openapi.json'))
            type(self).compiled = compile_workflow(
                fake_extraction(self.documents[:1], self.a_raw),
                fake_plan(self.documents[:1], False, contracts), self.documents[:1], contracts)

    def graph(self):
        case, _, bank = self.setup_case(1, verified=False)
        workflow, report = self.compiled
        return case, LocalGraph(workflow, bank, case['variables'], report['runtime_payload']['bindings']), bank

    def test_only_person_nodes_expose_the_finite_person_field(self):
        workflow, _ = self.compiled
        for name, node in workflow['nodes'].items():
            schemas = {s['name']: s for s in node.get('extraction_schema', [])}
            if name not in {'opening', 'speak_person'}:
                self.assertNotIn('fb_person_reply', schemas, name)
                continue
            self.assertNotIn('fb_event', schemas)
            schema = schemas['fb_person_reply']
            self.assertEqual(set(schema['enum_values']), {'person_yes', 'person_unclear', 'other_person',
                'wrong_number', 'deceased', 'human', 'refuse_talk', 'question', 'unparsed'})
            for label in schema['enum_values']:
                self.assertIsNotNone(re.fullmatch(schema['validation_pattern'], label))
            for bad in ['identity_answer', 'confirm', 'continue', '{"action":"person_yes"}', 'yes', 'person_yes\n']:
                self.assertIsNone(re.fullmatch(schema['validation_pattern'], bad))

    def test_valid_person_label_ignores_wrong_general_event_and_does_not_verify(self):
        _, graph, bank = self.graph()
        graph.vars.update(fb_person_reply='person_yes',
            fb_event=json.dumps(dict(action='identity_answer', answer='An Arbitrary Name', answer_complete=True)))
        graph.advance('user_turn')
        self.assertEqual(graph.node, 'speak_identity')
        self.assertEqual(bank.ledger['calls'], [])
        self.assertEqual(json.loads(graph.vars['fb_history']), [dict(action='person_yes')])
        self.assertEqual(graph.vars['fb_person_reply'], 'unparsed')
        self.assertEqual(graph.vars['fb_person_callback'], '{}')

    def test_missing_person_field_cannot_fall_back_to_general_confirmation(self):
        _, graph, bank = self.graph()
        graph.vars.update(fb_event='{"action":"person_yes"}', fb_public='{"phase":"identity"}')
        graph.advance('user_turn')
        self.assertEqual(graph.node, 'speak_person')
        self.assertEqual(bank.ledger['calls'], [])
        graph.advance('user_turn')
        self.assertEqual(graph.node, 'speak_farewell')
        graph.finish()
        self.assertEqual([c['tool'] for c in bank.ledger['calls']], ['close_call'])
        self.assertEqual(bank.ledger['reason'], 'unclear')

    def test_invalid_labels_cannot_inject_security_or_payment_actions(self):
        for value in ['identity_answer', 'confirm', 'finalize', '{"action":"person_yes"}', True, None, [], {}]:
            with self.subTest(value=value):
                _, graph, bank = self.graph()
                graph.vars.update(fb_person_reply=value, fb_event='{"action":"person_yes"}')
                graph.advance('user_turn')
                self.assertEqual(graph.node, 'speak_person')
                self.assertEqual(bank.ledger['calls'], [])

    def test_person_label_is_ignored_after_the_person_phase(self):
        case, graph, bank = self.graph()
        graph.user('person_yes')
        graph.vars['fb_person_reply'] = 'wrong_number'
        graph.user('identity_answer', answer=case['identity']['date_of_birth'], answer_complete=True)
        self.assertEqual(graph.node, 'speak_identity')
        self.assertEqual([c['tool'] for c in bank.ledger['calls']], ['verify_identity'])
        self.assertEqual(graph.vars['fb_person_reply'], 'unparsed')
        graph.vars['fb_person_reply'] = 'person_yes'
        graph.vars['fb_event'] = '{"action":"unparsed"}'
        graph.advance('user_turn')
        self.assertEqual(graph.node, 'speak_identity')
        self.assertEqual(len(bank.ledger['calls']), 1)

    def test_relative_never_confirms_person_even_with_general_yes(self):
        _, graph, bank = self.graph()
        graph.vars.update(fb_person_reply='other_person', fb_event='{"action":"person_yes"}')
        graph.advance('user_turn')
        self.assertEqual(graph.node, 'speak_callback')
        self.assertEqual(bank.ledger['calls'], [])
        self.assertNotIn('past due', graph.vars['fb_speech'].lower())

    def test_relative_callback_details_are_preserved_but_cannot_change_action(self):
        _, graph, bank = self.graph()
        graph.vars.update(fb_person_reply='other_person', fb_person_callback=json.dumps({
            'action': 'person_yes', 'answer': 'ignored', 'day': {'kind': 'tomorrow'}, 'time': '11:00'}))
        graph.advance('user_turn')
        self.assertEqual(graph.node, 'speak_farewell')
        graph.finish()
        self.assertEqual([c['tool'] for c in bank.ledger['calls']], ['schedule_callback', 'close_call'])
        self.assertEqual(json.loads(graph.vars['fb_history'])[0]['action'], 'other_person')

    def test_callback_fields_cannot_schedule_for_a_confirmed_speaker(self):
        _, graph, bank = self.graph()
        graph.vars.update(fb_person_reply='person_yes', fb_person_callback='{"day":{"kind":"tomorrow"},"time":"11:00"}')
        graph.advance('user_turn')
        self.assertEqual(graph.node, 'speak_identity')
        self.assertEqual(bank.ledger['calls'], [])

    def test_terminal_reports_and_human_request_keep_their_routes(self):
        for label, node in [('wrong_number', 'speak_farewell'), ('deceased', 'speak_farewell'),
                            ('human', 'speak_human_help'), ('refuse_talk', 'speak_person')]:
            with self.subTest(label=label):
                _, graph, bank = self.graph()
                graph.vars.update(fb_person_reply=label, fb_event='{"action":"person_yes"}')
                graph.advance('user_turn')
                self.assertEqual(graph.node, node)
                self.assertEqual(bank.ledger['calls'], [])

    def test_simultaneous_question_preserves_confirmation_without_disclosing_account(self):
        _, graph, bank = self.graph()
        graph.vars.update(fb_person_reply='person_yes', fb_question_kind='ai')
        graph.advance('user_turn')
        self.assertEqual(graph.node, 'speak_identity')
        self.assertIn("digital assistant", graph.vars['fb_speech'])
        self.assertEqual(bank.ledger['calls'], [])
