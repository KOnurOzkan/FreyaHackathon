import json
from pathlib import Path
import tempfile
import unittest

from tools.call_artifacts import customer_secrets, persist_call_snapshot
from tools.live_smoke import redact


class CallArtifactTests(unittest.TestCase):
    def test_discovers_nested_and_wrapped_customer_secrets_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            private = ['workspace-secret']
            call = {'id': 'call-1', 'endedAt': 'done', 'customerId': {'value': 'customer-one'},
                    'messages': [{'message': json.dumps({'customer_id': 'customer-two'})},
                                 {'message': 'customer-one customer-two workspace-secret'}]}
            self.assertEqual(customer_secrets(call), {'customer-one', 'customer-two'})
            result = persist_call_snapshot(temp, 'call-1', call, private, redact, case_id='DEV-A')
            saved = Path(result['path']).read_text(encoding='utf-8')
            for secret in ('customer-one', 'customer-two', 'workspace-secret'):
                self.assertNotIn(secret, saved)
            self.assertTrue(result['created'])
            self.assertTrue(result['call_ended'])
            self.assertEqual(Path(result['path']).name, 'call-DEV-A.json')

    def test_provisional_evidence_is_preserved_when_mapping_or_completion_arrives(self):
        with tempfile.TemporaryDirectory() as temp:
            partial = {'id': 'call-1', 'messages': []}
            first = persist_call_snapshot(temp, 'call-1', partial, [], redact)
            finished = {**partial, 'endedAt': 'done', 'messages': [{'message': 'Goodbye.'}]}
            second = persist_call_snapshot(temp, 'call-1', finished, [], redact)
            final = persist_call_snapshot(temp, 'call-1', finished, [], redact, case_id='PUB-01')
            self.assertEqual(len({first['path'], second['path'], final['path']}), 3)
            self.assertEqual(json.loads(Path(first['path']).read_text()), partial)
            self.assertEqual(json.loads(Path(second['path']).read_text()), finished)
            self.assertEqual(json.loads(Path(final['path']).read_text()), finished)
            again = persist_call_snapshot(temp, 'call-1', finished, [], redact, case_id='PUB-01')
            self.assertFalse(again['created'])

    def test_final_call_cannot_be_silently_replaced_with_different_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            call = {'id': 'call-1', 'endedAt': 'done', 'messages': []}
            result = persist_call_snapshot(temp, 'call-1', call, [], redact, case_id='PUB-01')
            with self.assertRaises(ValueError):
                persist_call_snapshot(temp, 'call-1', {**call, 'messages': ['changed']}, [], redact, case_id='PUB-01')
            self.assertEqual(json.loads(Path(result['path']).read_text()), call)

    def test_invalid_identifiers_or_mismatched_call_fail_before_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            for call_id, case_id, call in [('../call', None, {}), ('call', '../case', {}),
                                           ('call', None, {'id': 'other-call'})]:
                with self.subTest(call_id=call_id, case_id=case_id), self.assertRaises(ValueError):
                    persist_call_snapshot(temp, call_id, call, [], redact, case_id=case_id)
            self.assertEqual(list(Path(temp).iterdir()), [])
