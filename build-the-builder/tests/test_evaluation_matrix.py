from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.artifact_checks import canonical_digest
from tools.evaluation_matrix import build_matrix


class EvaluationMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = {'id': 'PUB-01', 'brief': 'A', 'script': 'Synthetic case', 'expected': {
            'outcome': 'paid', 'reason': None, 'promises': [], 'notes': [], 'callbacks': [], 'transfers': []}}
        self.public = self.root / 'public.json'
        self.public.write_text(json.dumps([self.case]))

    def save(self, folder, name, value):
        (folder / name).write_text(json.dumps(value), encoding='utf-8')

    def alter(self, folder, name, update):
        value = json.loads((folder / name).read_text())
        update(value)
        self.save(folder, name, value)

    def batch(self, name, *, sha='a'*64, style=None, version=7, case=None, call_id=None, sim=None):
        folder = self.root / name
        folder.mkdir()
        frozen = deepcopy(case or self.case)
        frozen.setdefault('bank_case_id', 'PUB-01')
        ident = frozen['id']
        call_id = call_id or 'call-' + name
        sim = sim or 'sim-' + name
        inputs = {'cases': [frozen], 'evaluation_only': True}
        self.save(folder, 'case-inputs.json', inputs)
        publication = {'status': 'readback_verified', 'differences': [], 'workflow_id': 'workflow',
                       'agent_id': 'agent', 'version': version, 'workflow_sha256': sha}
        self.save(folder, 'test-publication.json', publication)
        row = {'id': ident, 'bank_case_id': 'PUB-01', 'call_id': call_id, 'call_ended': True,
               'pass': True, 'ledger_pass': True, 'official_outcome': 'pass',
               'official_observed_outcomes': ['pass'], 'evaluation_issues': []}
        result = {'status': 'completed', 'agent_id': 'agent', 'workflow_sha256': sha,
                  'expected_version': version, 'style': style, 'simulation_id': sim,
                  'iteration_id': 'iteration-' + name, 'total': 1, 'passed': 1,
                  'version_verified': True, 'case_inputs_sha256': canonical_digest(inputs),
                  'verdict_observation': {'stable': True, 'snapshots': 2, 'personas': {
                      'persona': {'outcomes': ['pass'], 'call_ids': [call_id], 'conflict': False}}}, 'rows': [row]}
        self.save(folder, 'result.json', result)
        self.save(folder, 'iteration.json', {'runs': [{'callId': call_id, 'personaId': 'persona',
            'outcome': 'pass', 'agentVersionNumber': version, 'agentId': 'agent'}]})
        self.save(folder, 'simulation.json', {'status': 'completed', 'agentId': 'agent'})
        self.save(folder, 'call-' + ident + '.json', {'id': call_id, 'endedAt': 'done',
            'entityVersionNumber': version, 'agentId': 'agent', 'messages': [{'message':'PRIVATE-TRANSCRIPT-CONTENT'}]})
        self.save(folder, 'ledger-' + ident + '.json', {**frozen['expected'], 'case': 'PUB-01',
            'calls': [{'tool': 'close_call', 'result': {'ok': True}}]})
        self.save(folder, 'speech-audit.json', {'workflow_sha256': sha, 'workflow_version': version,
            'status': 'passed', 'cases': {ident: {'status': 'passed', 'findings': [], 'utterances_checked': 3}}})
        self.save(folder, 'manual-review.json', {'workflow_sha256': sha, 'workflow_version': version,
            'simulation_id': sim, 'review_passed': True, 'findings': [], 'evaluation_issues': [],
            'case_evidence': [{'case': ident}]})
        return folder

    def matrix(self, folders):
        return build_matrix(folders, public_path=self.public)

    def test_three_fresh_trials_conjoin_across_styles_but_keep_style_counts(self):
        folders = [self.batch('one'), self.batch('two', style='corrects'), self.batch('three', style='pieces')]
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for folder in folders for p in folder.iterdir()}
        report = self.matrix(folders)
        case = report['workflows'][0]['cases'][0]
        self.assertTrue(case['three_pass_conjunction'])
        self.assertEqual(case['unique_calls'], 3)
        self.assertEqual(case['unique_simulations'], 3)
        self.assertEqual(set(case['styles']), {'default', 'corrects', 'pieces'})
        self.assertTrue(all(not item['three_pass_conjunction'] for item in case['styles'].values()))
        self.assertNotIn('submission_ready', json.dumps(report))
        self.assertNotIn('PRIVATE-TRANSCRIPT-CONTENT', json.dumps(report))
        self.assertTrue(all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path,digest in before.items()))

    def test_different_workflow_hashes_and_changed_scenario_scripts_never_pool_repeats(self):
        variant = {**deepcopy(self.case), 'id': 'DEV-TARGET', 'bank_case_id': 'PUB-01'}
        changed = {**deepcopy(variant), 'script': 'A genuinely different synthetic conversation'}
        folders = [self.batch('one', case=variant), self.batch('two', case=variant),
                   self.batch('three', case=changed), self.batch('four', case=variant, sha='b'*64)]
        report = self.matrix(folders)
        self.assertEqual(len(report['workflows']), 2)
        rows = [row for group in report['workflows'] for row in group['cases']]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['category'] == 'targeted' for row in rows))
        self.assertFalse(any(row['three_pass_conjunction'] for row in rows))
        self.assertEqual(report['workflows'][0]['public_cases_missing'], ['PUB-01'])

    def test_one_failed_or_inconclusive_trial_cannot_be_discarded_around_three_passes(self):
        for official in ('fail', 'inconclusive', None):
            with self.subTest(official=official):
                folders = [self.batch(str(official) + str(i)) for i in range(4)]
                self.alter(folders[-1], 'result.json', lambda r:r['rows'][0].update(official_outcome=official, **{'pass':False}))
                case = self.matrix(folders)['workflows'][0]['cases'][0]
                self.assertEqual(case['qualifying_passes'], 3)
                self.assertFalse(case['three_pass_conjunction'])
                self.assertEqual(case['status'], 'blocked')

    def test_repeated_call_and_simulation_ids_do_not_create_fresh_repetitions(self):
        folders = [self.batch('one', call_id='reused'), self.batch('two', call_id='reused'), self.batch('three')]
        case = self.matrix(folders)['workflows'][0]['cases'][0]
        self.assertFalse(case['three_pass_conjunction'])
        self.assertEqual(case['blocker_counts']['reused_or_missing_call_id'], 2)
        folders = [self.batch('four', sim='same-simulation'), self.batch('five', sim='same-simulation')]
        case = self.matrix(folders)['workflows'][0]['cases'][0]
        self.assertEqual(case['qualifying_passes'], 0)
        self.assertEqual(case['blocker_counts']['reused_simulation_for_case'], 2)

    def test_missing_review_wrong_actual_version_and_manual_findings_block(self):
        folders = [self.batch('one'), self.batch('two'), self.batch('three')]
        (folders[0] / 'manual-review.json').unlink()
        self.alter(folders[1], 'call-PUB-01.json', lambda r:r.update(entityVersionNumber=8))
        self.alter(folders[2], 'manual-review.json', lambda r:r.update(findings=[{'case':'PUB-01','observed':'Policy violation'}]))
        case = self.matrix(folders)['workflows'][0]['cases'][0]
        self.assertFalse(case['three_pass_conjunction'])
        self.assertEqual(case['qualifying_passes'], 0)
        self.assertIn('manual_missing', case['blocker_counts'])
        self.assertIn('actual_version_unverified_or_mismatched', case['blocker_counts'])
        self.assertIn('manual_findings', case['blocker_counts'])

    def test_nested_manual_review_cases_are_supported_and_stale_review_hashes_block(self):
        folder = self.batch('one')
        self.alter(folder, 'manual-review.json', lambda r:r.update(case_evidence=[],
            cases={'PUB-01': {'status':'pass_no_observed_violation', 'findings':[]}}))
        self.assertEqual(self.matrix([folder])['workflows'][0]['cases'][0]['qualifying_passes'], 1)
        self.alter(folder, 'manual-review.json', lambda r:r.update(sources_sha256={'call-PUB-01.json':'wrong'}))
        self.assertIn('manual_stale_sources', self.matrix([folder])['workflows'][0]['cases'][0]['blocker_counts'])

    def test_ledger_is_rechecked_and_matching_speech_is_required(self):
        first, second = self.batch('one'), self.batch('two')
        self.alter(first, 'ledger-PUB-01.json', lambda r:r.update(outcome='no_promise'))
        self.alter(second, 'speech-audit.json', lambda r:r.update(workflow_sha256='b'*64))
        case = self.matrix([first, second])['workflows'][0]['cases'][0]
        self.assertEqual(case['qualifying_passes'], 0)
        self.assertIn('ledger_failed_or_unverified', case['blocker_counts'])
        self.assertIn('speech_unverified', case['blocker_counts'])

    def test_conflicting_stable_observation_and_changed_frozen_inputs_block(self):
        first, second = self.batch('one'), self.batch('two')
        self.alter(first, 'result.json', lambda r:r['verdict_observation']['personas']['persona'].update(outcomes=['fail','pass'], conflict=True))
        self.alter(second, 'case-inputs.json', lambda r:r['cases'][0].update(script='changed'))
        case = self.matrix([first, second])['workflows'][0]['cases'][0]
        reasons = {reason for row in self.matrix([first, second])['workflows'][0]['cases'] for reason in row['blocker_counts']}
        self.assertIn('conflicting_or_missing_persona_observation', reasons)
        self.assertIn('frozen_inputs_changed', reasons)

    def test_malformed_sources_are_reported_and_duplicate_paths_are_not_duplicate_trials(self):
        folder = self.batch('one')
        bad = self.root / 'malformed'
        bad.mkdir()
        (bad/'result.json').write_text('not JSON')
        report = self.matrix([folder, folder, bad])
        self.assertEqual(len(report['rejected_sources']), 1)
        self.assertEqual(report['workflows'][0]['cases'][0]['trials'], 1)
        self.assertEqual(report['workflows'][0]['cases'][0]['qualifying_passes'], 1)

    def test_legacy_public_without_frozen_inputs_is_labeled_but_cannot_qualify(self):
        folder = self.batch('one')
        (folder / 'case-inputs.json').unlink()
        self.alter(folder, 'result.json', lambda r:r.pop('case_inputs_sha256'))
        case = self.matrix([folder])['workflows'][0]['cases'][0]
        self.assertEqual(case['category'], 'public')
        self.assertEqual(case['qualifying_passes'], 0)
        self.assertIn('missing_or_ambiguous_frozen_inputs', case['blocker_counts'])

    def test_result_style_overrides_frozen_persona_style_and_wrong_review_version_blocks(self):
        self.case['style'] = 'pieces'
        self.public.write_text(json.dumps([self.case]))
        folder = self.batch('one', style='corrects')
        case = self.matrix([folder])['workflows'][0]['cases'][0]
        self.assertEqual(set(case['styles']), {'corrects'})
        self.alter(folder, 'manual-review.json', lambda r:r.update(workflow_version=6))
        self.assertIn('manual_mismatched', self.matrix([folder])['workflows'][0]['cases'][0]['blocker_counts'])
