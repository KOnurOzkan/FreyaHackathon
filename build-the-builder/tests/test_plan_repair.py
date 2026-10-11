"""One source-backed structural repair, bounded by the original build deadline."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from builder.codex_provider import ProviderError
from builder.compile_run import run_compilation
from builder.contracts import Contracts
from builder.dialogue_plan import validate_plan
from builder.inputs import InvalidInput, read_json
from tests.support import OfflineCase, ROOT
from tests.test_extraction import FakeProvider, fake_extraction, short_reference
from tests.workflow_support import fake_plan


class PlanRepairTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.contracts = Contracts(read_json(ROOT / "tools/openapi.json"))
        self.good = fake_plan(self.documents, True, self.contracts)
        self.broken = deepcopy(self.good)
        self.broken["plans"][0]["evidence"] = [entry for entry in self.broken["plans"][0]["evidence"]
                                              if entry["section"] != "transfer_forbids_notes"]

    def run_plan(self, folder, provider, timeout=60):
        folder = Path(folder)
        extraction = folder / "extraction.json"
        extraction.write_text(json.dumps(fake_extraction(self.documents, self.b_raw)), encoding="utf-8")
        return run_compilation([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"],
            ROOT / "tools/openapi.json", extraction, folder / "compiled", provider, timeout)

    def provider(self, replies, times=None, clock=None):
        provider = FakeProvider(replies[0])
        original = provider.extract
        provider.prompts, provider.timeouts = [], []

        def extract(prompt, schema, **kwargs):
            index = provider.calls
            provider.result = replies[min(index, len(replies) - 1)]
            provider.prompts.append(prompt)
            provider.timeouts.append(kwargs["timeout"])
            result = original(prompt, schema, **kwargs)
            if times is not None:
                clock[0] = times[index]
            return result

        provider.extract = extract
        return provider

    def test_missing_evidence_is_repaired_once_with_original_inputs_and_trace(self):
        provider = self.provider([self.broken, self.good])
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_plan(temp, provider)
            output = Path(temp) / "compiled"
            self.assertEqual(result["status"], "compiled_locally")
            self.assertEqual(result["model_calls"], 2)
            self.assertEqual(result["repairs_used"], 1)
            self.assertEqual(read_json(output / "dialogue-plan-attempt-1.json"), self.broken)
            self.assertEqual(read_json(output / "dialogue-plan-attempt-2.json"), self.good)
            self.assertIn("transfer_forbids_notes", provider.prompts[1])
            self.assertIn("STRUCTURAL VALIDATION FEEDBACK", provider.prompts[1])
            self.assertIn(self.documents[0].sha256, provider.prompts[1])
            events = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
            starts = [e for e in events if e["event"] == "model_call_start"]
            self.assertEqual(len(starts), 2)
            self.assertIn("repair", starts[1]["purpose"].lower())

    def test_evidence_feedback_identifies_missing_and_duplicate_sections(self):
        broken = deepcopy(self.broken)
        entry = broken["plans"][0]["evidence"][0]
        broken["plans"][0]["evidence"].append(entry)
        with self.assertRaises(InvalidInput) as caught:
            validate_plan(broken, self.documents, self.b, self.contracts)
        self.assertIn("transfer_forbids_notes", str(caught.exception))
        self.assertIn(entry["section"], str(caught.exception))
        self.assertIn("duplicate", str(caught.exception).lower())

    def gaps(self, folder):
        output = Path(folder) / "compiled"
        events = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
        return read_json(output / "manifest.json").get("best_effort_gaps", []), [
            e for e in events if e["event"] == "best_effort_gaps"]

    def test_explicit_unsupported_rule_builds_best_effort_and_is_reported(self):
        approximated = deepcopy(self.good)
        approximated["unsupported"] = [{"message": "The brief requires an unsupported negotiation strategy",
                                        "sources": [short_reference(self.documents[0])]}]
        provider = self.provider([approximated, self.good])
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_plan(temp, provider)
            self.assertEqual(result["status"], "compiled_locally")
            self.assertTrue((Path(temp) / "compiled/workflow.json").is_file())
            gaps, events = self.gaps(temp)
        self.assertEqual(provider.calls, 1)
        self.assertIn({"stage": "dialogue_plan", "message": "The brief requires an unsupported negotiation strategy"}, gaps)
        self.assertEqual(len(events), 1)

    def test_semantically_incompatible_strategy_is_coerced_without_repair_and_reported(self):
        incompatible = deepcopy(self.good)
        incompatible["plans"][0]["transfer_forbids_notes"] = False
        provider = self.provider([incompatible, self.good])
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_plan(temp, provider)
            self.assertEqual(result["status"], "compiled_locally")
            payload = read_json(Path(temp) / "compiled/runtime-payload.json")
            gaps, _ = self.gaps(temp)
        self.assertEqual(provider.calls, 1)
        self.assertTrue(payload["plan"]["transfer_forbids_notes"])
        self.assertTrue(any(g["stage"] == "compiler" and "transfer" in g["message"] for g in gaps))

    def test_unrecognized_strategy_value_gets_one_structural_repair(self):
        incompatible = deepcopy(self.good)
        incompatible["plans"][0]["first_persuasion"] = "a_new_unsupported_strategy"
        provider = self.provider([incompatible, self.good])
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_plan(temp, provider)
        self.assertEqual(result["status"], "compiled_locally")
        self.assertEqual(provider.calls, 2)
        self.assertIn("first_persuasion", provider.prompts[1])

    def test_second_structural_failure_stops_without_a_workflow(self):
        provider = self.provider([self.broken, self.broken, self.good])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(InvalidInput):
                self.run_plan(temp, provider)
            self.assertEqual(provider.calls, 2)
            output = Path(temp) / "compiled"
            self.assertEqual(read_json(output / "manifest.json")["status"], "failed")
            self.assertTrue((output / "dialogue-plan-attempt-2.json").is_file())
            self.assertFalse((output / "workflow.json").exists())

    def test_insufficient_remaining_time_does_not_start_repair(self):
        clock = [0]
        provider = self.provider([self.broken, self.good], [15], clock)
        with tempfile.TemporaryDirectory() as temp, patch("builder.compile_run.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(ProviderError):
                self.run_plan(temp, provider, 20)
            self.assertEqual(provider.calls, 1)
            self.assertFalse((Path(temp) / "compiled/workflow.json").exists())

    def test_late_repair_response_does_not_emit_workflow_or_reset_deadline(self):
        clock = [0]
        provider = self.provider([self.broken, self.good], [4, 25], clock)
        with tempfile.TemporaryDirectory() as temp, patch("builder.compile_run.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(ProviderError):
                self.run_plan(temp, provider, 20)
            self.assertEqual(provider.calls, 2)
            self.assertLess(provider.timeouts[1], provider.timeouts[0])
            self.assertFalse((Path(temp) / "compiled/workflow.json").exists())
