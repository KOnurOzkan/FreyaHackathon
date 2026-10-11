"""A long model call must not mix inputs or builder code from different revisions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from builder.compile_run import run_build, run_compilation
from builder.contracts import Contracts
from builder.extract_run import run_extraction, write_json
from builder.inputs import InvalidInput, read_json, run_fingerprints
from tests.support import OfflineCase, ROOT
from tests.test_extraction import FakeProvider, fake_extraction
from tests.workflow_support import fake_plan


class RunIntegrityTests(OfflineCase):
    def copied_inputs(self, folder):
        paths = []
        for original in (ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md", ROOT / "tools/openapi.json"):
            target = Path(folder) / original.name
            target.write_bytes(original.read_bytes())
            paths.append(target)
        return paths[:2], paths[2]

    def test_brief_changed_during_model_call_never_becomes_an_accepted_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            briefs, api = self.copied_inputs(folder)
            provider = FakeProvider(fake_extraction(self.documents, self.b_raw))
            original = provider.extract
            def change(*args, **kwargs):
                result = original(*args, **kwargs)
                briefs[0].write_text(briefs[0].read_text(encoding="utf-8") + "\nChanged during generation.\n", encoding="utf-8")
                return result
            output = Path(folder) / "run"
            with patch.object(provider, "extract", side_effect=change), self.assertRaises(InvalidInput):
                run_extraction(briefs, api, output, provider, 60)
            self.assertFalse((output / "policy.json").exists())
            self.assertTrue((output / "extraction-attempt-1.json").exists())
            self.assertEqual(read_json(output / "manifest.json")["status"], "failed")
            self.assertEqual(provider.calls, 1)

    def test_builder_source_drift_is_detected_without_modifying_the_workspace(self):
        paths = [ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md", ROOT / "tools/openapi.json"]
        snapshot = run_fingerprints(paths)
        changed = deepcopy(snapshot)
        changed["builder"]["workflow.py"] = "different-source-hash"
        provider = FakeProvider(fake_extraction(self.documents, self.b_raw))
        with tempfile.TemporaryDirectory() as folder, patch("builder.inputs.run_fingerprints", side_effect=[changed]):
            # extract_run imports its initial snapshot function directly; the
            # final guard resolves inputs.run_fingerprints and sees the drift.
            with self.assertRaises(InvalidInput):
                run_extraction(paths[:2], paths[2], Path(folder) / "run", provider, 60)
            self.assertEqual(provider.calls, 1)

    def test_changed_prior_extraction_blocks_compilation_before_emitting_workflow(self):
        with tempfile.TemporaryDirectory() as folder:
            briefs, api = self.copied_inputs(folder)
            extraction = Path(folder) / "extraction.json"
            write_json(extraction, fake_extraction(self.documents, self.b_raw))
            contracts = Contracts(read_json(api))
            provider = FakeProvider(fake_plan(self.documents, True, contracts))
            original = provider.extract
            def change(*args, **kwargs):
                result = original(*args, **kwargs)
                extraction.write_text(extraction.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                return result
            output = Path(folder) / "run"
            with patch.object(provider, "extract", side_effect=change), self.assertRaises(InvalidInput):
                run_compilation(briefs, api, extraction, output, provider, 60)
            self.assertFalse((output / "workflow.json").exists())
            self.assertTrue((output / "dialogue-plan.json").exists())

    def test_api_changed_between_extraction_and_compilation_stops_the_complete_build(self):
        with tempfile.TemporaryDirectory() as folder:
            briefs, api = self.copied_inputs(folder)
            provider = FakeProvider(fake_extraction(self.documents, self.b_raw))
            def change_after_extraction(*args, **kwargs):
                result = run_extraction(*args, **kwargs)
                api.write_text(api.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                return result
            output = Path(folder) / "run"
            with patch("builder.compile_run.run_extraction", side_effect=change_after_extraction), self.assertRaises(InvalidInput):
                run_build(briefs, api, output, provider, 120)
            self.assertFalse((output / "compiled").exists())
            self.assertEqual(provider.calls, 1)

    def test_development_recompile_reuses_only_an_intact_plan_with_identical_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            briefs, api = self.copied_inputs(folder)
            extraction = Path(folder) / "extraction.json"
            write_json(extraction, fake_extraction(self.documents, self.b_raw))
            provider = FakeProvider(fake_plan(self.documents, True, Contracts(read_json(api))))
            first = Path(folder) / "first"
            run_compilation(briefs, api, extraction, first, provider, 60)
            result = run_compilation(briefs, api, extraction, Path(folder) / "second", None, 60, reuse_plan_from=first)
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["mode"], "development_recompile")
            self.assertEqual(provider.calls, 1)
            raw = read_json(first / "dialogue-plan.json")
            raw["plans"][0]["messages"]["identity_wrong"] = "Edited after generation"
            (first / "dialogue-plan.json").write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(InvalidInput):
                run_compilation(briefs, api, extraction, Path(folder) / "changed-plan", None, 60, reuse_plan_from=first)
            api.write_text(api.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaises(InvalidInput):
                run_compilation(briefs, api, extraction, Path(folder) / "changed-api", None, 60, reuse_plan_from=first)
