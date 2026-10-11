import json
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tools.collect_smoke import customer_secrets, read_retry, match_runs, SmokeError
from tools.collect_smoke import collect
from tools.artifact_checks import canonical_digest
from tools.scenario_inputs import ROOT, load_case_inputs


class RecoveryTests(unittest.TestCase):
    def test_multi_case_recovery_matches_personas_not_response_order(self):
        started = {'cases': {'PUB-01': 'p1', 'PUB-10': 'p2'}}
        runs = [{'personaId': 'p2', 'callId': 'c2'}, {'personaId': 'p1', 'callId': 'c1'}]
        self.assertEqual(match_runs(started, runs), [('PUB-01', runs[1]), ('PUB-10', runs[0])])
        for invalid in (runs[:1], [runs[0], runs[0]], [runs[0], {'personaId':'p1'}]):
            with self.assertRaises(SmokeError):
                match_runs(started, invalid)

    def test_recovers_customer_from_nested_json_events_without_other_ids(self):
        call = {'id': 'call-id', 'messages': [{'message': json.dumps({'event': 'route',
                'variable_table': {'customer_id': {'value': 'synthetic-customer'}},
                'args': {'customer_id': 'synthetic-customer'}})}]}
        self.assertEqual(customer_secrets(call), {'synthetic-customer'})

    def test_first_collected_call_survives_a_later_download_failure_without_a_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'source', Path(temp) / 'recovered'
            source.mkdir()
            started = {'simulation_id': 'simulation', 'agent_id': 'agent', 'expected_version': 1,
                       'cases': {'PUB-01': 'persona-1', 'PUB-10': 'persona-2'}}
            publication = {'agent_id': 'agent', 'version': 1, 'workflow_sha256': 'hash'}
            for name, value in [('simulation-started.json', started), ('test-publication.json', publication)]:
                (source / name).write_text(json.dumps(value), encoding='utf-8')
            iteration = {'runs': [{'personaId': 'persona-' + str(i), 'callId': 'call-' + str(i),
                                  'outcome': 'pass'} for i in (1, 2)]}
            freya = SimpleNamespace(api=lambda method, path: {'iterations': [{'id': 'iteration'}]}
                if path.endswith('/iterations') else {'status': 'completed', 'agentId': 'agent'},
                get_iteration=lambda *_: iteration)
            call = {'id': 'call-1', 'endedAt': 'done', 'messages': [{'role': 'system',
                    'message': '{"customer_id":{"value":"private-customer-1"}}'}]}
            def download(_, call_id, **kwargs):
                if call_id == 'call-1':
                    return call
                saved = output / 'call-PUB-01.json'
                self.assertTrue(saved.is_file())
                self.assertNotIn('private-customer-1', saved.read_text())
                raise ConnectionResetError('Later call unavailable')
            with patch.dict(sys.modules, {'freya': freya}), patch('tools.collect_smoke.load_credentials', return_value=[]), \
                    patch('tools.collect_smoke.download_call', side_effect=download), \
                    patch('tools.score.http') as ledger, patch('tools.live_smoke.time.sleep'), \
                    redirect_stdout(io.StringIO()), self.assertRaises(ConnectionResetError):
                collect(source, Path(temp) / '.env', output)
            ledger.assert_not_called()
            self.assertFalse((output / 'result.json').exists())
            self.assertNotIn('private-customer-1', (output / 'trace.jsonl').read_text())

    def test_connection_read_is_bounded_and_can_recover(self):
        operation = Mock(side_effect=[ConnectionResetError(), {'status': 'completed'}])
        with patch('tools.live_smoke.time.sleep'):
            self.assertEqual(read_retry(operation, Mock()), {'status': 'completed'})
        self.assertEqual(operation.call_count, 2)
        operation = Mock(side_effect=ConnectionResetError())
        with patch('tools.live_smoke.time.sleep'), self.assertRaises(ConnectionResetError):
            read_retry(operation, Mock())
        self.assertEqual(operation.call_count, 3)

    def test_application_errors_are_not_blindly_retried(self):
        operation = Mock(side_effect=ValueError('bad response'))
        with self.assertRaises(ValueError):
            read_retry(operation, Mock())
        self.assertEqual(operation.call_count, 1)

    def test_custom_case_recovery_scores_the_snapshot_against_its_base_bank_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'source', Path(temp) / 'recovered'
            source.mkdir()
            case_id = 'DEV-A-READBACK-FLOOR'
            inputs = load_case_inputs(ROOT / 'tests/fixtures/live-scenarios.json', [case_id])
            case = inputs['cases'][0]
            started = {'simulation_id': 'simulation', 'agent_id': 'agent', 'expected_version': 1,
                       'case': case_id, 'cases': {case_id: 'persona'}, 'bank_cases': {case_id: 'PUB-01'},
                       'case_inputs_sha256': canonical_digest(inputs)}
            publication = {'agent_id': 'agent', 'workflow_id': 'workflow', 'version': 1, 'workflow_sha256': 'hash'}
            definition = {'workflow': {'nodes': {'opening': {'node_type': 'conversation',
                            'on_enter': {'type': 'tts', 'text': 'Hello.'}}}}}
            for name, value in [('simulation-started.json', started), ('case-inputs.json', inputs),
                                ('test-publication.json', publication), ('tested-definition.json', definition)]:
                (source / name).write_text(json.dumps(value), encoding='utf-8')
            call = {'entityVersionNumber': 1, 'endedAt': 'done', 'messages': [
                {'role': 'system', 'message': '{"event":"request","customer_id":"synthetic-customer-secret"}'},
                {'role': 'system', 'message': '{"event":"node_enter","node":"opening"}'},
                {'role': 'assistant', 'message': 'Hello.'}]}
            iteration = {'runs': [{'personaId': 'persona', 'callId': 'call', 'outcome': 'pass', 'agentVersionNumber': 1}]}
            freya = SimpleNamespace(api=lambda method, path: {'iterations': [{'id': 'iteration'}]}
                if path.endswith('/iterations') else {'status': 'completed', 'agentId': 'agent'},
                get_iteration=lambda *_: iteration)
            ledger = {**case['expected'], 'case': 'PUB-01', 'calls': [
                {'tool': tool, 'result': {'ok': True}} for tool in
                ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call']]}
            with patch.dict(sys.modules, {'freya': freya}), patch('tools.collect_smoke.load_credentials', return_value=[]), \
                    patch('tools.collect_smoke.download_call', return_value=call), \
                    patch('tools.score.http', return_value=ledger), patch('tools.live_smoke.time.sleep'), \
                    redirect_stdout(io.StringIO()):
                result = collect(source, Path(temp) / '.env', output)
            self.assertEqual(result['passed'], 1)
            self.assertEqual(result['rows'][0]['id'], case_id)
            self.assertEqual(result['rows'][0]['bank_case_id'], 'PUB-01')
            self.assertTrue(result['rows'][0]['ledger_pass'])
            self.assertTrue(result['speech_verified'])
            self.assertEqual(json.loads((output / 'case-inputs.json').read_text()), inputs)
            self.assertNotIn('synthetic-customer-secret', (output / ('call-' + case_id + '.json')).read_text())

    def test_a_call_that_never_started_is_recorded_without_losing_the_batch(self):
        # 11 October outage: one call had no messages at all, and the collector
        # used to abort the whole batch because it had no customer ID.
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'source', Path(temp) / 'recovered'
            source.mkdir()
            ids = ['DEV-A-READBACK-FLOOR', 'DEV-A-VAGUE-YES']
            inputs = load_case_inputs(ROOT / 'tests/fixtures/live-scenarios.json', ids)
            cases = {c['id']: c for c in inputs['cases']}
            started = {'simulation_id': 'simulation', 'agent_id': 'agent', 'expected_version': 1,
                       'case': ','.join(ids), 'cases': {ids[0]: 'persona-ok', ids[1]: 'persona-empty'},
                       'bank_cases': {i: 'PUB-01' for i in ids}, 'case_inputs_sha256': canonical_digest(inputs)}
            publication = {'agent_id': 'agent', 'workflow_id': 'workflow', 'version': 1, 'workflow_sha256': 'hash'}
            definition = {'workflow': {'nodes': {'opening': {'node_type': 'conversation',
                            'on_enter': {'type': 'tts', 'text': 'Hello.'}}}}}
            for name, value in [('simulation-started.json', started), ('case-inputs.json', inputs),
                                ('test-publication.json', publication), ('tested-definition.json', definition)]:
                (source / name).write_text(json.dumps(value), encoding='utf-8')
            calls = {'call-ok': {'entityVersionNumber': 1, 'endedAt': 'done', 'messages': [
                         {'role': 'system', 'message': '{"event":"request","customer_id":"synthetic-customer-secret"}'},
                         {'role': 'system', 'message': '{"event":"node_enter","node":"opening"}'},
                         {'role': 'assistant', 'message': 'Hello.'}]},
                     'call-empty': {'entityVersionNumber': 1, 'endedAt': 'done', 'messages': []}}
            iteration = {'runs': [
                {'personaId': 'persona-ok', 'callId': 'call-ok', 'outcome': 'pass', 'agentVersionNumber': 1},
                {'personaId': 'persona-empty', 'callId': 'call-empty', 'outcome': 'fail', 'agentVersionNumber': 1}]}
            freya = SimpleNamespace(api=lambda method, path: {'iterations': [{'id': 'iteration'}]}
                if path.endswith('/iterations') else {'status': 'completed', 'agentId': 'agent'},
                get_iteration=lambda *_: iteration)
            ledger = {**cases[ids[0]]['expected'], 'case': 'PUB-01', 'calls': [
                {'tool': tool, 'result': {'ok': True}} for tool in
                ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call']]}
            reads = []
            def bank(path):
                reads.append(path)
                return ledger
            with patch.dict(sys.modules, {'freya': freya}), patch('tools.collect_smoke.load_credentials', return_value=[]), \
                    patch('tools.collect_smoke.download_call', side_effect=lambda _freya, call_id, **_: calls[call_id]), \
                    patch('tools.score.http', side_effect=bank), patch('tools.live_smoke.time.sleep'), \
                    redirect_stdout(io.StringIO()):
                result = collect(source, Path(temp) / '.env', output)
            rows = {row['id']: row for row in result['rows']}
            self.assertEqual(result['total'], 2)
            self.assertTrue(rows[ids[0]]['pass'])
            self.assertFalse(rows[ids[1]]['pass'])
            self.assertTrue(rows[ids[1]]['never_started'])
            self.assertIsNone(rows[ids[1]]['ledger_pass'])
            self.assertEqual(len(reads), 1)

    def test_paused_simulation_keeps_finished_calls_and_records_unfinished_ones(self):
        # 11 October: the watchdog paused a batch while Freya was still retrying
        # 22 calls; those runs had no call, and collection lost every result.
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            source.mkdir()
            ids = ['DEV-A-READBACK-FLOOR', 'DEV-A-VAGUE-YES']
            inputs = load_case_inputs(ROOT / 'tests/fixtures/live-scenarios.json', ids)
            cases = {c['id']: c for c in inputs['cases']}
            started = {'simulation_id': 'simulation', 'agent_id': 'agent', 'expected_version': 1,
                       'case': ','.join(ids), 'cases': {ids[0]: 'persona-ok', ids[1]: 'persona-retrying'},
                       'bank_cases': {i: 'PUB-01' for i in ids}, 'case_inputs_sha256': canonical_digest(inputs)}
            publication = {'agent_id': 'agent', 'workflow_id': 'workflow', 'version': 1, 'workflow_sha256': 'hash'}
            definition = {'workflow': {'nodes': {'opening': {'node_type': 'conversation',
                            'on_enter': {'type': 'tts', 'text': 'Hello.'}}}}}
            for name, value in [('simulation-started.json', started), ('case-inputs.json', inputs),
                                ('test-publication.json', publication), ('tested-definition.json', definition)]:
                (source / name).write_text(json.dumps(value), encoding='utf-8')
            call = {'entityVersionNumber': 1, 'endedAt': 'done', 'messages': [
                {'role': 'system', 'message': '{"event":"request","customer_id":"synthetic-customer-secret"}'},
                {'role': 'system', 'message': '{"event":"node_enter","node":"opening"}'},
                {'role': 'assistant', 'message': 'Hello.'}]}
            iteration = {'runs': [
                {'personaId': 'persona-ok', 'callId': 'call-ok', 'outcome': 'pass', 'agentVersionNumber': 1},
                {'personaId': 'persona-retrying', 'outcome': 'retrying'}]}
            ledger = {**cases[ids[0]]['expected'], 'case': 'PUB-01', 'calls': [
                {'tool': tool, 'result': {'ok': True}} for tool in
                ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call']]}
            for status, should_collect in (('paused', True), ('completed', False)):
                with self.subTest(status=status):
                    freya = SimpleNamespace(api=lambda method, path, status=status: {'iterations': [{'id': 'iteration'}]}
                        if path.endswith('/iterations') else {'status': status, 'agentId': 'agent'},
                        get_iteration=lambda *_: iteration)
                    output = Path(temp) / ('recovered-' + status)
                    with patch.dict(sys.modules, {'freya': freya}), patch('tools.collect_smoke.load_credentials', return_value=[]), \
                            patch('tools.collect_smoke.download_call', return_value=call), \
                            patch('tools.score.http', return_value=ledger), patch('tools.live_smoke.time.sleep'), \
                            redirect_stdout(io.StringIO()):
                        if not should_collect:
                            with self.assertRaises(SmokeError):
                                collect(source, Path(temp) / '.env', output)
                            continue
                        result = collect(source, Path(temp) / '.env', output)
                    rows = {row['id']: row for row in result['rows']}
                    self.assertTrue(rows[ids[0]]['pass'])
                    self.assertFalse(rows[ids[1]]['pass'])
                    self.assertTrue(rows[ids[1]]['never_started'])
                    self.assertTrue(result['version_verified'])
