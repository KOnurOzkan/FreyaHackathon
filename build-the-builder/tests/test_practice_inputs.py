import json
from pathlib import Path
import tempfile
import unittest

from builder.inputs import InvalidInput
from tools.practice_inputs import ROOT, check_practice_inputs, check_practice_manifest, digest


class PracticeInputsTests(unittest.TestCase):
    def setUp(self):
        self.a = ROOT / "briefs/brief-a.md"
        self.b = ROOT / "briefs/brief-b.md"
        self.api = ROOT / "tools/openapi.json"

    def test_unchanged_a_and_amended_b_inputs_match_their_cases(self):
        self.assertEqual(check_practice_inputs([self.a], self.api, "PUB-01,PUB-10")["brief"], "A")
        self.assertEqual(check_practice_inputs([self.a, self.b], self.api, "PUB-11,PUB-13")["brief"], "B")

    def test_unknown_duplicate_cross_brief_or_mismatched_cases_are_rejected(self):
        for cases in ("PUB-01,PUB-11", "PUB-01,PUB-01", "unseen", "", "PUB-11"):
            with self.subTest(cases=cases), self.assertRaises(InvalidInput):
                check_practice_inputs([self.a], self.api, cases)
        with self.assertRaises(InvalidInput):
            check_practice_inputs([self.a, self.b], self.api, "PUB-01")

    def test_paths_can_change_but_policy_and_api_contents_must_match(self):
        with tempfile.TemporaryDirectory() as folder:
            renamed = Path(folder) / "copied.md"
            renamed.write_bytes(self.a.read_bytes())
            check_practice_inputs([renamed], self.api, "PUB-01")
            renamed.write_bytes(self.a.read_bytes() + b"\nChanged policy\n")
            with self.assertRaises(InvalidInput):
                check_practice_inputs([renamed], self.api, "PUB-01")
            changed_api = Path(folder) / "api.json"
            value = json.loads(self.api.read_text())
            value["servers"][0]["url"] = "https://different-bank.invalid/v1"
            changed_api.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(InvalidInput):
                check_practice_inputs([self.a], changed_api, "PUB-01")

    def test_compiled_input_fingerprints_guard_existing_candidate_evaluation(self):
        manifest = {"documents": [{"sha256": digest(self.a)}], "api": {"sha256": digest(self.api)}}
        check_practice_manifest(manifest, ["PUB-01"])
        with self.assertRaises(InvalidInput):
            check_practice_manifest(manifest, ["PUB-11"])
        for incomplete in ({}, {"documents": manifest["documents"]}):
            with self.assertRaises(InvalidInput):
                check_practice_manifest(incomplete, ["PUB-01"])
