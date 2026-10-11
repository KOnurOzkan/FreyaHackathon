from pathlib import Path
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.live_smoke import load_credentials, missing_values, redact, reject_embedded_secrets, SmokeError
from tools import live_smoke


class LiveSmokeSafetyTests(unittest.TestCase):
    def test_case_selection_preserves_order_and_rejects_empty_duplicate_or_unknown_ids(self):
        cases = [{"id": "PUB-01"}, {"id": "PUB-10"}]
        self.assertEqual(live_smoke.select_cases(cases, "PUB-10,PUB-01"), list(reversed(cases)))
        for invalid in ("", "PUB-01,", "PUB-01,PUB-01", "PUB-1", "PUB-99"):
            with self.subTest(invalid=invalid), self.assertRaises(SmokeError):
                live_smoke.select_cases(cases, invalid)

    def test_secret_values_are_removed_from_nested_errors_and_transcript_strings(self):
        value = {"customer_id": "synthetic", "message": 'body={"id":"synthetic"}; key=fak_test',
                 "nested": [{"Authorization": "Bearer fak_test", "apiKey": "different-secret"}]}
        safe = redact(value, ["synthetic", "fak_test"])
        self.assertNotIn("synthetic", str(safe))
        self.assertNotIn("fak_test", str(safe))
        self.assertNotIn("different-secret", str(safe))

    def test_saved_workflow_additions_are_allowed_but_changed_actions_are_not(self):
        expected = {"nodes": {"action": {"code": "guard()", "tools": []}}, "order": [1, 2]}
        self.assertEqual(missing_values(expected, {"nodes": {"action": {"code": "guard()", "default": True}}, "order": [1, 2]}), [])
        changed = {"nodes": {"action": {"code": "changed()"}}, "order": [2, 1]}
        self.assertEqual(len(missing_values(expected, changed)), 3)

    def test_only_explicit_freya_credentials_are_loaded_without_evaluating_shell(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / ".env"
            path.write_text('FREYA_API_KEY="fak_test"\nFREYA_BANK_TEAM_KEY=abcdefghijklmnop\nOPENAI_API_KEY=unused\nUNRELATED=$(anything)\n', encoding="utf-8")
            load_credentials(path)
            self.assertEqual(os.environ["FREYA_API_KEY"], "fak_test")
            self.assertNotIn("OPENAI_API_KEY", os.environ)
            self.assertNotIn("UNRELATED", os.environ)

    def test_invalid_local_credentials_fail_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text('FREYA_API_KEY=private-invalid-key\n', encoding="utf-8")
            with self.assertRaises(SmokeError) as caught:
                load_credentials(path)
            self.assertNotIn("private-invalid-key", str(caught.exception))

    def test_lost_simulation_creation_response_cannot_start_a_second_run(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            output = root / "live"
            output.mkdir()
            workflow = root / "workflow.json"
            workflow.write_text('{"nodes":{}}', encoding="utf-8")
            digest = hashlib.sha256(workflow.read_bytes()).hexdigest()
            (root / "manifest.json").write_text(json.dumps({"workflow_sha256": digest}), encoding="utf-8")
            (output / "publication.json").write_text(json.dumps({"status": "readback_verified",
                "workflow_sha256": digest, "agent_id": "agent", "workflow_id": "workflow", "version": 1}), encoding="utf-8")
            (root / "public-test-set.json").write_text('[{"id":"PUB-11","identity":{}}]', encoding="utf-8")
            env = root / ".env"
            env.write_text('FREYA_API_KEY=fak_synthetic\nFREYA_BANK_TEAM_KEY=synthetic-team-key-12345\n', encoding="utf-8")
            attempted = []
            def api(method, path, body=None, **params):
                if path == "/api/v2/workflow/workflow":
                    return {"workflow": {"workflow": {"nodes": {}, "agent_config": {"llmConfig": {
                        "provider": "experimental-5", "model": "experimental-5"}}}}}
                if path == "/api/v2/versions/workflow/workflow":
                    return {"versions": [{"versionNumber": 1, "isPublished": True}]}
                attempted.append(path)
                raise TimeoutError("Lost POST response")
            freya = SimpleNamespace(api=api, AGENT_LLM={"provider": "experimental-5", "model": "experimental-5"},
                SIM_LLM={"provider": "fake", "model": "fake"},
                get_iteration=lambda *_: {'runs': []},
                workspace_id=lambda: "workspace",
                create_personas=lambda specs: {"personas": [{"id": "persona"}]})
            cases = SimpleNamespace(persona_for=lambda *args: {},
                http=lambda *args: {"variables": {"customer_id": "synthetic-customer-secret"}})
            def run_once(agent, selected, ids):
                variables = cases.http("/v1/practice/sessions", {"case": selected[0]["id"]})["variables"]
                return freya.run_simulation(agent, "synthetic", {"persona": variables})
            cases.run_once = run_once
            argv = ["live_smoke.py", "simulate", "--workflow", str(workflow), "--env", str(env), "--out", str(output)]
            with patch.object(live_smoke, "ROOT", root), patch.object(sys, "argv", argv), \
                    patch.dict(sys.modules, {"freya": freya, "run_cases": cases}), \
                    patch('tools.practice_inputs.check_practice_manifest'), \
                    patch('tools.live_smoke.load_case_inputs', return_value={'cases': [{'id': 'PUB-11', 'bank_case_id': 'PUB-11', 'identity': {}}]}), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(live_smoke.main(), 2)
                self.assertTrue((output / "simulation-attempted.json").is_file())
                self.assertFalse((output / "simulation-started.json").exists())
                self.assertEqual(live_smoke.main(), 2)
            self.assertEqual(attempted, ["/api/v2/simulations"])
            self.assertNotIn("synthetic-customer-secret", (output / "trace.jsonl").read_text())

    def test_oversized_code_cannot_reach_platform_publication(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            source = 'private-source-marker' * 7000
            definition = {'nodes': {'controller': {'pre_actions': [{'function': {
                'handler':'__function__:code_exec', 'parameters': {'properties': {
                    'code': {'default': source}}}}}]}}}
            workflow = root / 'workflow.json'
            workflow.write_text(json.dumps(definition), encoding='utf-8')
            digest = hashlib.sha256(workflow.read_bytes()).hexdigest()
            (root / 'manifest.json').write_text(json.dumps({'workflow_sha256':digest}))
            env = root / '.env'
            env.write_text('FREYA_API_KEY=fak_synthetic\nFREYA_BANK_TEAM_KEY=synthetic-team-key-12345\n')
            api = Mock(side_effect=AssertionError('Publication must stop before network access'))
            validate = Mock(side_effect=AssertionError('Server validation must not occur'))
            freya = SimpleNamespace(api=api, validate_workflow=validate,
                AGENT_LLM={'provider':'experimental-5','model':'experimental-5'})
            argv = ['live_smoke.py','publish','--workflow',str(workflow),'--env',str(env),
                    '--out',str(root / 'live')]
            output = io.StringIO()
            with patch.object(sys,'argv',argv), patch.dict(sys.modules, {'freya':freya,'run_cases':SimpleNamespace()}), \
                    redirect_stdout(output):
                self.assertEqual(live_smoke.main(),2)
            api.assert_not_called()
            validate.assert_not_called()
            self.assertIn('conservative sandbox budget',output.getvalue())
            self.assertNotIn('private-source-marker',output.getvalue())

    def test_schema_fields_are_preserved_but_actual_secret_values_block_artifacts(self):
        schema = {"customer_id": {"type": "string", "default": "{{fb_arg_customer_id}}"},
                  "apiKey": {"type": "string"}}
        original = json.dumps(schema)
        reject_embedded_secrets(schema, ["actual-private-value"])
        self.assertEqual(json.dumps(schema), original)
        with self.assertRaises(SmokeError) as caught:
            reject_embedded_secrets({"code": "header = 'actual-private-value'"}, ["actual-private-value"])
        self.assertNotIn("actual-private-value", str(caught.exception))

    def test_completed_batch_observes_delayed_verdict_once_and_preserves_independent_evidence(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            output = root / 'live'
            output.mkdir()
            definition = {'nodes': {'opening': {'node_type': 'conversation',
                'on_enter': {'type': 'tts', 'text': 'Hello.'}}}}
            workflow = root / 'workflow.json'
            workflow.write_text(json.dumps(definition), encoding='utf-8')
            digest = hashlib.sha256(workflow.read_bytes()).hexdigest()
            (root / 'manifest.json').write_text(json.dumps({'workflow_sha256': digest}))
            (output / 'publication.json').write_text(json.dumps({'status': 'readback_verified',
                'workflow_sha256': digest, 'agent_id': 'agent', 'workflow_id': 'workflow', 'version': 1}))
            (root / 'public-test-set.json').write_text('[{"id":"PUB-11","identity":{}}]')
            env = root / '.env'
            env.write_text('FREYA_API_KEY=fak_synthetic\nFREYA_BANK_TEAM_KEY=synthetic-team-key-12345\n')
            llm = {'provider': 'experimental-5', 'model': 'experimental-5'}
            api_calls = []
            simulation_bodies = []

            def api(method, path, body=None, **params):
                api_calls.append((method, path))
                if path == '/api/v2/workflow/workflow':
                    return {'workflow': {'workflow': {**definition, 'agent_config': {'llmConfig': llm}}}}
                if path == '/api/v2/versions/workflow/workflow':
                    return {'versions': [{'versionNumber': 1, 'isPublished': True}]}
                if path == '/api/v2/simulations':
                    simulation_bodies.append(body)
                    return {'id': 'simulation'}
                if path == '/api/v2/simulations/simulation':
                    return {'status': 'completed', 'agentId': 'agent'}
                if path.endswith('/iterations'):
                    return {'iterations': [{'id': 'iteration'}]}
                raise AssertionError('Unexpected API operation')

            outcomes = iter([None, 'pass', 'pass'])
            iteration = Mock(side_effect=lambda *_: {'runs': [{'personaId': 'persona', 'callId': 'call',
                'outcome': next(outcomes), 'agentVersionNumber': 1}]})
            freya = SimpleNamespace(api=api, AGENT_LLM=llm, SIM_LLM=llm, get_iteration=iteration,
                workspace_id=lambda: 'workspace', create_personas=lambda specs: {'personas': [{'id': 'persona'}]})
            persona = Mock(side_effect=lambda case, ident, style: {'name': 'test', 'prompt': style})
            cases = SimpleNamespace(STYLES={'corrects': 'correct speech'}, persona_for=persona,
                score_ledger=lambda *_: {'failed': []}, http=Mock(side_effect=lambda path, body=None:
                    {'variables': {'customer_id': 'synthetic-customer-secret'}} if body is not None
                    else {'case': 'PUB-11', 'calls': []}))

            def run_once(agent, selected, ids):
                variables = cases.http('/v1/practice/sessions', {'case': selected[0]['id']})['variables']
                sim, it, status = freya.run_simulation(agent, 'synthetic', {'persona': variables})
                freya.get_iteration(sim, it)
                freya.get_call('call')
                self.assertTrue((output / 'call-DEV-CUSTOM.json').is_file())
                self.assertFalse((output / 'result.json').exists())
                return ([{'id': selected[0]['id'], 'customer_id': variables['customer_id'], 'call_id': 'call',
                          'pass': False, 'failed': ['stale initial verdict']}], sim, it, status, 12, 0)

            cases.run_once = run_once
            original_http = cases.http
            call = {'endedAt': 'done', 'entityVersionNumber': 1, 'messages': [
                {'role': 'system', 'message': '{"event":"node_enter","node":"opening"}'},
                {'role': 'assistant', 'message': 'Hello.'}]}
            argv = ['live_smoke.py', 'simulate', '--workflow', str(workflow), '--env', str(env),
                    '--out', str(output), '--style', 'corrects', '--cases', str(root / 'scenarios.json')]
            with patch.object(live_smoke, 'ROOT', root), patch.object(sys, 'argv', argv), \
                    patch.dict(sys.modules, {'freya': freya, 'run_cases': cases}), \
                    patch('tools.practice_inputs.check_practice_manifest'), \
                    patch('tools.live_smoke.load_case_inputs', return_value={'cases': [{'id': 'DEV-CUSTOM', 'bank_case_id': 'PUB-11', 'identity': {}, 'variable_overrides': {'now': '2026-10-16T11:00'}}]}), \
                    patch('tools.call_download.get_call', return_value=call), \
                    patch('tools.live_smoke.time.sleep'), redirect_stdout(io.StringIO()):
                self.assertEqual(live_smoke.main(), 0)
                self.assertIs(freya.api, api)
                self.assertIs(freya.get_iteration, iteration)
                self.assertIs(cases.http, original_http)
                self.assertFalse(hasattr(freya, 'run_simulation'))
                self.assertFalse(hasattr(freya, 'get_call'))
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(result['passed'], 1)
            row = result['rows'][0]
            self.assertTrue(row['call_ended'])
            self.assertTrue(row['ledger_pass'])
            self.assertEqual(row['official_outcome'], 'pass')
            self.assertEqual(row['id'], 'DEV-CUSTOM')
            self.assertEqual(row['bank_case_id'], 'PUB-11')
            self.assertEqual(result['style'], 'corrects')
            self.assertEqual(iteration.call_count, 3)
            self.assertEqual(api_calls.count(('POST', '/api/v2/simulations')), 1)
            self.assertEqual(original_http.call_args_list[0].args, ('/v1/practice/sessions', {'case': 'PUB-11'}))
            self.assertEqual(simulation_bodies[0]['customerVariablesMap']['persona']['now'], '2026-10-16T11:00')
            self.assertEqual(persona.call_args.args[-1], 'corrects')
            self.assertEqual(json.loads((output / 'speech-audit.json').read_text())['status'], 'passed')
            self.assertNotIn('synthetic-customer-secret', (output / 'result.json').read_text())
            snapshot = json.loads((output / 'case-inputs.json').read_text())
            self.assertEqual(snapshot['cases'][0]['id'], 'DEV-CUSTOM')
