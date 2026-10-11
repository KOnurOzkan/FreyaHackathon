from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from builder.codex_provider import CodexProvider, ProviderError, child_environment
from builder.contracts import Contracts
from builder.extraction import (REQUEST_ROLES, RESPONSE_ROLES, make_prompt, source_units,
                                extraction_schema, validate_extraction)
from builder.extract_run import run_extraction
from builder.inputs import InvalidInput, read_json
from tests.support import OfflineCase, ROOT


def short_reference(document, line=1):
    return {"document": document.id, "start_line": line, "end_line": line,
            "quote": document.text.splitlines()[line - 1]}


def fake_extraction(documents, fixture):
    """Test-only provider response. Never imported or used by the real builder."""
    policy = deepcopy(fixture)
    del policy["schema_version"]
    policy["bands"] = [{"min_days_late": item["minimum"], "max_days_late": item["maximum"],
                        "business_days": item["business_days"], "attempts": item["attempts"]}
                       for item in policy["bands"]]
    policy["evidence"] = [{"section": key, "sources": [{k: v for k, v in ref.items() if k != "sha256"}
                           for ref in refs]} for key, refs in policy["evidence"].items()]
    for key in ("floor_fields", "note_reasons", "transfer_queues"):
        policy[key] = [{"role": k, "field": v} for k, v in policy[key].items()]
    policy["outcomes"] = [{"role": k, "outcome": v["outcome"], "reason": v.get("reason", "")}
                          for k, v in policy["outcomes"].items()]
    requirements, coverage = [], []
    for unit in source_units(documents):
        key = "r" + str(len(requirements))
        reference = {"document": unit["document"], "start_line": unit["start_line"],
                     "end_line": unit["end_line"], "quote": unit["text"]}
        requirements.append({"id": key, "text": unit["text"], "sources": [reference],
                             "status": "effective", "replaced_by": [], "parameter_sections": []})
        coverage.append({"unit": unit["id"], "disposition": "policy", "requirement_ids": [key],
                         "explanation": "Synthetic coverage for a structural validator test"})
    bindings = []
    for name, roles in REQUEST_ROLES.items():
        responses = [{"role": role, "field": "products[]." + role if name == "get_account" and
                      role not in {"ok", "products"} else role} for role in sorted(RESPONSE_ROLES[name])]
        bindings.append({"tool": name, "request": [{"role": role, "field": role} for role in sorted(roles)],
                         "response_status": "200", "response": responses})
    return {"schema_version": 1, "policy_candidates": [policy], "requirements": requirements,
            "precedence": [], "coverage": coverage, "bindings": bindings, "blockers": []}


class FakeProvider:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def check_access(self, timeout):
        return {"provider": "test_double", "model": "offline-test", "authentication": "none"}

    def extract(self, prompt, schema, *, timeout, event, purpose="Offline fake response"):
        self.calls += 1
        event("model_call_start", model="offline-test", purpose=purpose)
        event("model_call_end", status="completed")
        return deepcopy(self.result)


class ExtractionTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.spec = read_json(ROOT / "tools/openapi.json")
        self.contracts = Contracts(self.spec)
        self.raw = fake_extraction(self.documents, self.b_raw)

    def validate(self, raw=None, contracts=None):
        return validate_extraction(raw or self.raw, self.documents, contracts or self.contracts)

    def test_policy_round_trip_into_existing_core(self):
        result = self.validate()
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["policy"], self.b_raw)
        self.assertEqual(len(result["bindings"]), 7)

    def test_units_account_for_every_nonempty_line(self):
        for document in self.documents:
            expected = {i for i, line in enumerate(document.text.splitlines(), 1) if line.strip()}
            actual = [i for u in source_units([document]) for i in range(u["start_line"], u["end_line"] + 1)]
            self.assertEqual(set(actual), expected)
            self.assertEqual(len(actual), len(set(actual)))

    def test_missing_coverage_or_false_quote_rejected(self):
        for mutate in (lambda r: r["coverage"].pop(),
                       lambda r: r["requirements"][0]["sources"][0].update(quote="fabricated quote"),
                       lambda r: r["coverage"][0].update(requirement_ids=["r99"])):
            raw = deepcopy(self.raw)
            mutate(raw)
            with self.assertRaises(InvalidInput):
                self.validate(raw)

    def test_array_duplicates_and_wrong_api_enum_rejected(self):
        for mutate in (lambda r: r["bindings"].append(r["bindings"][0]),
                       lambda r: r["policy_candidates"][0]["transfer_queues"][0].update(field="invented-queue"),
                       lambda r: r["policy_candidates"][0]["evidence"].append(r["policy_candidates"][0]["evidence"][0])):
            raw = deepcopy(self.raw)
            mutate(raw)
            with self.assertRaises(InvalidInput):
                self.validate(raw)

    def test_cross_document_replacement_needs_precedence_and_cycles_fail(self):
        first = self.raw["requirements"][0]
        later = next(r for r in self.raw["requirements"] if r["sources"][0]["document"] == "document_2")
        first.update(status="superseded", replaced_by=[later["id"]])
        with self.assertRaises(InvalidInput):
            self.validate()
        relation = {"earlier_document": "document_1", "later_document": "document_2",
                    "explanation": "Explicit extension", "sources": [short_reference(self.documents[1], 5)]}
        self.raw["precedence"] = [relation]
        self.assertEqual(self.validate()["status"], "extracted")
        self.raw["precedence"].append({**relation, "earlier_document": "document_2", "later_document": "document_1"})
        with self.assertRaises(InvalidInput):
            self.validate()

    def test_renamed_api_fields_supported_and_stale_bindings_fail(self):
        request = self.spec["paths"]["/create_promise"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        request["properties"]["value_v2"] = request["properties"].pop("amount")
        request["required"] = ["value_v2" if x == "amount" else x for x in request["required"]]
        changed = Contracts(self.spec)
        with self.assertRaises(InvalidInput):
            self.validate(contracts=changed)
        binding = next(x for x in self.raw["bindings"] if x["tool"] == "create_promise")
        next(x for x in binding["request"] if x["role"] == "amount")["field"] = "value_v2"
        self.assertEqual(self.validate(contracts=changed)["bindings"]["create_promise"]["request"]["amount"], "value_v2")

    def test_unknown_required_api_field_and_response_path_fail(self):
        request = self.spec["paths"]["/create_promise"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        request["properties"]["approval_code"] = {"type": "string"}
        request["required"].append("approval_code")
        with self.assertRaises(InvalidInput):
            self.validate(contracts=Contracts(self.spec))
        self.raw["bindings"][0]["response"][0]["field"] = "imaginary.path"
        with self.assertRaises(InvalidInput):
            self.validate()

    def test_blockers_stop_policy_emission(self):
        self.raw["policy_candidates"] = []
        self.raw["bindings"] = []
        self.raw["blockers"] = [{"kind": "unsupported", "message": "Test-only unsupported behavior",
                                 "sources": [short_reference(self.documents[0])]}]
        self.assertEqual(self.validate()["status"], "blocked")
        with tempfile.TemporaryDirectory() as temp:
            result = run_extraction([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"],
                                    ROOT / "tools/openapi.json", Path(temp) / "run", FakeProvider(self.raw))
            self.assertEqual(result["status"], "blocked")
            self.assertFalse((Path(temp) / "run/policy.json").exists())
            self.assertTrue((Path(temp) / "run/blockers.json").is_file())

    def test_policy_with_reported_gaps_is_built_best_effort_and_keeps_the_record(self):
        self.raw["blockers"] = [{"kind": "unsupported", "message": "Test-only unsupported behavior",
                                 "sources": [short_reference(self.documents[0])]}]
        validated = self.validate()
        self.assertEqual(validated["status"], "extracted")
        self.assertEqual(validated["best_effort_gaps"], [{"kind": "unsupported", "message": "Test-only unsupported behavior"}])
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "run"
            result = run_extraction([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"],
                                    ROOT / "tools/openapi.json", output, FakeProvider(self.raw))
            self.assertEqual(result["status"], "extracted")
            self.assertEqual(result["best_effort_gaps"], validated["best_effort_gaps"])
            self.assertTrue((output / "policy.json").is_file())
            self.assertTrue((output / "blockers.json").is_file())
            self.assertIn('"best_effort_gaps"', (output / "trace.jsonl").read_text())

    def test_trace_and_artifacts_are_complete_and_do_not_load_environment_secrets(self):
        provider = FakeProvider(self.raw)
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"FREYA_API_KEY": "NEVER_COPY_BANK_SECRET"}):
            output = Path(temp) / "run"
            result = run_extraction([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"],
                                    ROOT / "tools/openapi.json", output, provider)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(result["model_calls"], 1)
            self.assertFalse(result["semantic_correctness_verified"])
            self.assertEqual(read_json(output / "policy.json"), self.b_raw)
            events = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
            self.assertEqual(events[0]["event"], "run_start")
            self.assertEqual(events[-1]["event"], "run_end")
            for path in output.iterdir():
                self.assertNotIn("NEVER_COPY_BANK_SECRET", path.read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                run_extraction([ROOT / "briefs/brief-a.md"], ROOT / "tools/openapi.json", output, provider)
            self.assertEqual(provider.calls, 1)

    def test_failed_validation_retains_raw_output_and_failed_trace(self):
        self.raw["coverage"].pop()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "run"
            with self.assertRaises(InvalidInput):
                run_extraction([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"],
                               ROOT / "tools/openapi.json", output, FakeProvider(self.raw))
            self.assertEqual(read_json(output / "manifest.json")["status"], "failed")
            self.assertTrue((output / "extraction.json").is_file())
            self.assertFalse((output / "policy.json").exists())

    def test_total_deadline_rejects_late_response_without_emitting_policy(self):
        with tempfile.TemporaryDirectory() as temp, patch("builder.extract_run.time.monotonic", side_effect=[0, 1, 2, 21, 22]):
            output = Path(temp) / "run"
            with self.assertRaises(ProviderError):
                run_extraction([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"],
                               ROOT / "tools/openapi.json", output, FakeProvider(self.raw), timeout=20)
            self.assertEqual(read_json(output / "manifest.json")["status"], "failed")
            self.assertFalse((output / "policy.json").exists())

    def test_prompt_uses_only_explicit_inputs_not_test_answers(self):
        prompt = make_prompt(self.documents, self.contracts)
        self.assertNotIn("PUB-01", prompt)
        self.assertNotIn("practice-b.policy.json", prompt)
        for unit in source_units(self.documents):
            self.assertIn(unit["id"], prompt)

    def test_downstream_capabilities_are_disclosed_without_injecting_policy_values(self):
        from builder.dialogue_plan import COUNTERS, STRATEGIES
        prompt = make_prompt(self.documents, self.contracts)
        metadata = prompt.split("DOWNSTREAM DIALOGUE LAYER:", 1)[1].split("\nDATA:", 1)[0]
        for name in (*COUNTERS, *STRATEGIES):
            self.assertIn(name, metadata)
        self.assertNotIn("2026-10-26", metadata)
        self.assertNotIn("three", metadata.lower())

    def test_coverage_repair_feedback_identifies_the_bad_link(self):
        first, second = self.raw["coverage"][:2]
        first["requirement_ids"] = second["requirement_ids"]
        with self.assertRaises(InvalidInput) as caught:
            self.validate()
        self.assertIn(first["unit"], str(caught.exception))
        self.assertIn(second["requirement_ids"][0], str(caught.exception))

    def test_one_structural_repair_is_automatic_and_traced(self):
        broken = deepcopy(self.raw)
        broken["coverage"].pop()
        provider = FakeProvider(broken)
        original = provider.extract

        def respond(prompt, schema, **kwargs):
            if provider.calls:
                self.assertIn("STRUCTURAL VALIDATION FEEDBACK", prompt)
                provider.result = self.raw
            return original(prompt, schema, **kwargs)

        with tempfile.TemporaryDirectory() as temp, patch.object(provider, "extract", side_effect=respond):
            output = Path(temp) / "run"
            result = run_extraction([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"],
                                    ROOT / "tools/openapi.json", output, provider)
            self.assertEqual(result["status"], "extracted")
            self.assertEqual(result["model_calls"], 2)
            self.assertEqual(result["repairs_used"], 1)
            self.assertTrue((output / "extraction-attempt-1.json").is_file())
            self.assertTrue((output / "extraction-attempt-2.json").is_file())
            self.assertTrue((output / "repair-prompt-1.txt").is_file())


class ProviderTests(OfflineCase):
    def provider(self, folder):
        executable = Path(folder) / "codex.exe"
        executable.touch()
        return CodexProvider("test-model", str(executable), folder)

    def test_child_environment_excludes_paid_keys_and_bank_secrets(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {
                "OPENAI_API_KEY": "paid-secret", "FREYA_API_KEY": "bank-secret", "ANTHROPIC_API_KEY": "other-secret",
                "CODEX_ACCESS_TOKEN": "override-secret", "CUSTOM_TOKEN": "secret"}):
            env = child_environment(temp)
            self.assertEqual(env["CODEX_HOME"], str(Path(temp).resolve()))
            self.assertNotIn("secret", json.dumps(env))

    def test_api_key_auth_refused_without_echoing_key(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = self.provider(temp)
            result = subprocess.CompletedProcess([], 0, "Logged in using API key: sk-SENTINEL", "")
            with patch.object(provider, "_run", return_value=result), self.assertRaises(ProviderError) as caught:
                provider.check_access()
            self.assertNotIn("SENTINEL", str(caught.exception))

    def test_chatgpt_auth_and_version_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = self.provider(temp)
            results = [subprocess.CompletedProcess([], 0, "Logged in using ChatGPT", ""),
                       subprocess.CompletedProcess([], 0, "codex-cli test-version", "")]
            with patch.object(provider, "_run", side_effect=results) as run:
                meta = provider.check_access()
            self.assertEqual(meta["authentication"], "chatgpt")
            self.assertEqual(run.call_args_list[0].args[0], ["login", "status"])

    def test_timeout_is_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = self.provider(temp)
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 1)) as run:
                with self.assertRaises(ProviderError):
                    provider._run(["exec"], timeout=1)
                self.assertEqual(run.call_count, 1)

    def test_native_process_uses_argument_list_and_filtered_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = self.provider(temp)
            with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                provider._run(["login", "status"], timeout=2)
            self.assertIsInstance(run.call_args.args[0], list)
            self.assertFalse(run.call_args.kwargs["shell"])
            self.assertNotIn("OPENAI_API_KEY", run.call_args.kwargs["env"])

    def test_invocation_is_read_only_no_shell_and_no_user_config(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = self.provider(temp)
            provider.metadata = {"model": "test-model", "provider": "codex_cli", "authentication": "chatgpt"}
            events = []

            def fake_run(args, **kwargs):
                self.assertIn("--ignore-user-config", args)
                self.assertIn("read-only", args)
                self.assertIn("features.shell_tool=false", args)
                self.assertIn('forced_login_method="chatgpt"', args)
                self.assertNotIn("danger-full-access", args)
                Path(args[args.index("--output-last-message") + 1]).write_text('{"result":true}', encoding="utf-8")
                return subprocess.CompletedProcess([], 0, '{"type":"turn.completed","usage":{"input_tokens":10}}', "")

            with patch.object(provider, "_run", side_effect=fake_run):
                self.assertEqual(provider.extract("prompt", extraction_schema(), timeout=10,
                                                 event=lambda kind, **fields: events.append((kind, fields))), {"result": True})
            self.assertEqual(events[-1][1]["usage"]["input_tokens"], 10)
