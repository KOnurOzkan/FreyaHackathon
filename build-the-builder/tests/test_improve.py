import hashlib
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.improve import evaluate, feedback, check_result, write


class Clock:
    value = 0
    def __call__(self):
        return self.value


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compiled = self.root / "compiled"
        self.compiled.mkdir()
        write(self.compiled / "workflow.json", {"nodes": {"speak_confirm": {
            "node_type": "conversation", "on_enter": {"type": "tts", "text": "{{fb_speech}}"}}}})
        self.digest = hashlib.sha256((self.compiled / "workflow.json").read_bytes()).hexdigest()
        write(self.compiled / "manifest.json", {"workflow_sha256": self.digest})
        self.commands = []
        self.clock = Clock()
        self.fail_stage = None
        self.active_number = 0
        self.uncertain = False
        self.review_current = False
        self.speech_mismatch = False
        practice = patch('tools.improve.check_practice_manifest', return_value={'brief': 'A'})
        self.practice_check = practice.start()
        self.addCleanup(practice.stop)

    def result(self, cases, number, passed=True):
        return {"status": "completed", "version_verified": True, "workflow_sha256": self.digest,
                "expected_version": 2, "simulation_id": "sim-" + str(number), "total": len(cases),
                "passed": len(cases) if passed else 0, "rows": [
                    {"id": case, "call_id": str(number) + "-" + case, "pass": passed,
                     "ledger_pass": passed, "official_outcome": "pass" if passed else "fail", "call_ended": True,
                     "failed": [] if passed else ["A strict invariant failed"]} for case in cases]}

    def fake_process(self, args, seconds, log):
        args = list(map(str, args))
        self.commands.append(args)
        self.clock.value += 10
        name = Path(log).stem
        if "publish" in args:
            out = Path(args[args.index("--out")+1])
            out.mkdir()
            write(out / "publication.json", {"status": "readback_verified"})
        if "simulate" in args:
            out = Path(args[args.index("--out")+1])
            self.active_number += 1
            write(out / "simulation-started.json", {"simulation_id": "sim-" + str(self.active_number)})
            if self.uncertain:
                raise TimeoutError("Synthetic monitor error")
            cases = args[args.index("--case")+1].split(",")
            write(out / "result.json", self.result(cases, self.active_number, name != self.fail_stage))
            for case in cases:
                write(out / ('call-'+case+'.json'), {'messages': [
                    {'role':'system','message':'{"event":"route_evaluated","variable_table":{"fb_speech":"Do you confirm?"}}'},
                    {'role':'system','message':'{"event":"node_enter","node":"speak_confirm"}'},
                    {'role':'assistant','message':'I recorded it.' if self.speech_mismatch else 'Do you confirm?'}]})
            if self.review_current:
                write(out / "manual-review.json", {"submission_ready": False, "findings": [{"case": cases[0], "observed": "A strict transcript violation"}]})
        return 1 if name == self.fail_stage else 0

    def evaluate(self, **kwargs):
        return evaluate(self.compiled, self.root / "evaluation", brief="A", strict=["security"],
            cases=["security", "negotiation"], live=True, env=self.root / ".env", slot="test-slot",
            run=self.fake_process, clock=self.clock, **kwargs)

    def test_strict_failure_stops_broader_calls_and_keeps_evidence(self):
        self.fail_stage = "strict_live"
        result = self.evaluate()
        self.assertEqual(result["status"], "blocked_strict_live")
        self.assertEqual(self.active_number, 1)
        self.assertFalse(result["submission_ready"])
        self.assertTrue((self.root / "evaluation/feedback.json").exists())

    def test_three_fresh_repeats_follow_strict_gate_without_builder_oracle_input(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "automated_gates_passed")
        simulations = [c for c in self.commands if "simulate" in c]
        self.assertEqual(len(simulations), 4)
        self.assertEqual(len({c[c.index("--out")+1] for c in simulations}), 4)
        self.assertEqual(result["case_trials"], {"security": 3, "negotiation": 3})
        self.assertTrue(all(c[c.index("--concurrency")+1] == "5" for c in simulations))
        self.assertFalse(result["test_oracles_sent_to_builder"])
        self.assertFalse(any("compile" in c or "build" in c for c in self.commands))

    def test_local_failure_prevents_publication(self):
        self.fail_stage = "strict_local"
        result = self.evaluate()
        self.assertEqual(result["status"], "blocked_local")
        self.assertEqual(len(self.commands), 1)

    def test_deadline_stops_new_simulations_and_unknown_started_run_needs_collection(self):
        result = self.evaluate(seconds=500)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.active_number, 0)
        self.assertIn("Insufficient budget", result["error"])

    def test_incomplete_remote_run_is_never_a_pass(self):
        self.uncertain = True
        result = self.evaluate()
        self.assertEqual(result["status"], "needs_collection")
        self.assertEqual(self.active_number, 1)
        self.assertEqual(result["simulation_id"], "sim-1")

    def test_reused_or_missing_trial_cannot_count_as_repeatability(self):
        calls, simulations = set(), set()
        result = self.result(["security"], 1)
        self.assertTrue(check_result(result, ["security"], self.digest, calls, simulations)[0])
        self.assertFalse(check_result(result, ["security"], self.digest, calls, simulations)[0])
        self.assertFalse(check_result(result, ["security", "missing"], self.digest, set(), set())[0])

    def test_manual_false_pass_and_prior_failure_block_same_candidate_promotion(self):
        history = self.root / "history"
        history.mkdir()
        write(history / "result.json", self.result(["security"], 0))
        write(history / "manual-review.json", {"submission_ready": False, "findings": [
            {"case": "security", "observed": "Disclosed debt before verification"}]})
        findings = feedback([history], ["security"])["findings"]
        self.assertEqual(findings[0]["priority"], "P0")
        self.assertEqual(self.evaluate(history=[history])["status"], "blocked_prior_evidence")

    def test_one_repeat_cannot_claim_three_repeat_gate(self):
        self.assertEqual(self.evaluate(reps=1)["status"], "partial_repetition_pass")

    def test_requested_speaking_style_reaches_every_live_stage_and_trace(self):
        result = self.evaluate(reps=1, style='corrects')
        self.assertEqual(result['style'], 'corrects')
        self.assertEqual(result['status'], 'partial_repetition_pass')
        for args in self.commands:
            if 'tools/live_smoke.py' in args:
                self.assertEqual(args[args.index('--style')+1], 'corrects')

    def test_scenario_evaluation_freezes_inputs_and_checks_the_base_bank_cases(self):
        snapshot = {'cases': [{'id': 'security', 'bank_case_id': 'PUB-01'},
                              {'id': 'negotiation', 'bank_case_id': 'PUB-01'}]}
        with patch('tools.improve.load_case_inputs', return_value=snapshot):
            result = self.evaluate(reps=1, case_inputs=self.root / 'mutable-scenarios.json')
        self.assertEqual(result['status'], 'partial_repetition_pass')
        self.assertEqual(self.practice_check.call_args.args[1], ['PUB-01'])
        self.assertTrue((self.root / 'evaluation/evaluation-case-inputs.json').exists())
        for args in self.commands:
            if 'tools/live_smoke.py' in args:
                self.assertEqual(Path(args[args.index('--cases')+1]), self.root / 'evaluation/evaluation-cases.json')

    def test_robustness_findings_keep_their_priority_without_enabling_promotion(self):
        history = self.root / 'history'
        history.mkdir()
        write(history / 'result.json', self.result(['security'], 0))
        write(history / 'manual-review.json', {'submission_ready': False, 'findings': [{
            'case': 'security', 'kind': 'robustness', 'policy_violation': False,
            'priority': 'P2', 'observed': 'Repeated an already answered identity question'}]})
        findings = feedback([history], ['security'])['findings']
        self.assertEqual(findings[0]['kind'], 'robustness_review')
        self.assertEqual(findings[0]['priority'], 'P2')
        self.assertTrue(findings[0]['blocks_promotion'])
        self.assertEqual(self.evaluate(history=[history])['status'], 'blocked_prior_evidence')

    def test_pass_label_alone_cannot_hide_missing_or_conflicting_evidence(self):
        for field, value in [('ledger_pass', False), ('official_outcome', 'inconclusive'),
                             ('call_ended', False), ('evaluation_issues', ['Conflicting official outcomes'])]:
            result = self.result(['security'], 1)
            result['rows'][0][field] = value
            with self.subTest(field=field):
                self.assertFalse(check_result(result, ['security'], self.digest, set(), set())[0])

    def test_inconclusive_and_judge_review_stay_distinct_from_policy_violations(self):
        history = self.root / 'history'
        history.mkdir()
        result = self.result(['security'], 0)
        result['rows'][0].update({'pass': False, 'ledger_pass': True,
                                 'official_outcome': 'inconclusive', 'failed': ['conversation: no verdict']})
        write(history / 'result.json', result)
        write(history / 'manual-review.json', {'submission_ready': False, 'findings': [],
              'evaluation_issues': [{'case': 'security', 'observed': 'Judge returned no reason'}]})
        findings = feedback([history], ['security'])['findings']
        self.assertEqual({row['kind'] for row in findings}, {'official_inconclusive', 'manual_evaluation_issue'})
        self.assertTrue(all(row['blocks_promotion'] for row in findings))
        self.assertEqual(self.evaluate(history=[history])['status'], 'blocked_prior_evidence')

    def test_official_pass_cannot_override_prepared_speech_violation(self):
        self.speech_mismatch = True
        self.assertEqual(self.evaluate()["status"], "blocked_speech_strict_live")
        self.assertEqual(self.active_number, 1)

    def test_review_of_current_trials_blocks_automatic_promotion(self):
        self.review_current = True
        self.assertEqual(self.evaluate()["status"], "blocked_prior_evidence")

    def test_missing_history_still_produces_an_incomplete_manifest(self):
        result = self.evaluate(history=[self.root / "missing"])
        self.assertEqual(result["status"], "incomplete_feedback")
        self.assertEqual(self.commands, [])
        self.assertTrue((self.root / "evaluation/manifest.json").exists())
