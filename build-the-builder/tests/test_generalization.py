"""API/policy changes must work or fail explicitly before a live call starts."""
from copy import deepcopy

from builder.contracts import Contracts, validate_value
from builder.extraction import validate_extraction
from builder.inputs import InvalidInput, read_json
from builder.local_bank import LocalBank
from builder.workflow import compile_workflow
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import fake_plan, LocalGraph


class GeneralizationContractTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.spec = read_json(ROOT / "tools/openapi.json")

    def request(self, tool):
        return self.spec["paths"]["/" + tool]["post"]["requestBody"]["content"]["application/json"]["schema"]

    def test_required_close_reason_cannot_be_satisfied_by_binding_alone(self):
        self.request("close_call")["required"].append("reason")
        with self.assertRaisesRegex(InvalidInput, "requires a close reason"):
            validate_extraction(fake_extraction(self.documents, self.b_raw), self.documents, Contracts(self.spec))
        # When the input policy supplies a reason for every outcome the same API
        # is usable; these synthetic values do not enter production code.
        raw = deepcopy(self.b_raw)
        for outcome in raw["outcomes"].values():
            outcome.setdefault("reason", "synthetic_completion")
        self.assertEqual(validate_extraction(fake_extraction(self.documents, raw), self.documents,
                                            Contracts(self.spec))["status"], "extracted")

    def test_required_note_text_is_rejected_when_notes_are_enabled(self):
        self.request("add_note")["required"].append("text")
        with self.assertRaisesRegex(InvalidInput, "requires note text"):
            validate_extraction(fake_extraction(self.documents, self.b_raw), self.documents, Contracts(self.spec))
        # An unused note action must not incorrectly block a no-notes policy.
        self.assertEqual(validate_extraction(fake_extraction(self.documents[:1], self.a_raw),
                                            self.documents[:1], Contracts(self.spec))["status"], "extracted")

    def test_integral_number_is_valid_for_integer_api_amount_without_rounding(self):
        for value in (0, 0.0, 360, 360.0):
            validate_value(value, {"type": "integer"})
        for value in (True, False, 360.01, float("inf"), float("nan"), "360"):
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                validate_value(value, {"type": "integer"})

    def test_emitted_workflow_records_integral_amount_in_altered_integer_api(self):
        self.request("create_promise")["properties"]["amount"]["type"] = "integer"
        contracts = Contracts(self.spec)
        workflow, report = compile_workflow(fake_extraction(self.documents[:1], self.a_raw),
            fake_plan(self.documents[:1], False, contracts), self.documents[:1], contracts)
        bank = LocalBank({"date_of_birth": "1981-09-14", "phone_last4": "7358"},
            [{"product_id": "arbitrary-integer-product", "type": "personal_loan", "past_due": 480,
              "instalment_amount": 120, "days_late": 9}], wrong_answer_limit=3)
        graph = LocalGraph(workflow, bank, {"now": "2029-03-07T12:00"}, report["runtime_payload"]["bindings"])
        graph.user("person_yes")
        graph.user("identity_answer", answer="1981-09-14", answer_complete=True)
        graph.user("identity_answer", answer="7358", answer_complete=True)
        graph.user("accept_offer")
        self.assertEqual(bank.ledger["promises"], [])
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        self.assertEqual(bank.ledger["promises"], [{"product_id": "arbitrary-integer-product", "amount": 480,
                                                   "date": "2029-03-07"}])
        self.assertEqual(bank.ledger["outcome"], "promise")

    def test_spoken_year_and_digit_code_identity_answers_reach_the_bank_normalized(self):
        # The kit's persona builder already knows birth_year and postcode
        # identity facts; here they replace date of birth and phone digits.
        self.request("verify_identity")["properties"]["field"]["enum"] = ["birth_year", "postcode"]
        contracts = Contracts(self.spec)
        raw = deepcopy(self.a_raw)
        raw["identity_fields"] = ["birth_year", "postcode"]
        plan = fake_plan(self.documents[:1], False, contracts)
        plan["plans"][0]["numeric_date_order"] = "not_applicable"
        plan["plans"][0]["verification_questions"] = [
            {"field": "birth_year", "question": "What is your year of birth?", "normalization": "year"},
            {"field": "postcode", "question": "What is your postcode?", "normalization": "digits"}]
        workflow, report = compile_workflow(fake_extraction(self.documents[:1], raw), plan, self.documents[:1], contracts)
        bank = LocalBank({"birth_year": "1988", "postcode": "04109"},
            [{"product_id": "p1", "type": "personal_loan", "past_due": 480, "instalment_amount": 120, "days_late": 9}],
            wrong_answer_limit=3)
        graph = LocalGraph(workflow, bank, {"now": "2029-03-07T12:00"}, report["runtime_payload"]["bindings"])
        graph.user("person_yes")
        graph.user("identity_answer", answer="nineteen", answer_complete=True)
        self.assertEqual(bank.ledger["calls"], [])
        graph.user("identity_answer", answer="nineteen eighty-seven", answer_complete=True)
        graph.user("identity_answer", answer="one nine eight eight", answer_complete=True)
        graph.user("identity_answer", answer="zero four one zero nine", answer_complete=True)
        self.assertEqual([(c["tool"], c["args"].get("answer")) for c in bank.ledger["calls"]],
                         [("verify_identity", "1987"), ("verify_identity", "1988"),
                          ("verify_identity", "04109"), ("get_account", None)])
        self.assertTrue(graph.state["verified"])

    def test_wire_floor_and_suffix_survive_collisions_with_internal_role_names(self):
        schema = self.spec["paths"]["/get_account"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["products"]["items"]
        props = schema["properties"]
        for old, new in (("past_due", "arrears"), ("minimum_payment", "past_due"),
                         ("product_id", "reference"), ("card_last4", "product_id")):
            props[new] = props.pop(old)
        schema["required"] = [{"past_due": "arrears", "product_id": "reference"}.get(k, k)
                              for k in schema.get("required", [])]
        raw = deepcopy(self.a_raw)
        raw["floor_fields"]["credit_card"] = "past_due"
        extraction = fake_extraction(self.documents[:1], raw)
        for binding in extraction["bindings"]:
            if binding["tool"] == "get_account":
                for response in binding["response"]:
                    if response["role"] in {"past_due", "product_id"}:
                        response["field"] = "products[]." + {"past_due": "arrears", "product_id": "reference"}[response["role"]]
        contracts = Contracts(self.spec)
        plan = fake_plan(self.documents[:1], False, contracts)
        next(p for p in plan["plans"][0]["product_labels"] if p["type"] == "credit_card")["suffix_field"] = "product_id"
        workflow, report = compile_workflow(extraction, plan, self.documents[:1], contracts)
        bank = LocalBank({"date_of_birth": "1981-09-14", "phone_last4": "7358"},
            [{"product_id": "authoritative-id-Z", "card_last4": "5029", "type": "credit_card",
              "past_due": 360, "minimum_payment": 60, "days_late": 9}], wrong_answer_limit=3)

        def adapt(tool, envelope):
            if tool == "get_account":
                for item in envelope["data"]["products"]:
                    item["arrears"] = item.pop("past_due")
                    item["past_due"] = item.pop("minimum_payment")
                    item["reference"] = item.pop("product_id")
                    item["product_id"] = item.pop("card_last4")
                    # An identically named raw API field cannot replace the
                    # runtime-owned wrapper with fake floor/display values.
                    item["_fb_wire"] = {"past_due": 999, "product_id": "WRONG"}
            return envelope

        graph = LocalGraph(workflow, bank, {"now": "2029-03-07T12:00"},
                           report["runtime_payload"]["bindings"], adapt)
        graph.user("person_yes")
        graph.user("identity_answer", answer="1981-09-14", answer_complete=True)
        self.assertNotIn("5029", graph.vars["fb_speech"])
        graph.user("identity_answer", answer="7358", answer_complete=True)
        self.assertEqual(graph.state["products"][0]["past_due"], "360.00")
        self.assertEqual(graph.state["products"][0]["floor"], "60.00")
        self.assertIn("ending 5029", graph.vars["fb_speech"])
        self.assertNotIn("authoritative-id-Z", graph.vars["fb_speech"])
        graph.user("payment", amount_kind="floor", day={"kind": "today"})
        self.assertIn("60.00", graph.vars["fb_speech"])
        self.assertEqual(bank.ledger["promises"], [])
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        self.assertEqual(bank.ledger["promises"], [{"product_id": "authoritative-id-Z", "amount": 60,
                                                   "date": "2029-03-07"}])
