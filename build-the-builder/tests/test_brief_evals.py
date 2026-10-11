"""Calibrate the evaluation harness without model calls or network access."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile

from builder.contracts import Contracts
from builder.extraction import make_prompt, validate_extraction
from builder.extract_run import write_json
from builder.inputs import read_documents, read_json
from builder.policy import RULE_FIELDS
from builder.workflow import compile_workflow
from tests.brief_evals import (prepare_pack, check_case, REQUEST_NAMES, PRODUCT_NAMES, PRODUCT_TYPES)
from tests.support import OfflineCase
from tests.test_extraction import fake_extraction
from tests.workflow_support import fake_plan


class BriefEvaluationTests(OfflineCase):
    def prepare(self, temp, name):
        pack = Path(temp) / "pack"
        manifest = prepare_pack(pack)
        case = next(c for c in manifest["cases"] if c["name"] == name)
        folder = pack / name
        documents = read_documents([folder / p for p in case["briefs"]])
        contracts = Contracts(read_json(folder / "openapi.json"))
        return pack, case, folder, documents, contracts

    def make_artifact(self, temp, name):
        pack, case, folder, documents, contracts = self.prepare(temp, name)
        expected = read_json(folder / "expected.json")
        raw = deepcopy(expected["policy"])
        raw.update(schema_version=1, evidence={section: [{"document": documents[0].id,
            "sha256": documents[0].sha256, "start_line": 1, "end_line": 1,
            "quote": documents[0].text.splitlines()[0]}] for section in RULE_FIELDS})
        extraction = fake_extraction(documents, raw)
        plan = fake_plan(documents, True, contracts)
        for key, value in expected["plan"].items():
            if key == "recorded_promise":
                plan["plans"][0]["messages"][key] = value
            else:
                plan["plans"][0][key] = value
        if name in {"api_identity_change", "raw_packet"}:
            plan["plans"][0]["identity_hint_messages"] = []
            plan["plans"][0]["numeric_date_order"] = "not_applicable"
            plan["plans"][0]["verification_questions"] = [
                {"field": f, "question": "Please confirm your " + f, "normalization": "text"}
                for f in raw["identity_fields"]]
            if name == "raw_packet":
                for label in plan["plans"][0]["product_labels"]:
                    label["type"] = PRODUCT_TYPES[label["type"]]
            for binding in extraction["bindings"]:
                for field in binding["request"]:
                    field["field"] = REQUEST_NAMES[field["role"]]
                for field in binding["response"]:
                    role = field["role"]
                    field["field"] = ("payload.accepted" if role == "ok" else
                        "payload.accounts" if role == "products" else
                        "payload.accounts[]." + PRODUCT_NAMES[role] if binding["tool"] == "get_account" else
                        "payload." + role)
        workflow, report = compile_workflow(extraction, plan, documents, contracts)
        accepted = validate_extraction(extraction, documents, contracts)
        run = Path(temp) / "run"
        compiled = run / "compiled"
        compiled.mkdir(parents=True)
        for file, data in (("workflow.json", workflow), ("runtime-payload.json", report["runtime_payload"]),
                           ("policy.json", accepted["policy"]), ("bindings.json", accepted["bindings"]),
                           ("dialogue-plan.json", plan)):
            write_json(compiled / file, data)
        write_json(compiled / "manifest.json", {"workflow_sha256": hashlib.sha256((compiled / "workflow.json").read_bytes()).hexdigest()})
        write_json(run / "eval-inputs.json", case)
        return pack, run

    def test_calendar_oracle_and_runtime_audit_accept_correct_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            pack, run = self.make_artifact(temp, "calendar_amendment")
            result = check_case(pack, "calendar_amendment", run)
            self.assertEqual(result["failed"], [])

    def test_api_oracle_checks_all_renamed_fields_and_real_generated_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            pack, run = self.make_artifact(temp, "api_identity_change")
            result = check_case(pack, "api_identity_change", run)
            self.assertEqual(result["failed"], [])

    def test_single_raw_packet_three_product_calendar_and_code_oracles(self):
        with tempfile.TemporaryDirectory() as temp:
            pack, run = self.make_artifact(temp, "raw_packet")
            result = check_case(pack, "raw_packet", run)
            self.assertEqual(result["failed"], [])
            self.assertEqual(result["generated_code_scenarios"], 21)

    def test_raw_packet_has_one_input_and_does_not_expose_expected_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            _, case, folder, documents, contracts = self.prepare(temp, "raw_packet")
            self.assertEqual(case["briefs"], ["packet.md"])
            self.assertEqual(len(documents), 1)
            prompt = make_prompt(documents, contracts)
            for excluded in ("expected.json", "random-r-482", "2028-01-12", "2028-03-10"):
                self.assertNotIn(excluded, prompt)
            text = documents[0].text
            self.assertLess(text.index("Signed amendment C-17 is the final authority"),
                            text.index("# Superseded operations email C-16"))
            self.assertIn("replaces only wire code values", text)
            self.assertNotIn("Both ordered security questions and the noncounting swapped-date hint stay.", text)
            self.assertIn("hint behavior are those specified in the approved identity amendment.", text)

    def test_oracle_rejects_stale_base_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            pack, run = self.make_artifact(temp, "calendar_amendment")
            path = run / "compiled/policy.json"
            raw = read_json(path)
            raw["calendar"]["callback_start"] = "10:00"
            import json
            path.write_text(json.dumps(raw), encoding="utf-8")
            result = check_case(pack, "calendar_amendment", run)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("callback_start" in item for item in result["failed"]))

    def test_changed_generated_artifact_is_not_accepted_as_the_original_run(self):
        with tempfile.TemporaryDirectory() as temp:
            pack, run = self.make_artifact(temp, "calendar_amendment")
            path = run / "compiled/workflow.json"
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            result = check_case(pack, "calendar_amendment", run)
            self.assertIn("Generated workflow changed after compilation", result["failed"])

    def test_oracle_is_not_in_model_inputs_and_fixture_host_cannot_be_published_accidentally(self):
        with tempfile.TemporaryDirectory() as temp:
            _, case, folder, documents, contracts = self.prepare(temp, "calendar_amendment")
            prompt = make_prompt(documents, contracts)
            self.assertNotIn("expected.json", prompt)
            self.assertNotIn("expected.json", case["briefs"])
            self.assertEqual(contracts.base_url, "https://synthetic-bank.invalid/v1")
            self.assertEqual(case["briefs"][-1], "old-email.md")
            self.assertIn("supersedes the earlier operations email C-16", prompt)

    def test_contradiction_is_only_a_pass_with_explicit_blocker_and_no_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            pack, *_ = self.prepare(temp, "conflicting_memos")
            run = Path(temp) / "run"
            run.mkdir()
            write_json(run / "manifest.json", {"status": "blocked"})
            write_json(run / "blockers.json", [{"kind": "contradiction"}])
            self.assertEqual(check_case(pack, "conflicting_memos", run)["status"], "passed")
            write_json(run / "policy.json", {})
            self.assertEqual(check_case(pack, "conflicting_memos", run)["status"], "failed")
