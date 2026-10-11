"""Validate development-only strict personas and their offline ledger oracles.

Semantic replays validate attainable expected outcomes, not Freya/model behavior.
The fixture is never used as policy input to the builder.
"""
from copy import deepcopy

from builder.contracts import Contracts
from builder.inputs import InvalidInput, read_json
from builder.local_bank import LocalBank, audit_ledger
from builder.workflow import compile_workflow
from tools.run_cases import persona_for
from tools.scenario_inputs import bank_case_ids, load_case_inputs, validate_cases
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import LocalGraph, fake_plan


FIXTURE = ROOT / "tests/fixtures/live-strict-scenarios.json"
EXPECTED = {
    "DEV-A-STRICT-WRONG-NUMBER": ("not_reached", "wrong_number"),
    "DEV-A-STRICT-DECEASED": ("not_reached", "deceased"),
    "DEV-A-STRICT-UNCLEAR-PERSON": ("not_reached", "unclear"),
    "DEV-A-STRICT-PRIVACY-DECLINED": ("not_verified", "declined"),
    "DEV-A-STRICT-HUMAN-BEFORE-VERIFY": ("transferred", "human"),
}


class LiveStrictScenarioTests(OfflineCase):
    def test_five_distinct_personas_keep_organizer_bank_data_and_complete_oracles(self):
        snapshot = load_case_inputs(FIXTURE)
        self.assertTrue(snapshot["evaluation_only"])
        self.assertEqual({case["id"] for case in snapshot["cases"]}, set(EXPECTED))
        self.assertEqual(bank_case_ids(snapshot["cases"]), ["PUB-01"])
        originals = {case["id"]: case for case in self.cases}
        for case in snapshot["cases"]:
            with self.subTest(case=case["id"]):
                original = originals[case["bank_case_id"]]
                for key in ("brief", "variables", "identity", "products", "faults"):
                    self.assertEqual(case.get(key), original.get(key))
                self.assertEqual((case["expected"]["outcome"], case["expected"]["reason"]), EXPECTED[case["id"]])
                for key in ("promises", "notes", "callbacks"):
                    self.assertEqual(case["expected"][key], [])
                self.assertEqual(case["expected"]["transfers"],
                                 [{"queue": "general"}] if EXPECTED[case["id"]][1] == "human" else [])
                self.assertNotIn("variable_overrides", case)
        self.assertEqual([case["id"] for case in load_case_inputs(FIXTURE, list(EXPECTED)[::-1])["cases"]],
                         list(EXPECTED)[::-1])
        with self.assertRaises(InvalidInput):
            load_case_inputs(FIXTURE, ["PUB-01"])

    def test_wrong_number_and_deceased_personas_do_not_receive_customer_security_answers(self):
        cases = load_case_inputs(FIXTURE)["cases"]
        for case in cases:
            with self.subTest(case=case["id"]):
                persona = persona_for(case, {"identity": case["identity"]})
                if EXPECTED[case["id"]][1] in {"wrong_number", "deceased"}:
                    self.assertIs(case["third_party"], True)
                    self.assertIn("You are NOT Daniel Reyes", persona["prompt"])
                    for answer in case["identity"].values():
                        self.assertNotIn(answer, persona["prompt"])
                self.assertIn("When the caller says goodbye", case["script"])
                self.assertIn("Then say nothing more and let the call end", case["script"])
                for check in case["checks"]:
                    self.assertIn(check, persona["successCriteria"])
                self.assertIn("exactly one close_call", persona["successCriteria"])
                self.assertIn("last bank tool call", persona["successCriteria"])

    def test_strict_fixture_validation_rejects_bank_identity_or_product_changes(self):
        case = load_case_inputs(FIXTURE)["cases"][0]
        changed_identity = deepcopy(case)
        changed_identity["identity"]["phone_last4"] = "7316"
        changed_product = deepcopy(case)
        changed_product["products"][0]["past_due"] = 719.38
        for changed in (changed_identity, changed_product):
            with self.subTest(changed=changed), self.assertRaises(InvalidInput):
                validate_cases([changed], self.cases)

    def test_strict_semantic_branches_match_oracles_without_account_or_identity_tool_calls(self):
        documents = self.documents[:1]
        contracts = Contracts(read_json(ROOT / "tools/openapi.json"))
        plan = fake_plan(documents, False, contracts)
        workflow, report = compile_workflow(fake_extraction(documents, self.a_raw), plan, documents, contracts)
        events = {
            "wrong_number": [("wrong_number", {})],
            "deceased": [("deceased", {})],
            "unclear": [("person_unclear", {}), ("person_unclear", {})],
            "declined": [("person_yes", {}), ("question", {"question_kind": "purpose"}),
                         ("question", {"question_kind": "purpose"})],
            "human": [("human", {}), ("human", {})],
        }
        for case in load_case_inputs(FIXTURE)["cases"]:
            with self.subTest(case=case["id"]):
                bank = LocalBank.from_case(case, self.a)
                graph = LocalGraph(workflow, bank, case["variables"], report["runtime_payload"]["bindings"])
                speeches, nodes = [], []
                reason = case["expected"]["reason"]
                for action, fields in events[reason]:
                    graph.user(action, **fields)
                    speeches.append(graph.vars["fb_speech"])
                    nodes.append(graph.node)
                self.assertEqual(graph.node, "speak_farewell")
                self.assertFalse(graph.state["verified"])
                self.assertIn("Thank you", speeches[-1])
                self.assertIn("Goodbye", speeches[-1])
                if reason == "deceased":
                    self.assertIn("I am sorry for your loss.", speeches[-1])
                    self.assertNotIn("?", speeches[-1])
                elif reason == "unclear":
                    self.assertEqual(nodes, ["speak_person", "speak_farewell"])
                elif reason == "declined":
                    privacy = plan["plans"][0]["messages"]["privacy_explanation"]
                    self.assertEqual(sum(speech.count(privacy) for speech in speeches), 1)
                    self.assertEqual(nodes, ["speak_identity", "speak_identity", "speak_farewell"])
                elif reason == "human":
                    self.assertEqual(nodes, ["speak_human_help", "speak_farewell"])
                graph.finish()
                scored = audit_ledger(case, bank.ledger)
                self.assertTrue(scored["pass"], scored["failed"])
                self.assertEqual([call["tool"] for call in bank.ledger["calls"]],
                                 ["transfer_to_agent", "close_call"] if reason == "human" else ["close_call"])
