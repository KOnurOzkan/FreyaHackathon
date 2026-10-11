"""Exercise the whole run controller with fake elapsed time and platform processes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from builder.inputs import InvalidInput, read_json
from builder.extract_run import write_json
from tools.complete_run import complete_run, run_phase, Deadline, remote_recovery_state, ROOT
from tools.practice_inputs import digest


class Clock:
    value = 0
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


class CompleteRunTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.phases = []

    def build(self, briefs, api, output, provider, seconds):
        self.assertLessEqual(seconds, 1800)
        (output / "compiled").mkdir(parents=True)
        write_json(output / "compiled/workflow.json", {"synthetic": True})
        self.digest = digest(output / "compiled/workflow.json")
        (output / "trace.jsonl").write_text('\n'.join(json.dumps({"event": "model_call_start", "model": "fake"})
            for _ in range(2)) + '\n', encoding="utf-8")
        self.clock.advance(400)
        return {"status": "compiled_locally", "workflow_sha256": self.digest, "model_calls": 2}

    def phase(self, name, workflow, env, output, slot, case, seconds, codex_home):
        self.phases.append((name, seconds))
        output.mkdir(exist_ok=True)
        self.clock.advance(60 if name != "simulate" else 300)
        if name == "simulate":
            write_json(output / "simulation-attempted.json", {})
            write_json(output / "simulation-started.json", {"simulation_id": "synthetic-sim"})
            write_json(output / "result.json", {"status": "completed"})
        if name == "export":
            write_json(output / "export-verification.json", {"status": "verified_export", "workflow_sha256": self.digest,
                "workflow_id": "workflow", "agent_id": "agent", "version": 2})
            write_json(output / "submission-export.json", {"synthetic": True})
        return 0

    def run_pipeline(self, folder, **kwargs):
        return complete_run([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"], ROOT / "tools/openapi.json",
            Path(folder) / "run", None, live=True, env="never-read.env",
            build=self.build, phase=kwargs.pop("phase", self.phase), clock=self.clock, **kwargs)

    def test_successful_sequence_shares_one_budget_and_keeps_trace(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_pipeline(folder)
            self.assertEqual([p[0] for p in self.phases], ["publish", "simulate", "export"])
            self.assertTrue(all(a[1] > b[1] for a, b in zip(self.phases, self.phases[1:])))
            self.assertEqual(result["status"], "verified_export")
            self.assertTrue(result["submission_export"])
            self.assertEqual(result["model_calls"], 2)
            trace = [json.loads(line) for line in (Path(folder) / "run/trace.jsonl").read_text().splitlines()]
            self.assertEqual(sum(e["event"] == "model_call_start" for e in trace), 2)
            self.assertEqual(trace[-1]["event"], "run_end")

    def test_default_mode_never_runs_platform_or_reads_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            result = complete_run([], "unused-api", Path(folder) / "run", None, build=self.build,
                                  phase=lambda *a: self.fail("Unexpected live access"), clock=self.clock)
            self.assertEqual(result["status"], "compiled_locally")
            self.assertFalse(result["submission_export"])
            self.assertEqual(result["official_freya_tests"], "not_run")

    def test_publish_failure_never_starts_simulation(self):
        def reject(*args):
            self.phases.append(args[0])
            return 2
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_pipeline(folder, phase=reject)
            self.assertEqual(self.phases, ["publish"])
            self.assertEqual(result["status"], "failed")

    def test_insufficient_time_skips_simulation_instead_of_consuming_export_reserve(self):
        def slow_publish(*args):
            self.phases.append(args[0])
            self.clock.advance(1050)
            return 0
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_pipeline(folder, phase=slow_publish)
            self.assertEqual(self.phases, ["publish"])
            self.assertEqual(result["error_type"], "RunExpired")
            self.assertFalse(result["submission_export"])

    def test_timed_out_simulation_is_recorded_for_collection_never_restarted(self):
        def timeout(name, workflow, env, output, *args):
            self.phases.append(name)
            output.mkdir(exist_ok=True)
            if name == "simulate":
                write_json(output / "simulation-started.json", {"simulation_id": "must-collect"})
                raise subprocess.TimeoutExpired("test-process", 10)
            return 0
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_pipeline(folder, phase=timeout)
            self.assertEqual(result["status"], "needs_collection")
            self.assertEqual(result["simulation_id"], "must-collect")
            self.assertEqual(self.phases, ["publish", "simulate"])
            self.assertFalse(result["submission_export"])

    def test_lost_simulation_creation_response_is_explicitly_uncertain(self):
        def lost(name, workflow, env, output, *args):
            self.phases.append(name)
            output.mkdir(exist_ok=True)
            if name == "simulate":
                write_json(output / "simulation-attempted.json", {})
                return 2
            return 0
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_pipeline(folder, phase=lost)
            self.assertEqual(result["status"], "simulation_creation_uncertain")
            self.assertFalse(result["submission_export"])
            self.assertEqual(self.phases, ["publish", "simulate"])

    def test_unverified_version_prevents_export_even_if_remote_simulation_completed(self):
        def incomplete(*args):
            result = self.phase(*args)
            return 1 if args[0] == "simulate" else result
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_pipeline(folder, phase=incomplete)
            self.assertEqual(result["status"], "failed")
            self.assertEqual([p[0] for p in self.phases], ["publish", "simulate"])
            self.assertEqual(result["official_freya_tests"], "completed")

    def test_output_reuse_and_invalid_budgets_fail_before_work(self):
        with tempfile.TemporaryDirectory() as folder:
            self.run_pipeline(folder)
            with self.assertRaises(FileExistsError):
                self.run_pipeline(folder)
        for seconds in (0, 59, 1801, True):
            with self.subTest(seconds=seconds), self.assertRaises(InvalidInput):
                Deadline(seconds)

    def test_process_call_has_timeout_no_shell_and_no_inherited_paid_key(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"OPENAI_API_KEY": "paid-secret"}):
            with patch("tools.complete_run.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as process:
                self.assertEqual(run_phase("publish", "workflow with spaces.json", "local.env", "out", "slot", "case", 75, folder), 0)
            self.assertFalse(process.call_args.kwargs["shell"])
            self.assertEqual(process.call_args.kwargs["timeout"], 75)
            self.assertNotIn("OPENAI_API_KEY", process.call_args.kwargs["env"])
            self.assertIn("workflow with spaces.json", process.call_args.args[0])

    def test_incompatible_practice_case_is_rejected_before_model_or_platform_work(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(InvalidInput):
            complete_run([ROOT / "briefs/brief-a.md"], ROOT / "tools/openapi.json", Path(folder) / "run", None,
                live=True, env="never-read.env", case="PUB-11", build=lambda *a: self.fail("Model build started"),
                phase=lambda *a: self.fail("Platform work started"))
        self.assertEqual(self.phases, [])

    def test_final_run_on_unseen_brief_builds_publishes_and_exports_without_self_test(self):
        seen = []
        def phase(name, workflow, env, output, slot, case, seconds, codex_home):
            seen.append((name, slot, case))
            return self.phase(name, workflow, env, output, slot, case, seconds, codex_home)
        with tempfile.TemporaryDirectory() as folder:
            unseen = Path(folder) / "unseen-brief.md"
            unseen.write_text("# A brief no practice customer was written for", encoding="utf-8")
            result = complete_run([unseen], ROOT / "tools/openapi.json", Path(folder) / "run", None, live=True,
                env="never-read.env", slot="final-slot", build=self.build, phase=phase, clock=self.clock, final=True)
            self.assertEqual(seen, [("publish", "final-slot", None), ("export", "final-slot", None)])
            self.assertEqual(result["status"], "verified_export")
            self.assertTrue(result["submission_export"])
            self.assertEqual(result["mode"], "complete_final_run")
            self.assertTrue(result["self_test"].startswith("not_run"))
            self.assertEqual(result["official_freya_tests"], "not_run")

    def final_run(self, folder, phase):
        unseen = Path(folder) / "unseen-brief.md"
        unseen.write_text("# A brief no practice customer was written for", encoding="utf-8")
        waits = []
        def sleep(seconds):
            waits.append(seconds)
            self.clock.advance(seconds)
        result = complete_run([unseen], ROOT / "tools/openapi.json", Path(folder) / "run", None, live=True,
            env="never-read.env", slot="final-slot", build=self.build, phase=phase, clock=self.clock,
            final=True, sleep=sleep)
        return result, waits

    def test_final_run_waits_out_a_publish_outage_in_fresh_folders(self):
        failures = {"publish": 2}
        folders = []
        def flaky(name, workflow, env, output, slot, case, seconds, codex_home):
            folders.append((name, output.name))
            if failures.get(name):
                failures[name] -= 1
                output.mkdir(exist_ok=True)
                (output / "trace.jsonl").write_text(json.dumps({"event": "api_error"}) + chr(10), encoding="utf-8")
                self.clock.advance(60)
                return 2
            return self.phase(name, workflow, env, output, slot, case, seconds, codex_home)
        with tempfile.TemporaryDirectory() as folder:
            result, waits = self.final_run(folder, flaky)
            self.assertEqual(folders, [("publish", "platform"), ("publish", "platform-2"), ("publish", "platform-3"),
                                       ("export", "platform-3")])
            self.assertEqual(waits, [45, 45])
            self.assertEqual(result["status"], "verified_export")
            self.assertEqual(result["export"], "platform-3/submission-export.json")
            trace = [json.loads(line) for line in (Path(folder) / "run/trace.jsonl").read_text().splitlines()]
            self.assertEqual(sum(e["event"] == "phase_failed_retrying" for e in trace), 2)
            self.assertEqual([e["stage"] for e in trace if e.get("event") == "api_error"], ["platform", "platform-2"])

    def test_final_run_retries_export_in_the_same_folder(self):
        failures = {"export": 1}
        folders = []
        def flaky(name, workflow, env, output, slot, case, seconds, codex_home):
            folders.append((name, output.name))
            if failures.get(name):
                failures[name] -= 1
                return 2
            return self.phase(name, workflow, env, output, slot, case, seconds, codex_home)
        with tempfile.TemporaryDirectory() as folder:
            result, waits = self.final_run(folder, flaky)
            self.assertEqual(folders, [("publish", "platform"), ("export", "platform"), ("export", "platform")])
            self.assertEqual(result["status"], "verified_export")

    def test_final_run_stops_retrying_when_the_budget_runs_out(self):
        def down(name, workflow, env, output, slot, case, seconds, codex_home):
            self.phases.append(name)
            self.clock.advance(60)
            return 2
        with tempfile.TemporaryDirectory() as folder:
            result, waits = self.final_run(folder, down)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_type"], "RunExpired")
            self.assertFalse(result["submission_export"])
            self.assertLessEqual(self.clock.value, 1800)
            self.assertTrue(all(name == "publish" for name in self.phases))

    def test_final_run_requires_live_mode(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(InvalidInput):
            complete_run([], ROOT / "tools/openapi.json", Path(folder) / "run", None, final=True,
                         build=lambda *a: self.fail("Model build started"))

    def test_final_export_is_untested_and_passes_no_practice_case(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("tools.complete_run.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as process:
                run_phase("export", "workflow.json", "local.env", "out", "final-slot", None, 75, folder)
                args = process.call_args.args[0]
                self.assertIn("--untested", args)
                self.assertNotIn("--case", args)
                run_phase("export", "workflow.json", "local.env", "out", "slot", "PUB-11", 75, folder)
                args = process.call_args.args[0]
                self.assertNotIn("--untested", args)
                self.assertEqual(args[args.index("--case") + 1], "PUB-11")

    def test_changed_workflow_after_publication_cannot_start_a_simulation(self):
        def changed(name, workflow, *args):
            self.phases.append(name)
            workflow.write_text('{"manually_changed":true}', encoding="utf-8")
            return 0
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_pipeline(folder, phase=changed)
            self.assertEqual(self.phases, ["publish"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("Workflow changed", result["error"])
            self.assertFalse(result["submission_export"])

    def test_changed_helper_source_stops_before_the_next_live_phase(self):
        original = digest
        def changed(path):
            return "changed-helper" if Path(path).name == "live_smoke.py" and self.phases else original(path)
        with tempfile.TemporaryDirectory() as folder, patch("tools.complete_run.digest", side_effect=changed):
            result = self.run_pipeline(folder)
            self.assertEqual([p[0] for p in self.phases], ["publish"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("helper source changed", result["error"])

    def test_running_and_malformed_snapshots_still_need_collection(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            write_json(output / "simulation-started.json", {"simulation_id": "existing-sim"})
            for content in ('{"status":"running"}', '{"status":"completed"', '[]'):
                (output / "result.json").write_text(content, encoding="utf-8")
                self.assertEqual(remote_recovery_state(output), {"status": "needs_collection", "simulation_id": "existing-sim"})
            for status in ("completed", "failed", "paused"):
                (output / "result.json").write_text(json.dumps({"status": status}), encoding="utf-8")
                self.assertIsNone(remote_recovery_state(output))

    def test_partial_creation_record_requires_resolution_not_a_new_simulation(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            for content in ('{"simulation_id":', '{}', '[]'):
                (output / "simulation-started.json").write_text(content, encoding="utf-8")
                self.assertEqual(remote_recovery_state(output), {"status": "simulation_creation_uncertain"})

    def test_killed_child_partial_trace_and_result_still_close_the_parent_run(self):
        def killed(name, workflow, env, output, *args):
            output.mkdir(exist_ok=True)
            if name == "simulate":
                write_json(output / "simulation-started.json", {"simulation_id": "must-collect"})
                (output / "trace.jsonl").write_text('{"event":"started"}\n{"event":', encoding="utf-8")
                (output / "result.json").write_text('{"status":', encoding="utf-8")
                raise subprocess.TimeoutExpired("test-process", 10)
            return 0
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_pipeline(folder, phase=killed)
            self.assertEqual(result["status"], "needs_collection")
            self.assertEqual(result["simulation_id"], "must-collect")
            self.assertEqual(len(result["evidence_errors"]), 2)
            self.assertFalse(result["submission_export"])
            output = Path(folder) / "run"
            self.assertEqual(read_json(output / "manifest.json")["status"], "needs_collection")
            self.assertEqual(json.loads((output / "trace.jsonl").read_text().splitlines()[-1])["event"], "run_end")
