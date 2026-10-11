import json
import unittest
from tools.speech_audit import audit_call


class SpeechAuditTests(unittest.TestCase):
    def audit(self, actual, expected='Can you pay 817.23 on 12 April 2027?'):
        workflow={'nodes':{'offer':{'node_type':'conversation','on_enter':{'type':'tts','text':'{{fb_speech}}'}}}}
        messages=[{'role':'system','message':json.dumps({'event':'route_evaluated','variable_table':{'fb_speech':expected}})},
                  {'role':'system','message':json.dumps({'event':'node_enter','node':'offer'})}]
        messages += [{'role':'assistant','message':part} for part in actual]
        messages.append({'role':'user','message':'Yes'})
        return audit_call(workflow, {'messages':messages})

    def test_wrong_spoken_date_fails_even_if_bank_and_judge_pass(self):
        result=self.audit(['Can you pay 817.23 on 13 April 2027?'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(result['findings']),1)

    def test_false_success_claim_cannot_replace_a_readback(self):
        self.assertEqual(self.audit(['I recorded your promise.'], 'Do you confirm?')['status'],'failed')

    def test_split_utterance_whitespace_is_normalized_without_changing_words(self):
        self.assertEqual(self.audit(['Can you pay 817.23', '\non 12 April 2027?'])['status'],'passed')

    def test_missing_delivery_is_not_a_pass(self):
        self.assertEqual(self.audit([])['status'],'failed')
