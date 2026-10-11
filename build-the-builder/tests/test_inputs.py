from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from builder.__main__ import main
from builder.contracts import Contracts, validate_value
from builder.inputs import InvalidInput, read_documents, read_json
from builder.policy import Policy
from tests.support import OfflineCase, ROOT


class InputTests(OfflineCase):
    def test_reads_full_sources_without_interpreting_order_as_precedence(self):
        self.assertEqual(len(self.documents), 2)
        self.assertIn("47.", self.documents[0].text)
        self.assertIn("before or after it", self.documents[1].text)
        self.assertNotEqual(self.documents[0].sha256, self.documents[1].sha256)

    def test_duplicate_or_secret_file_input_rejected(self):
        with self.assertRaises(InvalidInput):
            read_documents([ROOT / "briefs/brief-a.md"] * 2)
        with self.assertRaises(InvalidInput):
            read_documents([ROOT.parent / ".env"])

    def test_changed_source_hash_and_fabricated_quote_rejected(self):
        for field, value in (("sha256", "0" * 64), ("quote", "Invented policy text"), ("start_line", 0)):
            raw = deepcopy(self.a_raw)
            raw["evidence"]["bands"][0][field] = value
            with self.subTest(field=field), self.assertRaises(InvalidInput):
                Policy.load(raw, self.documents)

    def test_missing_provenance_and_overlapping_bands_rejected(self):
        raw = deepcopy(self.a_raw)
        del raw["evidence"]["identity_fields"]
        with self.assertRaises(InvalidInput):
            Policy.load(raw, self.documents)
        raw = deepcopy(self.a_raw)
        raw["bands"][1]["minimum"] = 7
        with self.assertRaises(InvalidInput):
            Policy.load(raw, self.documents)

    def test_json_duplicate_keys_and_nonfinite_numbers_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            for value in ('{"a": 1, "a": 2}', '{"a": NaN}'):
                path.write_text(value, encoding="utf-8")
                with self.assertRaises(InvalidInput):
                    read_json(path)

    def test_prepare_emits_honest_artifacts_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "run"
            args = ["prepare", "--brief", str(ROOT / "briefs/brief-a.md"), "--brief", str(ROOT / "briefs/brief-b.md"),
                    "--api", str(ROOT / "tools/openapi.json"), "--out", str(output)]
            with patch.dict("os.environ", {"FREYA_API_KEY": "DO_NOT_COPY_THIS_SENTINEL"}), redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            manifest = read_json(output / "manifest.json")
            self.assertEqual(manifest["model_calls"], 0)
            self.assertEqual(manifest["network_calls"], 0)
            self.assertEqual(manifest["workflow_generation"], "not_connected")
            self.assertEqual(manifest["official_freya_tests"], "not_run")
            self.assertEqual(len(read_json(output / "tool-templates.json")), 7)
            for path in output.iterdir():
                self.assertNotIn("DO_NOT_COPY_THIS_SENTINEL", path.read_text(encoding="utf-8"))
            events = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
            self.assertEqual((events[0]["event"], events[-1]["event"]), ("run_start", "run_end"))
            original = (output / "manifest.json").read_bytes()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)
            self.assertEqual((output / "manifest.json").read_bytes(), original)

    def test_check_policy_command_does_not_claim_semantic_extraction(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = main(["check-policy", "--brief", str(ROOT / "briefs/brief-a.md"), "--policy", str(ROOT / "tests/fixtures/practice-a.policy.json")])
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(stream.getvalue())["semantic_extraction_verified"])


class ContractTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.spec = read_json(ROOT / "tools/openapi.json")
        self.contracts = Contracts(self.spec)

    def test_tool_templates_preserve_fields_without_stale_defaults(self):
        schema = self.spec["paths"]["/create_promise"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        schema["properties"]["amount"]["default"] = 999
        tools = Contracts(self.spec).freya_tools()
        promise = next(t for t in tools if t["function"]["name"] == "create_promise")
        params = promise["function"]["parameters"]["properties"]
        self.assertNotIn("default", params["body"]["properties"]["amount"])
        self.assertEqual(params["body"]["required"], schema["required"])
        self.assertEqual(params["method"]["default"], "POST")
        self.assertEqual(promise["config"]["maxRetries"], 0)
        self.assertNotIn("Authorization", json.dumps(tools))
        self.assertNotIn("x-team-key", json.dumps(tools))

    def test_renamed_request_fields_are_read_from_api(self):
        schema = self.spec["paths"]["/create_promise"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        schema["properties"]["sum_due"] = schema["properties"].pop("amount")
        schema["required"] = ["sum_due" if x == "amount" else x for x in schema["required"]]
        changed = Contracts(self.spec)
        body = changed.bind_request("create_promise", {"customer": "synthetic", "product": "opaque-id", "money": 12.34, "day": "2030-01-04"},
                                    {"customer": "customer_id", "product": "product_id", "money": "sum_due", "day": "date"})
        self.assertEqual(body["sum_due"], 12.34)
        self.assertNotIn("amount", body)
        with self.assertRaises(InvalidInput):
            changed.validate_request("create_promise", {"customer_id": "synthetic", "product_id": "opaque-id", "amount": 12.34, "date": "2030-01-04"})

    def test_changed_allowed_values_enforced(self):
        schema = self.spec["paths"]["/transfer_to_agent"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        schema["properties"]["queue"]["enum"] = ["assistance_v2"]
        contracts = Contracts(self.spec)
        contracts.validate_request("transfer_to_agent", {"customer_id": "synthetic", "queue": "assistance_v2"})
        with self.assertRaises(InvalidInput):
            contracts.validate_request("transfer_to_agent", {"customer_id": "synthetic", "queue": "support"})

    def test_local_ref_resolved_and_external_ref_rejected(self):
        operation = self.spec["paths"]["/get_account"]["post"]
        self.spec["components"]["schemas"] = {"AccountRequest": operation["requestBody"]["content"]["application/json"]["schema"]}
        operation["requestBody"]["content"]["application/json"]["schema"] = {"$ref": "#/components/schemas/AccountRequest"}
        self.assertEqual(len(Contracts(self.spec).operations), 7)
        operation["requestBody"]["content"]["application/json"]["schema"] = {"$ref": "https://example.invalid/schema"}
        with self.assertRaises(InvalidInput):
            Contracts(self.spec)

    def test_unsupported_construct_and_missing_tool_rejected(self):
        spec = deepcopy(self.spec)
        spec["paths"]["/get_account"]["post"]["requestBody"]["content"]["application/json"]["schema"]["oneOf"] = []
        with self.assertRaises(InvalidInput):
            Contracts(spec)
        del self.spec["paths"]["/close_call"]
        with self.assertRaises(InvalidInput):
            Contracts(self.spec)

    def test_malformed_constraint_rejected_during_ingestion(self):
        schema = self.spec["paths"]["/create_promise"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        schema["properties"]["amount"]["minimum"] = "a lot"
        with self.assertRaises(InvalidInput):
            Contracts(self.spec)

    def test_boolean_not_number_and_iso_date_validation(self):
        for amount, day in ((True, "2026-10-14"), (float("nan"), "2026-10-14"), (42, "2026-02-30"), (42, "tomorrow")):
            with self.subTest(amount=amount, day=day), self.assertRaises(InvalidInput):
                self.contracts.validate_request("create_promise", {"customer_id": "synthetic", "product_id": "p1", "amount": amount, "date": day})

    def test_exclusive_bounds_and_closed_object(self):
        schema = {"type": "number", "exclusiveMinimum": 0}
        with self.assertRaises(InvalidInput):
            validate_value(0, schema)
        with self.assertRaises(InvalidInput):
            validate_value({"unexpected": 1}, {"type": "object", "properties": {}, "additionalProperties": False})
