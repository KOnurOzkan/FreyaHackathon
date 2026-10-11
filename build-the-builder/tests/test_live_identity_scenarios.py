"""Evaluation-only identity personas over unchanged public bank profiles.

These semantic replays verify the scenario oracles. They do not execute the
Freya speech, extraction, routing models, or the official bank service.
"""
from copy import deepcopy
from datetime import date

from builder.contracts import Contracts
from builder.inputs import InvalidInput, read_json
from builder.local_bank import LocalBank, audit_ledger
from builder.workflow import compile_workflow
from tools.run_cases import persona_for
from tools.scenario_inputs import bank_case_ids, load_case_inputs, validate_cases
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import LocalGraph, fake_plan


FIXTURE = ROOT / "tests/fixtures/live-identity-scenarios.json"
CUMULATIVE = "DEV-A-IDENTITY-CUMULATIVE-THREE"
SWAPPED = "DEV-A-IDENTITY-SWAPPED-NONCOUNTING"


class LiveIdentityScenarioTests(OfflineCase):
    def cases_by_id(self):
        return {case["id"]: case for case in load_case_inputs(FIXTURE)["cases"]}

    def graph(self, case, *, swap_hint=None):
        documents = self.documents[:1]
        contracts = Contracts(read_json(ROOT / "tools/openapi.json"))
        plan = fake_plan(documents, False, contracts)
        workflow, report = compile_workflow(fake_extraction(documents, self.a_raw), plan, documents, contracts)
        bank = LocalBank.from_case(case, self.a, swap_hint=swap_hint)
        return LocalGraph(workflow, bank, case["variables"], report["runtime_payload"]["bindings"]), bank

    def assert_private(self, graph, bank):
        self.assertFalse(graph.state.get("verified"))
        self.assertNotIn("products", graph.state)
        self.assertNotIn("get_account", [row["tool"] for row in bank.ledger["calls"]])
        for forbidden in ("collections", "past due", "credit card", "personal loan", "car loan"):
            self.assertNotIn(forbidden, graph.vars["fb_speech"].lower())

    def test_two_scenarios_preserve_bank_profiles_and_expected_outcomes(self):
        snapshot = load_case_inputs(FIXTURE)
        self.assertTrue(snapshot["evaluation_only"])
        self.assertEqual([case["id"] for case in snapshot["cases"]], [CUMULATIVE, SWAPPED])
        self.assertEqual(bank_case_ids(snapshot["cases"]), ["PUB-03", "PUB-07"])
        originals = {case["id"]: case for case in self.cases}
        for case in snapshot["cases"]:
            with self.subTest(case=case["id"]):
                original = originals[case["bank_case_id"]]
                for key in ("brief", "variables", "identity", "products", "faults"):
                    self.assertEqual(case.get(key), original.get(key))
                self.assertNotIn("variable_overrides", case)
                self.assertFalse(case.get("third_party", False))
                for key in ("promises", "notes", "callbacks", "transfers"):
                    self.assertEqual(case["expected"][key], [])
        self.assertEqual(snapshot["cases"][0]["expected"]["outcome"], "not_verified")
        self.assertEqual(snapshot["cases"][0]["expected"]["reason"], "failed")
        self.assertEqual(snapshot["cases"][1]["expected"]["outcome"], "paid")
        self.assertIsNone(snapshot["cases"][1]["expected"]["reason"])
        selected = load_case_inputs(FIXTURE, [SWAPPED, CUMULATIVE])["cases"]
        self.assertEqual([case["id"] for case in selected], [SWAPPED, CUMULATIVE])

    def test_selected_swapped_birth_date_is_a_real_distinct_valid_date(self):
        case = self.cases_by_id()[SWAPPED]
        actual = date.fromisoformat(case["identity"]["date_of_birth"])
        self.assertLessEqual(actual.day, 12)
        self.assertNotEqual(actual.day, actual.month)
        swapped = date(actual.year, actual.day, actual.month)
        self.assertNotEqual(actual, swapped)
        self.assertEqual(swapped.isoformat(), "1975-05-12")
        self.assertIn("12/05/1975", case["script"])
        # The test double models the documented service hint; no fixture fault,
        # identity edit, or unsupported synthetic session is needed to reach it.
        bank = LocalBank.from_case(case, self.a, swap_hint="day_month_swapped")
        result = bank.call("verify_identity", {"field": "date_of_birth", "answer": swapped.isoformat()})
        self.assertIs(result["verified"], False)
        self.assertEqual(result["hint"], "day_month_swapped")
        self.assertEqual(result["wrong_answers_left"], 3)

    def test_persona_separates_scripted_caller_answers_from_judge_rules(self):
        for case in self.cases_by_id().values():
            with self.subTest(case=case["id"]):
                persona = persona_for(case, {"identity": case["identity"]})
                self.assertIn(case["script"], persona["prompt"])
                self.assertIn("When the caller says goodbye", case["script"])
                self.assertIn("Then say nothing more and let the call end", case["script"])
                for term in ("verify_identity", "get_account", "wrong_answers_left", "day_month_swapped", "close_call"):
                    self.assertNotIn(term, persona["prompt"])
                for check in case["checks"]:
                    self.assertIn(check, persona["successCriteria"])
                self.assertIn("exactly one close_call", persona["successCriteria"])
                self.assertIn("last bank tool call", persona["successCriteria"])

    def test_profile_or_fault_mutations_are_rejected(self):
        original = self.cases_by_id()[SWAPPED]
        changed_identity = deepcopy(original)
        changed_identity["identity"]["date_of_birth"] = "1975-05-12"
        changed_faults = deepcopy(original)
        changed_faults["faults"] = [{"tool": "verify_identity", "fail_first_n": 1}]
        for changed in (changed_identity, changed_faults):
            with self.subTest(changed=changed), self.assertRaises(InvalidInput):
                validate_cases([changed], self.cases)

    def test_third_wrong_answer_across_both_fields_closes_without_account(self):
        case = self.cases_by_id()[CUMULATIVE]
        graph, bank = self.graph(case)
        graph.user("person_yes")
        for answer, field, remaining in (
            ("twelve July nineteen ninety-one", "date_of_birth", 2),
            ("twenty-one July nineteen ninety-two", "date_of_birth", 2),
            ("six six zero three", "phone_last4", 1),
            ("six six zero four", "phone_last4", 0),
        ):
            graph.user("identity_answer", answer=answer, answer_complete=True)
            self.assertEqual(bank.ledger["calls"][-1]["args"]["field"], field)
            self.assertEqual(graph.state["wrong_answers_left"], remaining)
            self.assert_private(graph, bank)
        self.assertEqual([row["args"]["answer"] for row in bank.ledger["calls"]],
                         ["1991-07-12", "1992-07-21", "6603", "6604"])
        self.assertEqual(graph.node, "speak_farewell")
        self.assertNotIn("?", graph.vars["fb_speech"])
        self.assertIn("mobile app", graph.vars["fb_speech"])
        self.assertIn("branch", graph.vars["fb_speech"])
        graph.finish()
        self.assertEqual([row["tool"] for row in bank.ledger["calls"]], ["verify_identity"] * 4 + ["close_call"])
        scored = audit_ledger(case, bank.ledger)
        self.assertTrue(scored["pass"], scored["failed"])

    def test_swap_hint_does_not_use_the_attempt_needed_for_later_phone_success(self):
        case = self.cases_by_id()[SWAPPED]
        graph, bank = self.graph(case, swap_hint="day_month_swapped")
        graph.user("person_yes")
        graph.user("identity_answer", answer="12/05/1975", answer_complete=True)
        self.assertEqual(bank.ledger["calls"][-1]["args"]["answer"], "1975-05-12")
        self.assertEqual(bank.ledger["calls"][-1]["result"]["hint"], "day_month_swapped")
        self.assertEqual(graph.state["wrong_answers_left"], 3)
        self.assertEqual(graph.node, "speak_identity")
        self.assertIn("month as a word", graph.vars["fb_speech"])
        self.assert_private(graph, bank)
        for answer, remaining in (("fifth of December nineteen seventy-five", 3),
                                  ("seven zero nine two", 2), ("seven zero eight three", 1)):
            graph.user("identity_answer", answer=answer, answer_complete=True)
            self.assertEqual(graph.state["wrong_answers_left"], remaining)
            self.assert_private(graph, bank)
        graph.user("identity_answer", answer="seven zero nine three", answer_complete=True)
        self.assertTrue(graph.state["verified"])
        self.assertEqual(graph.state["wrong_answers_left"], 1)
        self.assertEqual(graph.node, "speak_negotiate")
        calls = bank.ledger["calls"]
        self.assertEqual([row["tool"] for row in calls], ["verify_identity"] * 5 + ["get_account"])
        self.assertEqual([row["args"]["field"] for row in calls[:5]], ["date_of_birth"] * 2 + ["phone_last4"] * 3)
        self.assertEqual([row["result"]["wrong_answers_left"] for row in calls[:5]], [3, 3, 2, 1, 1])
        graph.user("paid")
        self.assertIn("one business day", graph.vars["fb_speech"])
        graph.finish()
        self.assertEqual([row["tool"] for row in bank.ledger["calls"]],
                         ["verify_identity"] * 5 + ["get_account", "close_call"])
        scored = audit_ledger(case, bank.ledger)
        self.assertTrue(scored["pass"], scored["failed"])
