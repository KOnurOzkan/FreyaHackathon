from copy import deepcopy
import json
import re

from builder.contracts import Contracts
from builder.dialogue_plan import validate_plan
from builder.extraction import validate_extraction
from builder.inputs import InvalidInput, read_json
from builder.local_bank import LocalBank, audit_ledger
from builder.workflow import compile_workflow, validate_workflow_local
from builder.compile_run import local_runtime_checks, run_compilation, run_build
from builder.extract_run import write_json
from builder.codex_provider import ProviderError
from unittest.mock import patch
from pathlib import Path
import tempfile
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction, FakeProvider
from tests.workflow_support import fake_plan, LocalGraph


class WorkflowTests(OfflineCase):
    @classmethod
    def setUpClass(cls):
        cls.compiled = {}

    def setUp(self):
        super().setUp()
        self.contracts = Contracts(read_json(ROOT / "tools/openapi.json"))
        for label, docs, raw in (("A", self.documents[:1], self.a_raw), ("B", self.documents, self.b_raw)):
            if label not in self.compiled:
                extraction = fake_extraction(docs, raw)
                plan = fake_plan(docs, label == "B", self.contracts)
                self.compiled[label] = compile_workflow(extraction, plan, docs, self.contracts)

    def graph(self, number, verified=True, response_adapter=None, **options):
        case, _, bank = self.setup_case(number, verified=False, **options)
        workflow, report = self.compiled[case["brief"]]
        graph = LocalGraph(workflow, bank, case["variables"], report["runtime_payload"]["bindings"], response_adapter)
        if verified:
            graph.user("person_yes")
            for value in case["identity"].values():
                graph.user("identity_answer", answer=value, answer_complete=True)
            self.assertIn(graph.node, {"speak_negotiate", "speak_all_today"}, graph.vars.get("fb_instruction"))
        return case, graph, bank

    def assert_pass(self, case, graph, bank):
        graph.finish()
        result = audit_ledger(case, bank.ledger)
        self.assertTrue(result["pass"], result["failed"])
        calls = [entry["tool"] for entry in bank.ledger["calls"]]
        self.assertEqual(calls[-1], "close_call")
        self.assertEqual(calls.count("close_call"), 1)
        goodbye = next(i for i, e in enumerate(graph.timeline) if e.get("speech_completion") == "speak_farewell")
        closing = next(i for i, e in enumerate(graph.timeline) if e.get("tool") == "close_call")
        self.assertLess(goodbye, closing)

    def confirm(self, graph):
        self.assertEqual(graph.node, "speak_confirm", graph.vars.get("fb_instruction"))
        graph.user("confirm", explicit_yes=True)
        self.assertEqual(graph.node, "speak_progress", graph.vars.get("fb_instruction"))

    def test_generated_code_runs_all_public_ledger_scenarios(self):
        for number in getattr(self, "case_numbers", range(1, 14)):
            with self.subTest(case=number):
                case, g, bank = self.graph(number, verified=number not in {2, 3})
                if number == 1:
                    g.user("accept_offer")
                elif number == 10:
                    g.user("payment", amount_kind="full", day={"kind": "iso", "value": "2026-10-16"})
                elif number == 2:
                    g.user("other_person")
                    g.user("callback", day={"kind": "tomorrow"}, time="11:00")
                elif number == 3:
                    g.user("person_yes")
                    for value in ("1991-07-12", "1992-08-21", "1990-07-21"):
                        g.user("identity_answer", answer=value, answer_complete=True)
                elif number == 4:
                    deadline = g.state["products"][0]["deadline"]
                    g.user("payment", amount_kind="full", day={"kind": "iso", "value": deadline})
                    self.assertEqual(g.state["products"][0]["attempts_used"], 0)
                elif number == 5:
                    g.user("hardship")
                    g.user("support_no")
                elif number == 6:
                    g.user("too_much")
                    g.user("payment", amount_kind="floor", day={"kind": "iso", "value": "2026-10-16"})
                elif number == 7:
                    g.user("payment", amount_kind="full", day={"kind": "iso", "value": "2026-10-26"})
                    self.assertEqual(g.node, "speak_negotiate")
                    self.assertEqual(g.state["offers"]["p1"]["date"], "2026-10-27")
                    g.user("accept_offer")
                elif number == 8:
                    g.user("paid")
                elif number == 9:
                    g.user("restructure")
                elif number == 11:
                    g.user("payment", scope="all", amount_kind="full", day={"kind": "iso", "value": "2026-10-16"})
                elif number == 12:
                    g.user("payment", product_id="p1", amount_kind="full", day={"kind": "tomorrow"})
                    self.assertEqual(g.state["products"][1]["attempts_used"], 1)
                    g.user("refuse_payment")
                    g.user("refuse_payment")
                    self.assertEqual(g.state["current_product"], "p3")
                    g.user("refuse_payment")
                    self.assertEqual(bank.ledger["notes"], [])
                elif number == 13:
                    g.user("payment", scope="all", amount_kind="full", day={"kind": "iso", "value": "2026-10-30"})
                    self.assertEqual([p["date"] for p in g.state["promises_to_confirm"]], ["2026-10-30", "2026-10-23"])
                if g.node == "speak_confirm":
                    self.assertEqual(bank.ledger["promises"], [])
                    self.confirm(g)
                self.assert_pass(case, g, bank)

    def test_privacy_fragments_corrections_and_swapped_date_hint(self):
        case, g, bank = self.graph(13, verified=False, swap_hint="day_month_swapped")
        g.user("identity_answer", answer=case["identity"]["date_of_birth"], answer_complete=True)
        self.assertEqual(bank.ledger["calls"], [])
        self.assertNotIn("products", g.state)
        g.user("person_yes")
        g.user("identity_answer", answer="08/01", answer_complete=False)
        self.assertEqual(bank.ledger["calls"], [])
        g.user("identity_answer", answer="01/08/1991", answer_complete=True)
        self.assertEqual(g.state["wrong_answers_left"], 3)
        self.assertIn("day_month_swapped", g.state["instruction"])
        g.user("identity_answer", answer="1991-01-08", answer_complete=True)
        g.user("identity_answer", answer="6604", answer_complete=True)
        self.assertTrue(g.state["verified"])
        self.assertEqual([c["tool"] for c in bank.ledger["calls"]].count("get_account"), 1)

    def test_security_answer_pieces_are_joined_without_counting_a_wrong_answer(self):
        # Live A059 ADV-A-FRAGMENT-LEADING-ZERO: "Second of November" arrived as
        # a complete answer and "nineteen seventy-nine" alone, so the agent asked
        # twice instead of joining them.
        scenarios = [
            ([("Fourteenth of March", True), ("1988", False)], "1988-03-14"),
            ([("1988", False), ("fourteenth of March", True)], "1988-03-14"),
            ([("14/03", False), ("1988", False)], "1988-03-14"),
            ([("fourteenth of March", True), ("nineteen eighty eight", True)], "1988-03-14"),
        ]
        for pieces, expected in scenarios:
            with self.subTest(pieces=pieces):
                _, g, bank = self.graph(1, verified=False)
                g.user("person_yes")
                for answer, complete in pieces[:-1]:
                    g.user("identity_answer", answer=answer, answer_complete=complete)
                    self.assertEqual(bank.ledger["calls"], [])
                    self.assertEqual(g.node, "speak_identity")
                answer, complete = pieces[-1]
                g.user("identity_answer", answer=answer, answer_complete=complete)
                self.assertEqual([(c["tool"], c["args"]["answer"]) for c in bank.ledger["calls"]],
                                 [("verify_identity", expected)])
                self.assertEqual(g.state["wrong_answers_left"], 3)
        _, g, bank = self.graph(1, verified=False)
        g.user("person_yes")
        g.user("identity_answer", answer="1988-03-14", answer_complete=True)
        g.user("identity_answer", answer="four four", answer_complete=False)
        g.user("identity_answer", answer="seven one", answer_complete=True)
        self.assertEqual(bank.ledger["calls"][-2]["args"]["answer"], "4471")
        self.assertTrue(g.state["verified"])

    def test_a_held_piece_is_dropped_once_an_answer_is_submitted(self):
        _, g, bank = self.graph(1, verified=False)
        g.user("person_yes")
        g.user("identity_answer", answer="Fourteenth of March", answer_complete=True)
        g.user("identity_answer", answer="1987-03-14", answer_complete=True)   # a wrong complete answer
        self.assertEqual(g.state["wrong_answers_left"], 2)
        g.user("identity_answer", answer="1988", answer_complete=False)
        self.assertEqual(len(bank.ledger["calls"]), 1)   # "1988" alone is not joined with the dropped piece

    def test_live_month_name_answer_reaches_bank_and_consumed_event_passes_schema(self):
        _, g, bank = self.graph(11, verified=False)
        g.user("person_yes")
        self.assertEqual(g.state["current_security_question"]["field"], "date_of_birth")
        g.user("identity_answer", answer="3 June 1987", answer_complete=True)
        self.assertEqual(bank.ledger["calls"][0]["tool"], "verify_identity")
        self.assertEqual(g.state["current_security_question"]["field"], "phone_last4")
        self.assertIsNotNone(re.fullmatch(g.workflow["nodes"]["speak_identity"]["extraction_schema"][0]["validation_pattern"], g.vars["fb_event"]))
        self.assertEqual(json.loads(g.vars["fb_event"]), {"action": "unparsed"})
        before = len(bank.ledger["calls"])
        g.advance("user_turn")
        self.assertEqual(len(bank.ledger["calls"]), before)
        g.user("identity_answer", answer="2288", answer_complete=True)
        self.assertEqual(g.node, "speak_all_today")
        self.assertEqual(len(g.state["products"]), 3)

    def test_month_names_do_not_guess_missing_or_invalid_date_parts(self):
        from builder.workflow_runtime import normalize_birth_date
        for answer in ("June 3rd, 1987", "3 Jun 1987", "3rd June 1987"):
            self.assertEqual(normalize_birth_date(answer, "day_month_year"), "1987-06-03")
        for answer in ("June", "3 June", "31 February 1987", "3 Unknown 1987"):
            with self.assertRaises(InvalidInput):
                normalize_birth_date(answer, "day_month_year")

    def test_literal_this_next_weekday_overrides_inconsistent_model_hint(self):
        from datetime import datetime
        from builder.workflow_runtime import payment_day
        from builder.rules import Calendar
        now = datetime(2026, 10, 14, 11)
        calendar = Calendar(self.b)
        self.assertEqual(payment_day({'kind':'next_weekday', 'weekday':4, 'source_text':'this Friday'}, now, calendar).isoformat(), '2026-10-16')
        self.assertEqual(payment_day({'kind':'this_weekday', 'weekday':4, 'source_text':'next Friday'}, now, calendar).isoformat(), '2026-10-23')
        self.assertEqual(payment_day({'kind':'iso', 'value':'2026-10-23', 'source_text':'this Friday'}, now, calendar).isoformat(), '2026-10-16')
        with self.assertRaises(InvalidInput):
            payment_day({'kind':'next_weekday', 'weekday':4}, now, calendar)

    def test_unclear_readback_and_correction_never_record_old_terms(self):
        _, g, bank = self.graph(11)
        g.user("accept_offer")
        g.user("confirm", explicit_yes=False)
        self.assertEqual(bank.ledger["promises"], [])
        g.user("correction", product_id="p1", amount_kind="floor", day={"kind": "iso", "value": "2026-10-19"})
        self.assertEqual(bank.ledger["promises"], [])
        self.confirm(g)
        g.finish()
        self.assertEqual(bank.ledger["promises"][0]["amount"], 66.0)
        self.assertEqual(bank.ledger["promises"][0]["date"], "2026-10-19")

    def test_transfer_after_one_write_preserves_it_and_never_adds_notes(self):
        _, g, bank = self.graph(12)
        g.user("payment", product_id="p1", amount_kind="full", day={"kind": "tomorrow"})
        g.user("dispute", product_id="p2")
        g.user("payment", product_id="p3", amount_kind="full", day={"kind": "tomorrow"})
        self.confirm(g)
        self.assertEqual(len(bank.ledger["promises"]), 1)
        g.user("human")
        g.user("human")
        g.finish()
        self.assertEqual(len(bank.ledger["promises"]), 1)
        self.assertEqual(bank.ledger["notes"], [])
        self.assertEqual(bank.ledger["outcome"], "transferred")

    def test_two_documented_failures_keep_successful_other_product(self):
        _, g, bank = self.graph(11)
        bank.faults = [{"tool": "create_promise", "product_id": "p2", "fail_first_n": 2}]
        bank.fault_hits = [0]
        g.user("accept_offer")
        self.confirm(g)
        g.finish()
        self.assertEqual([p["product_id"] for p in bank.ledger["promises"]], ["p1", "p3"])
        self.assertEqual(bank.ledger["notes"], [{"product_id": "p2", "reason": "service_error"}])
        self.assertEqual(bank.ledger["outcome"], "promise")
        self.assertEqual(sum(c["tool"] == "create_promise" for c in bank.ledger["calls"]), 4)

    def test_unknown_transport_result_stops_without_retry_or_claimed_success(self):
        def timeout(name, response):
            return {"status_code": 504, "data": "unknown"} if name == "create_promise" else response
        _, g, bank = self.graph(1, response_adapter=timeout)
        g.user("accept_offer")
        g.user("confirm", explicit_yes=True)
        self.assertEqual(g.node, "speak_abort")
        self.assertEqual(sum(c["tool"] == "create_promise" for c in bank.ledger["calls"]), 1)
        self.assertEqual(g.state.get("recorded_products"), [])
        # This is explicitly an incomplete call, not a compliant passing outcome.
        self.assertIsNone(bank.ledger["outcome"])

    def test_model_cannot_finalize_and_missing_extraction_does_not_repeat_answer(self):
        _, g, bank = self.graph(1, verified=False)
        g.user("person_yes")
        g.user("identity_answer", answer="1988-03-14", answer_complete=True)
        count = len(bank.ledger["calls"])
        g.advance("user_turn")
        self.assertEqual(len(bank.ledger["calls"]), count)
        g.user("finalize")
        self.assertEqual(g.node, "speak_abort")
        self.assertIsNone(bank.ledger["outcome"])

    def test_callback_one_correction_and_privacy_refusal_close_cleanly(self):
        _, g, bank = self.graph(2, verified=False)
        g.user("other_person")
        g.user("callback", day={"kind": "today"}, time="09:00")
        self.assertEqual(g.node, "speak_callback")
        g.user("callback", day={"kind": "tomorrow"}, time="20:00")
        g.finish()
        self.assertEqual(bank.ledger["reason"], "no_callback")
        _, g, bank = self.graph(1, verified=False)
        g.user("person_yes")
        g.user("privacy_refusal")
        self.assertEqual(bank.ledger["calls"], [])
        g.user("privacy_refusal")
        g.finish()
        self.assertEqual(bank.ledger["reason"], "declined")

    def test_compiler_keeps_runtime_model_and_isolates_bank_tools(self):
        workflow, report = self.compiled["B"]
        self.assertNotIn("agent_config", workflow)
        self.assertEqual(report["validation"]["bank_tools"], 7)
        for node in workflow["nodes"].values():
            if node["node_type"] == "conversation":
                self.assertEqual(node["tools"], [])
                self.assertNotIn("pre_actions", node)
        bad = deepcopy(workflow)
        bad["nodes"]["opening"]["pre_actions"] = [{}]
        with self.assertRaises(InvalidInput):
            validate_workflow_local(bad, self.contracts)

    def test_opening_fallback_personalizes_without_relying_on_fixed_entry_speech(self):
        workflow, report = self.compiled["B"]
        opening = workflow["nodes"]["opening"]
        # The chat route can generate dialogue instead of playing on_enter TTS.
        for title, surname in (("Ms", "Almeida"), ("Dr", "Sato")):
            rendered = opening["task_message"].replace("{{title}}", title).replace("{{last_name}}", surname)
            expected = report["runtime_payload"]["plan"]["messages"]["opening"].replace("{{title}}", title).replace("{{last_name}}", surname)
            self.assertIn(expected, rendered)
            self.assertIn(title + " " + surname, rendered)

    def test_automatic_speech_steps_extract_nothing(self):
        # Live B057: Freya re-ran extraction on passthrough steps before routing,
        # delaying recording and the post-goodbye notes/close_call.
        for brief in ("A", "B"):
            workflow, _ = self.compiled[brief]
            for name, node in workflow["nodes"].items():
                if not name.startswith("speak_") or name == "speak_abort":
                    continue
                with self.subTest(brief=brief, node=name):
                    if node.get("passthrough"):
                        self.assertNotIn("extraction_schema", node)
                    else:
                        self.assertTrue(node["extraction_schema"])

    def test_extraction_validation_rejects_bare_labels_and_accepts_controller_events(self):
        workflow, _ = self.compiled["B"]
        for node in workflow["nodes"].values():
            for schema in node.get("extraction_schema", []):
                pattern = schema["validation_pattern"]
                if schema["name"] == "fb_person_reply":
                    for value in schema["enum_values"]:
                        self.assertIsNotNone(re.fullmatch(pattern, value))
                    for value in ('{"action":"person_yes"}', 'identity_answer', 'confirm'):
                        self.assertIsNone(re.fullmatch(pattern, value))
                    continue
                if schema["name"] == "fb_person_callback":
                    self.assertIsNotNone(re.fullmatch(pattern, '{}'))
                    self.assertIsNone(re.fullmatch(pattern, 'person_yes'))
                    continue
                if schema["name"] == "fb_question_kind":
                    for value in ("none", "ai", "scam"):
                        self.assertIsNotNone(re.fullmatch(pattern, value))
                    self.assertIsNone(re.fullmatch(pattern, "unknown"))
                    continue
                if schema["name"] == "fb_confirm_scope":
                    self.assertEqual(node["name"], "speak_confirm")
                    for value in schema["enum_values"]:
                        self.assertIsNotNone(re.fullmatch(pattern, value))
                    for value in ("yes", '{"action":"confirm"}', "partial"):
                        self.assertIsNone(re.fullmatch(pattern, value))
                    continue
                for bad in ("person_yes", "unknown", '"person_yes"', '{"answer":"yes"}',
                            '{"action":"identity_answer"}', '{"action":"identity_answer","answer":"supplied"}'):
                    self.assertIsNone(re.fullmatch(pattern, bad))
                for event in ({"action": "person_yes"}, {"action": "identity_answer", "answer": "synthetic", "answer_complete": True}):
                    self.assertIsNotNone(re.fullmatch(pattern, json.dumps(event)))

    def test_api_hooks_use_documented_result_mapping_and_reset_stale_write_route(self):
        workflow, _ = self.compiled["B"]
        node = workflow["nodes"]["api_create_promise"]
        tool = node["pre_actions"][0]
        variables = {"fb_next": "api_create_promise", "fb_request_body": '{"test":"synthetic"}'}
        exec(tool["pre_process_code"], {"vars": variables, "config": {}})
        # Even if the following controller is killed before it runs, no old write
        # route remains selected. Default routing can only go to the abort stage.
        self.assertEqual(variables["fb_next"], "speak_abort")
        response = {"status_code": 500, "data": {"ok": False}, "header": {"test": "discard"}}
        exec(tool["post_process_code"], {"result": response})
        self.assertEqual(node["response_mapping"], [{"path": "data.fb_envelope", "variable": "fb_response"}])
        restored = json.loads(response["data"]["fb_envelope"])
        self.assertEqual(restored, {"status_code": 500, "data": {"ok": False}})

    def test_consumed_result_cannot_be_replayed_as_a_new_bank_response(self):
        _, g, bank = self.graph(1, verified=False)
        g.user("person_yes")
        g.user("identity_answer", answer="1988-03-14", answer_complete=True)
        count = len(bank.ledger["calls"])
        g.advance("bank_result")
        self.assertEqual(g.node, "speak_abort")
        self.assertEqual(len(bank.ledger["calls"]), count)

    def test_unsupported_strategy_and_false_api_failure_quote_build_best_effort_and_are_reported(self):
        plan = fake_plan(self.documents, True, self.contracts)
        plan["plans"][0]["confirmation"] = "per_product"
        normalized = validate_plan(plan, self.documents, self.b, self.contracts)
        self.assertEqual(normalized["confirmation"], "combined")
        self.assertTrue(any("combined" in c for c in normalized["best_effort_coercions"]))
        plan = fake_plan(self.documents, True, self.contracts)
        claim = plan["documented_no_effect_failures"][0]
        claim["quote"] = "Invented retry guarantee"
        normalized = validate_plan(plan, self.documents, self.b, self.contracts)
        self.assertNotIn(str(claim["status_code"]), normalized["failure_statuses"].get(claim["tool"], []))
        self.assertTrue(any("unverifiable" in c for c in normalized["best_effort_coercions"]))
        workflow, report = compile_workflow(fake_extraction(self.documents, self.b_raw), plan, self.documents, self.contracts)
        self.assertEqual(report["best_effort_coercions"], normalized["best_effort_coercions"])
        self.assertNotIn("best_effort_coercions", report["runtime_payload"]["plan"])

    def test_source_stage_requirements_remain_in_plan_while_speech_is_prepared(self):
        extraction = fake_extraction(self.documents, self.b_raw)
        plan = fake_plan(self.documents, True, self.contracts)
        marker = "Describe every returned product separately using authoritative display facts."
        plan["plans"][0]["stage_instructions"]["all_today"] = marker
        workflow, report = compile_workflow(extraction, plan, self.documents, self.contracts)
        node = workflow["nodes"]["speak_all_today"]
        self.assertEqual(report["runtime_payload"]["plan"]["stage_instructions"]["all_today"], marker)
        self.assertEqual(node["on_enter"], {"type": "tts", "text": "{{fb_speech}}"})
        plan["plans"][0]["stage_instructions"]["all_today"] = ""
        with self.assertRaises(InvalidInput):
            validate_plan(plan, self.documents, self.b, self.contracts)

    def test_speech_schema_rejects_baked_values_missing_slots_and_success_claims(self):
        for value in ("Pay 700 on Friday.", "Can you pay [[amount]] on [[date]]?",
                      "I recorded [[amount]] for [[product]] on [[date]].",
                      "[[product]] [[amount]] [[date]] [[invented]]"):
            plan = fake_plan(self.documents, True, self.contracts)
            plan['plans'][0]['speech_templates']['offer'] = value
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                validate_plan(plan, self.documents, self.b, self.contracts)

    def test_combined_spoken_readback_announces_product_caps(self):
        _, g, bank = self.graph(11)
        products = g.state['products']
        self.assertTrue(all(p['past_due'] in g.vars['fb_speech'] for p in products))
        day = max(p['deadline'] for p in products)
        g.user('payment', scope='all', amount_kind='full', day={'kind':'iso','value':day})
        self.assertEqual(g.node, 'speak_confirm')
        from datetime import date
        for p in g.state['promises_to_confirm']:
            d=date.fromisoformat(p['date'])
            self.assertIn(str(d.day)+' '+d.strftime('%B')+' '+str(d.year), g.vars['fb_speech'])
            self.assertIn(p['amount'], g.vars['fb_speech'])
        self.assertIn('beyond the deadline', g.vars['fb_speech'])
        self.assertEqual(bank.ledger['promises'], [])

    def test_text_identity_questions_do_not_require_an_invented_date_order(self):
        from dataclasses import replace
        policy = replace(self.b, identity_fields=("postcode", "birth_year"))
        plan = fake_plan(self.documents, True, self.contracts)
        plan["plans"][0]["numeric_date_order"] = "not_applicable"
        plan["plans"][0]["verification_questions"] = [
            {"field": field, "question": "Confirm " + field, "normalization": "text"}
            for field in policy.identity_fields]
        self.assertEqual(validate_plan(plan, self.documents, policy, self.contracts)["numeric_date_order"], "not_applicable")
        plan["plans"][0]["verification_questions"][0]["normalization"] = "date"
        with self.assertRaises(InvalidInput):
            validate_plan(plan, self.documents, policy, self.contracts)
    def test_amount_is_retained_across_date_clarification_and_date_only_correction(self):
        _, g, bank = self.graph(1)
        g.user("payment", amount_kind="explicit", amount="200")
        self.assertEqual(g.node, "speak_negotiate")
        g.user("payment", day={"kind": "tomorrow"})
        self.assertEqual(g.state["promises_to_confirm"][0]["amount"], "200.00")
        g.user("correction", day={"kind": "iso", "value": "2026-10-16"})
        self.assertEqual(g.state["promises_to_confirm"][0]["amount"], "200.00")
        g.user("correction", amount_kind="explicit", amount="210")
        self.assertEqual(g.state["promises_to_confirm"][0]["date"], "2026-10-16")
        self.confirm(g)
        g.finish()
        self.assertEqual(bank.ledger["promises"], [{"product_id": "p1", "amount": 210.0, "date": "2026-10-16"}])

    def test_human_help_offer_pauses_recording_and_acceptance_resumes_it(self):
        _, g, bank = self.graph(11)
        g.user("accept_offer")
        self.confirm(g)
        g.user("human")
        self.assertEqual(g.node, "speak_human_help")
        self.assertEqual(len(bank.ledger["promises"]), 1)
        with self.assertRaises(AssertionError):
            g.speech_done()
        g.user("help_yes")
        self.assertEqual(g.node, "speak_progress")
        g.finish()
        self.assertEqual(len(bank.ledger["promises"]), 3)
        self.assertEqual(bank.ledger["transfers"], [])

    def test_deferring_to_the_agent_after_a_floor_offer_keeps_the_remainder_notice(self):
        # Live PUB-06 (0.5.13): "You tell me." after the floor offer became an
        # "unavailable information" question, and the floor was repeated without
        # saying what stays overdue. A deferral is a non-answer (rule 30).
        case, g, bank = self.graph(6)
        g.user("too_much")
        self.assertIn("The rest stays overdue.", g.vars["fb_speech"])
        self.assertIn("16 October 2026", g.vars["fb_speech"])
        g.user("unknown")
        speech = g.vars["fb_speech"]
        self.assertIn("The rest stays overdue.", speech)
        self.assertEqual(speech.count("The rest stays overdue."), 1)
        self.assertNotIn("I do not have that information", speech)
        self.assertIn("637.50", speech)
        # Attempt 2 names the floor again on the deadline, never the same offer.
        self.assertIn("23 October 2026", speech)
        g.user("payment", amount_kind="floor", day={"kind": "iso", "value": "2026-10-16"})
        self.confirm(g)
        self.assert_pass(case, g, bank)

    def test_too_much_on_a_single_product_offers_the_floor_for_this_week(self):
        # Live PUB-06 (be-01): the floor was offered for today and a cut-off
        # "I can pay that amount, but can I do" recorded today instead of the
        # customer's Friday. The floor offer is persuasion attempt 1 (rule 30).
        case, g, bank = self.graph(6)
        g.user("too_much")
        speech = g.vars["fb_speech"]
        self.assertIn("637.50", speech)
        self.assertIn("16 October 2026", speech)
        self.assertEqual(speech.count("The rest stays overdue."), 1)
        self.assertEqual(g.state["products"][0]["attempts_used"], 1)
        g.user("accept_offer")
        self.confirm(g)
        self.assert_pass(case, g, bank)

    def test_unfinished_reply_to_a_floor_offer_lets_the_customer_finish(self):
        # Controller side of an unparsed reply to the floor offer: clarify, no
        # read-back, no promise. (An extractor rule mapping cut-off replies to
        # unparsed was tried in be-02 and reverted: it made the live extractor
        # drop amounts and dates from complete payment proposals.)
        case, g, bank = self.graph(6)
        g.user("too_much")
        g.user("unparsed")
        self.assertNotEqual(g.node, "speak_confirm")
        self.assertIn("clarify", g.vars["fb_speech"])
        self.assertFalse(bank.ledger["promises"])
        g.user("payment", amount_kind="floor", day={"kind": "iso", "value": "2026-10-16"})
        self.assertIn("16 October 2026", g.vars["fb_speech"])
        self.confirm(g)
        self.assert_pass(case, g, bank)

    def test_below_floor_amount_without_date_gets_floor_offer(self):
        _, g, _ = self.graph(1)
        g.user("payment", amount_kind="explicit", amount="1")
        self.assertEqual(g.state["offers"]["p1"]["amount"], g.state["products"][0]["floor"])
        self.assertIn("remainder", g.state["instruction"])

    def test_multi_product_too_much_starts_attempt_one_without_repeating_today(self):
        _, g, _ = self.graph(11)
        g.user("too_much")
        self.assertEqual(g.state["offers"]["p1"]["date"], "2026-10-16")
        self.assertEqual(g.state["products"][0]["attempts_used"], 1)

    def test_paid_dispute_aggregation_and_early_refusal_to_talk(self):
        _, g, bank = self.graph(11)
        g.user("paid", product_id="p1")
        g.user("dispute", product_id="p2")
        g.user("paid", product_id="p3")
        self.assertEqual(bank.ledger["notes"], [])
        g.finish()
        self.assertEqual(bank.ledger["reason"], "dispute")
        self.assertEqual(len(bank.ledger["notes"]), 3)
        _, g, bank = self.graph(1, verified=False)
        g.user("refuse_talk")
        g.user("refuse_talk")
        g.finish()
        self.assertEqual(bank.ledger["reason"], "declined")

    def test_hardship_voluntary_promise_and_support_transfer(self):
        _, g, bank = self.graph(1)
        g.user("hardship", scope="one", amount_kind="full", day={"kind": "tomorrow"})
        self.assertEqual(g.node, "speak_support")
        self.assertNotIn("Do you confirm?", g.vars["fb_speech"])
        self.assertIn("sympathetic", g.state["instruction"])
        g.user("support_no")
        self.confirm(g)
        g.finish()
        self.assertEqual(len(bank.ledger["promises"]), 1)
        _, g, bank = self.graph(11)
        g.user("hardship")
        g.user("support_yes")
        g.finish()
        self.assertEqual(bank.ledger["notes"], [])
        self.assertEqual(bank.ledger["reason"], "support")

    def test_renamed_requests_and_nested_response_bindings_reach_generated_hooks(self):
        spec = deepcopy(self.contracts.spec)
        # Rename a bank request field and nest the account response, without changing
        # the semantic controller or hard-coding the altered names into it.
        request = spec["paths"]["/create_promise"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        request["properties"]["payment_amount"] = request["properties"].pop("amount")
        request["required"] = ["payment_amount" if k == "amount" else k for k in request["required"]]
        account = spec["paths"]["/get_account"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        wrapped = {"type": "object", "properties": {"result": deepcopy(account)}, "required": ["result"]}
        spec["paths"]["/get_account"]["post"]["responses"]["200"]["content"]["application/json"]["schema"] = wrapped
        contracts = Contracts(spec)
        extraction = fake_extraction(self.documents, self.b_raw)
        for binding in extraction["bindings"]:
            if binding["tool"] == "create_promise":
                next(x for x in binding["request"] if x["role"] == "amount")["field"] = "payment_amount"
            if binding["tool"] == "get_account":
                for item in binding["response"]:
                    item["field"] = "result." + item["field"]
        workflow, report = compile_workflow(extraction, fake_plan(self.documents, True, contracts), self.documents, contracts)
        case, _, bank = self.setup_case(11, verified=False)
        def adapter(name, response):
            if name == "get_account":
                response["data"] = {"result": response["data"]}
            return response
        g = LocalGraph(workflow, bank, case["variables"], report["runtime_payload"]["bindings"], adapter)
        g.user("person_yes")
        for value in case["identity"].values():
            g.user("identity_answer", answer=value, answer_complete=True)
        g.user("accept_offer")
        self.confirm(g)
        self.assertIn("payment_amount", json.loads(g.vars["fb_request_body"]))
        g.finish()
        self.assertEqual(len(bank.ledger["promises"]), 3)

    def test_compilation_run_records_artifact_hash_and_unverified_boundaries(self):
        import hashlib
        extraction = fake_extraction(self.documents, self.b_raw)
        provider = FakeProvider(fake_plan(self.documents, True, self.contracts))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_json(folder / "extraction.json", extraction)
            out = folder / "compiled"
            result = run_compilation([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"], ROOT / "tools/openapi.json",
                                     folder / "extraction.json", out, provider, 30)
            self.assertEqual(result["status"], "compiled_locally")
            self.assertEqual(result["workflow_sha256"], hashlib.sha256((out / "workflow.json").read_bytes()).hexdigest())
            self.assertFalse(result["published"])
            self.assertFalse(result["submission_export"])
            self.assertEqual(result["official_freya_tests"], "not_run")
            self.assertEqual(local_runtime_checks(read_json(out / "workflow.json"))["status"], "passed")
            events = [json.loads(line) for line in (out / "trace.jsonl").read_text().splitlines()]
            self.assertEqual(sum(e["event"] == "model_call_start" for e in events), 1)
            self.assertEqual(events[-1]["event"], "run_end")

    def test_compile_timeout_retains_plan_but_never_emits_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_json(folder / "extraction.json", fake_extraction(self.documents, self.b_raw))
            provider = FakeProvider(fake_plan(self.documents, True, self.contracts))
            out = folder / "compiled"
            with patch("builder.compile_run.time.monotonic", side_effect=[0, 1, 2, 50, 50]):
                with self.assertRaises(ProviderError):
                    run_compilation([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"], ROOT / "tools/openapi.json",
                                    folder / "extraction.json", out, provider, 30)
            self.assertTrue((out / "dialogue-plan.json").exists())
            self.assertFalse((out / "workflow.json").exists())
            self.assertEqual(read_json(out / "manifest.json")["status"], "failed")

    def test_unattended_build_contains_both_model_calls_in_its_trace(self):
        class PipelineProvider(FakeProvider):
            def extract(inner, prompt, schema, **kwargs):
                inner.result = (fake_extraction(self.documents, self.b_raw) if "policy_candidates" in schema["properties"] else
                                fake_plan(self.documents, True, self.contracts))
                return super().extract(prompt, schema, **kwargs)
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "build"
            result = run_build([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"], ROOT / "tools/openapi.json", out, PipelineProvider(None), 60)
            self.assertEqual(result["model_calls"], 2)
            self.assertEqual(result["status"], "compiled_locally")
            events = [json.loads(line) for line in (out / "trace.jsonl").read_text().splitlines()]
            self.assertEqual([e["stage"] for e in events if e["event"] == "model_call_start"], ["extraction", "compiled"])
