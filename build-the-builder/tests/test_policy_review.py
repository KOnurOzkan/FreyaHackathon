"""Policy review regressions with independent amounts, IDs and calendar dates.

These execute emitted code with semantic events; they do not claim speech-model
or Freya simulation passes. Cases are derived from policy invariants, not IDs.
"""
from builder.contracts import Contracts
from builder.inputs import read_json
from builder.local_bank import LocalBank
from builder.workflow import compile_workflow
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import LocalGraph, fake_plan


class PolicyReviewTests(OfflineCase):
    def test_invalid_readback_correction_cannot_leave_the_previous_draft_recordable(self):
        graph, bank = self.graph()
        graph.user("accept_offer")
        graph.user("correction", day={"kind": "iso", "value": "not-a-date"})
        self.assertEqual(graph.node, "speak_negotiate")
        self.assertEqual(graph.state["promises_to_confirm"], [])
        graph.user("confirm", explicit_yes=True)
        self.assertEqual(bank.ledger["promises"], [])

    def test_unparsed_reply_does_not_repeat_collective_today_question(self):
        graph, _ = self.graph(True)
        original = graph.vars["fb_speech"]
        graph.user("unparsed")
        self.assertNotEqual(graph.vars["fb_speech"], original)
        self.assertNotIn("today", graph.vars["fb_speech"].lower())

    def test_declined_support_resumes_retained_voluntary_proposal_with_fresh_confirmation(self):
        graph, bank = self.graph()
        graph.user("hardship", amount_kind="explicit", amount="310.42", day={"kind": "tomorrow"})
        self.assertEqual(graph.node, "speak_support")
        self.assertNotIn("Do you confirm?", graph.vars["fb_speech"])
        graph.user("support_no")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertIn("310.42", graph.vars["fb_speech"])
        self.assertEqual(bank.ledger["promises"], [])
        graph.user("confirm", explicit_yes=True)
        self.assertEqual(bank.ledger["promises"][0]["amount"], 310.42)

    def graph(self, multiple=False, *, vague_clarifications=1, account_products=None):
        documents = self.documents if multiple else self.documents[:1]
        raw = self.b_raw if multiple else self.a_raw
        contracts = Contracts(read_json(ROOT / "tools/openapi.json"))
        plan = fake_plan(documents, multiple, contracts)
        plan["plans"][0]["vague_date_clarifications"] = vague_clarifications
        workflow, report = compile_workflow(fake_extraction(documents, raw), plan, documents, contracts)
        products = [{"product_id": "review-card", "type": "credit_card", "card_last4": "7359",
                     "past_due": 753.42, "minimum_payment": 219.17, "days_late": 6}]
        if multiple:
            products += [{"product_id": "review-loan", "type": "personal_loan", "past_due": 1084.71,
                          "instalment_amount": 357.83, "days_late": 17},
                         {"product_id": "review-car", "type": "car_loan", "past_due": 418.19,
                          "instalment_amount": 143.28, "days_late": 4}]
        bank = LocalBank({"date_of_birth": "1987-09-23", "phone_last4": "6082"},
                         account_products if account_products is not None else products, wrong_answer_limit=3)
        graph = LocalGraph(workflow, bank, {"now": "2028-02-16T12:35"}, report["runtime_payload"]["bindings"])
        graph.user("person_yes")
        graph.user("identity_answer", answer="1987-09-23", answer_complete=True)
        graph.user("identity_answer", answer="6082", answer_complete=True)
        return graph, bank

    def test_accept_support_after_hardship_with_voluntary_proposal_transfers_without_writing(self):
        graph, bank = self.graph()
        graph.user("hardship", amount_kind="explicit", amount="310.42", day={"kind": "tomorrow"})
        self.assertIn("customer support", graph.vars["fb_speech"])
        graph.user("support_yes")
        self.assertEqual(graph.node, "speak_farewell")
        self.assertEqual(bank.ledger["transfers"], [{"queue": "support"}])
        self.assertEqual(bank.ledger["promises"], [])
        graph.finish()
        self.assertEqual((bank.ledger["outcome"], bank.ledger["reason"]), ("transferred", "support"))

    def test_global_hardship_partial_voluntary_payment_does_not_solicit_other_products(self):
        graph, bank = self.graph(True)
        graph.user("hardship")
        graph.user("payment", support_decision="decline", product_id="review-card",
                   amount_kind="full", day={"kind": "tomorrow"})
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual(graph.state["unpromised_reasons"], {"review-loan": "hardship", "review-car": "hardship"})
        self.assertTrue(all(p["attempts_used"] == 0 for p in graph.state["products"]))
        self.assertEqual(bank.ledger["notes"], [])
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        self.assertEqual(len(bank.ledger["promises"]), 1)
        self.assertEqual([n["reason"] for n in bank.ledger["notes"]], ["hardship", "hardship"])

    def test_accepting_new_floor_offer_after_readback_does_not_restore_the_old_full_amount(self):
        graph, bank = self.graph()
        graph.user("accept_offer")
        graph.user("correction", amount_kind="explicit", amount="10.00", day={"kind": "tomorrow"})
        self.assertIn("219.17", graph.vars["fb_speech"])
        graph.user("accept_offer")
        self.assertEqual(graph.state["promises_to_confirm"], [
            {"product_id": "review-card", "amount": "219.17", "date": "2028-02-17"}])
        self.assertEqual(bank.ledger["promises"], [])
        graph.user("confirm", explicit_yes=True)
        self.assertEqual(bank.ledger["promises"][0]["amount"], 219.17)

    def test_accepting_new_deadline_offer_after_readback_does_not_restore_the_old_date(self):
        graph, bank = self.graph()
        graph.user("accept_offer")
        deadline = graph.state["products"][0]["deadline"]
        graph.user("correction", day={"kind": "iso", "value": "2028-04-03"})
        self.assertEqual(graph.state["offers"]["review-card"]["date"], deadline)
        graph.user("accept_offer")
        self.assertEqual(graph.state["promises_to_confirm"][0]["date"], deadline)
        self.assertEqual(bank.ledger["promises"], [])

    def test_past_date_clarification_remembers_the_product_being_corrected(self):
        graph, _ = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "tomorrow"})
        graph.user("correction", product_id="review-loan", day={"kind": "iso", "value": "2028-02-15"})
        self.assertEqual(graph.state["current_product"], "review-loan")
        graph.user("payment", day={"kind": "iso", "value": "2028-02-21"})
        self.assertEqual({p["product_id"]: p["date"] for p in graph.state["promises_to_confirm"]},
                         {"review-card": "2028-02-17", "review-loan": "2028-02-21", "review-car": "2028-02-17"})

    def test_transfer_after_first_recorded_product_keeps_it_and_drops_other_drafts_and_notes(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "tomorrow"})
        graph.user("confirm", explicit_yes=True)
        self.assertEqual(len(bank.ledger["promises"]), 1)
        graph.user("restructure")
        graph.finish()
        self.assertEqual(len(bank.ledger["promises"]), 1)
        self.assertEqual(bank.ledger["notes"], [])
        self.assertEqual((bank.ledger["outcome"], bank.ledger["reason"]), ("transferred", "restructuring"))

    def test_human_request_after_unpromised_disposition_prevents_all_note_writes(self):
        graph, bank = self.graph(True)
        graph.user("dispute", product_id="review-card")
        self.assertEqual(bank.ledger["notes"], [])
        graph.user("human")
        graph.user("human")
        graph.finish()
        self.assertEqual(bank.ledger["notes"], [])
        self.assertEqual(bank.ledger["promises"], [])
        self.assertEqual((bank.ledger["outcome"], bank.ledger["reason"]), ("transferred", "human"))

    def test_unresolved_product_after_collective_question_clarifies_without_repeating_today(self):
        graph, bank = self.graph(True)
        first_question = graph.vars["fb_speech"]
        self.assertIn("pay all these amounts today", first_question)
        graph.user("payment", product_id="a product not in the account", amount_kind="full", day={"kind": "tomorrow"})
        self.assertIn("Which product", graph.vars["fb_speech"])
        self.assertNotIn("today", graph.vars["fb_speech"])
        self.assertEqual(graph.state["promises_to_confirm"], [])
        self.assertEqual(bank.ledger["promises"], [])

    def test_support_decline_with_corrected_day_preserves_held_voluntary_amount(self):
        graph, bank = self.graph()
        graph.user("hardship", amount_kind="explicit", amount="310.42", day={"kind": "tomorrow"})
        graph.user("support_no", day={"kind": "iso", "value": "2028-02-18"})
        self.assertEqual(graph.state["promises_to_confirm"], [
            {"product_id": "review-card", "amount": "310.42", "date": "2028-02-18"}])
        self.assertEqual(bank.ledger["promises"], [])

    def test_new_voluntary_proposal_replaces_held_proposal_before_later_support_decline(self):
        graph, bank = self.graph()
        graph.user("hardship", amount_kind="explicit", amount="310.42", day={"kind": "tomorrow"})
        graph.user("payment", amount_kind="explicit", amount="340.18", day={"kind": "iso", "value": "2028-02-18"})
        graph.user("support_no")
        self.assertEqual(graph.state["promises_to_confirm"], [
            {"product_id": "review-card", "amount": "340.18", "date": "2028-02-18"}])
        self.assertEqual(bank.ledger["promises"], [])

    def test_explicit_amount_without_kind_never_becomes_the_full_arrears(self):
        graph, bank = self.graph()
        graph.user("payment", amount="310.42", day={"kind": "tomorrow"})
        # Either retain the explicit amount or clarify; never silently use full.
        self.assertTrue(all(p["amount"] == "310.42" for p in graph.state["promises_to_confirm"]))
        graph.user("confirm", explicit_yes=True)
        self.assertTrue(all(p["amount"] == 310.42 for p in bank.ledger["promises"]))

    def test_unknown_amount_kind_cannot_silently_record_the_full_arrears(self):
        for at_readback in (False, True):
            with self.subTest(at_readback=at_readback):
                graph, bank = self.graph()
                if at_readback:
                    graph.user("accept_offer")
                graph.user("payment", amount_kind="unrecognized", amount="310.42", day={"kind": "tomorrow"})
                self.assertEqual(graph.state["promises_to_confirm"], [])
                graph.user("confirm", explicit_yes=True)
                self.assertEqual(bank.ledger["promises"], [])

    def test_yes_to_product_clarification_cannot_accept_every_product_today(self):
        graph, bank = self.graph(True)
        graph.user("payment", product_id="unknown-product", amount_kind="full", day={"kind": "tomorrow"})
        self.assertIn("Which product", graph.vars["fb_speech"])
        graph.user("confirm", explicit_yes=True)
        self.assertEqual(graph.state["promises_to_confirm"], [])
        self.assertEqual(bank.ledger["promises"], [])

    def test_accept_offer_label_after_invalid_correction_cannot_revive_old_terms(self):
        graph, bank = self.graph()
        graph.user("accept_offer")
        graph.user("correction", day={"kind": "iso", "value": "not-a-date"})
        self.assertEqual(graph.state["promises_to_confirm"], [])
        graph.user("accept_offer")
        self.assertEqual(graph.state["promises_to_confirm"], [])
        self.assertEqual(bank.ledger["promises"], [])

    def test_dateless_reply_after_vague_date_uses_deadline_without_persuasion_or_old_terms(self):
        for action, fields in (("accept_offer", {}), ("confirm", {"explicit_yes": True}),
                               ("unknown", {}), ("unparsed", {}), ("payment", {})):
            with self.subTest(action=action):
                graph, bank = self.graph()
                graph.user("payment", amount="310.42", day={"kind": "vague", "source_text": "next month"})
                self.assertIn("exact payment date", graph.vars["fb_speech"])
                graph.user(action, **fields)
                self.assertEqual(graph.state["offers"]["review-card"], {"amount": "310.42", "date": "2028-03-03"})
                self.assertEqual(graph.state["products"][0]["attempts_used"], 0)
                self.assertIn("3 March 2028", graph.vars["fb_speech"])
                self.assertEqual(graph.state["promises_to_confirm"], [])
                self.assertEqual(bank.ledger["promises"], [])
                graph.user("accept_offer")
                self.assertEqual(graph.state["promises_to_confirm"], [
                    {"product_id": "review-card", "amount": "310.42", "date": "2028-03-03"}])

    def test_vague_date_clarification_allowance_is_a_supplied_parameter(self):
        graph, bank = self.graph(vague_clarifications=2)
        graph.user("payment", amount="310.42", day={"kind": "vague", "source_text": "when salary comes"})
        graph.user("accept_offer")
        self.assertIn("exact payment date", graph.vars["fb_speech"])
        self.assertEqual(graph.state["promises_to_confirm"], [])
        graph.user("confirm", explicit_yes=True)
        self.assertEqual(graph.state["offers"]["review-card"], {"amount": "310.42", "date": "2028-03-03"})
        self.assertEqual(graph.state["products"][0]["attempts_used"], 0)
        self.assertEqual(bank.ledger["promises"], [])

    def test_shared_nonbusiness_date_correction_keeps_every_product_without_new_persuasion(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "tomorrow"})
        graph.user("correction", scope="all", day={"kind": "iso", "value": "2028-02-19"})
        # The builder may offer the adjusted day together or per product. Each
        # pending product must retain the shared request, without a refusal count.
        for _ in range(3):
            if graph.node == "speak_confirm":
                break
            self.assertEqual(graph.node, "speak_negotiate")
            self.assertEqual(graph.state["offers"][graph.state["current_product"]]["date"], "2028-02-21")
            self.assertTrue(all(p["attempts_used"] == 0 for p in graph.state["products"]))
            graph.user("accept_offer")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual({p["product_id"]: p["date"] for p in graph.state["promises_to_confirm"]},
                         {"review-card": "2028-02-21", "review-loan": "2028-02-21", "review-car": "2028-02-21"})
        self.assertEqual(bank.ledger["promises"], [])

    def test_new_shared_correction_supersedes_every_pending_adjustment(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "tomorrow"})
        graph.user("correction", scope="all", day={"kind": "iso", "value": "2028-02-19"})
        graph.user("correction", scope="all", day={"kind": "iso", "value": "2028-02-24"})
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual({p["product_id"]: p["date"] for p in graph.state["promises_to_confirm"]},
                         {"review-card": "2028-02-24", "review-loan": "2028-02-24", "review-car": "2028-02-24"})
        self.assertEqual(bank.ledger["promises"], [])
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        self.assertEqual([p["date"] for p in bank.ledger["promises"]], ["2028-02-24"] * 3)

    def test_paid_during_shared_adjustment_keeps_remaining_requested_dates_without_persuasion(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "tomorrow"})
        graph.user("correction", scope="all", day={"kind": "iso", "value": "2028-02-19"})
        graph.user("paid", product_id="review-card")
        for _ in range(2):
            if graph.node == "speak_confirm":
                break
            self.assertEqual(graph.state["offers"][graph.state["current_product"]]["date"], "2028-02-21")
            self.assertTrue(all(p["attempts_used"] == 0 for p in graph.state["products"]))
            graph.user("accept_offer")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual({p["product_id"]: p["date"] for p in graph.state["promises_to_confirm"]},
                         {"review-loan": "2028-02-21", "review-car": "2028-02-21"})
        self.assertEqual(bank.ledger["notes"], [])
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        self.assertEqual(bank.ledger["notes"], [{"product_id": "review-card", "reason": "paid"}])

    def test_transfer_during_shared_adjustment_discards_all_pending_promises_and_notes(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "tomorrow"})
        graph.user("correction", scope="all", day={"kind": "iso", "value": "2028-02-19"})
        graph.user("restructure")
        graph.finish()
        self.assertEqual(bank.ledger["promises"], [])
        self.assertEqual(bank.ledger["notes"], [])
        self.assertEqual((bank.ledger["outcome"], bank.ledger["reason"]), ("transferred", "restructuring"))

    def test_queued_shared_date_retains_each_products_own_deadline_cap(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "iso", "value": "2028-02-26"})
        self.assertEqual(graph.state["current_product"], "review-card")
        graph.user("accept_offer")
        # The loan's deadline has passed on the shared date. Its cap needs to
        # survive waiting for the card's weekend adjustment without a new ask.
        self.assertEqual(graph.state["current_product"], "review-car")
        self.assertEqual(graph.state["offers"]["review-car"]["date"], "2028-02-28")
        graph.user("accept_offer")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual({p["product_id"]: p["date"] for p in graph.state["promises_to_confirm"]},
                         {"review-card": "2028-02-28", "review-loan": "2028-02-25", "review-car": "2028-02-28"})
        self.assertTrue(all(p["attempts_used"] == 0 for p in graph.state["products"]))
        self.assertIn("25 February 2028", graph.vars["fb_speech"])
        self.assertEqual(bank.ledger["promises"], [])

    def test_shared_date_only_correction_preserves_distinct_previously_agreed_amounts(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="floor", day={"kind": "tomorrow"})
        graph.user("correction", scope="all", day={"kind": "iso", "value": "2028-02-19"})
        for _ in range(3):
            graph.user("accept_offer")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual({p["product_id"]: p["amount"] for p in graph.state["promises_to_confirm"]},
                         {"review-card": "219.17", "review-loan": "357.83", "review-car": "143.28"})
        self.assertEqual({p["date"] for p in graph.state["promises_to_confirm"]}, {"2028-02-21"})
        self.assertEqual(bank.ledger["promises"], [])

    def test_one_product_correction_preserves_other_waiting_shared_terms(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="floor", day={"kind": "iso", "value": "2028-02-19"})
        graph.user("correction", product_id="review-loan", amount="400.12", day={"kind": "iso", "value": "2028-02-24"})
        self.assertEqual(graph.state["current_product"], "review-card")
        graph.user("accept_offer")
        self.assertEqual(graph.state["current_product"], "review-car")
        graph.user("accept_offer")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual({p["product_id"]: (p["amount"], p["date"]) for p in graph.state["promises_to_confirm"]},
                         {"review-card": ("219.17", "2028-02-21"), "review-loan": ("400.12", "2028-02-24"),
                          "review-car": ("143.28", "2028-02-21")})
        self.assertEqual(bank.ledger["promises"], [])

    def test_exhausting_refusal_for_one_adjusted_offer_preserves_other_shared_requests(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "iso", "value": "2028-02-19"})
        graph.user("refuse_payment")
        graph.user("refuse_payment")
        self.assertEqual(graph.state["unpromised_reasons"], {"review-card": "refused"})
        self.assertEqual(graph.state["current_product"], "review-loan")
        for _ in range(2):
            self.assertEqual(graph.state["offers"][graph.state["current_product"]]["date"], "2028-02-21")
            graph.user("accept_offer")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual([p["attempts_used"] for p in graph.state["products"]], [1, 0, 0])
        self.assertEqual(bank.ledger["notes"], [])
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        self.assertEqual(bank.ledger["notes"], [{"product_id": "review-card", "reason": "refused"}])

    def test_unsupported_shared_correction_withdraws_pending_terms_without_reviving_them(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "iso", "value": "2028-02-19"})
        graph.user("correction", scope="all", day={"kind": "iso", "value": "invalid-date"})
        self.assertEqual(graph.state["promises_to_confirm"], [])
        graph.user("accept_offer")
        self.assertEqual(graph.state["promises_to_confirm"], [])
        self.assertEqual(bank.ledger["promises"], [])

    def test_hardship_discards_earlier_shared_terms_until_a_new_voluntary_proposal(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "iso", "value": "2028-02-19"})
        graph.user("hardship")
        graph.user("support_no")
        self.assertEqual(graph.node, "speak_farewell")
        self.assertEqual(graph.state["promises_to_confirm"], [])
        self.assertTrue(all(p["attempts_used"] == 0 for p in graph.state["products"]))
        graph.finish()
        self.assertEqual(bank.ledger["promises"], [])
        self.assertEqual([note["reason"] for note in bank.ledger["notes"]], ["hardship"] * 3)

    def test_one_product_hardship_preserves_unaffected_shared_requests(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="floor", day={"kind": "iso", "value": "2028-02-19"})
        graph.user("hardship", scope="one", product_id="review-card")
        graph.user("support_no")
        self.assertEqual(graph.state["current_product"], "review-loan")
        for _ in range(2):
            self.assertEqual(graph.state["offers"][graph.state["current_product"]]["date"], "2028-02-21")
            graph.user("accept_offer")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual(graph.state["unpromised_reasons"], {"review-card": "hardship"})
        self.assertTrue(all(p["attempts_used"] == 0 for p in graph.state["products"]))
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        self.assertEqual(bank.ledger["notes"], [{"product_id": "review-card", "reason": "hardship"}])

    def test_human_help_pause_resumes_shared_adjustment_without_losing_terms(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="floor", day={"kind": "iso", "value": "2028-02-19"})
        graph.user("human")
        graph.user("help_yes")
        self.assertEqual(graph.state["current_product"], "review-card")
        for _ in range(3):
            graph.user("accept_offer")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual([p["amount"] for p in graph.state["promises_to_confirm"]], ["219.17", "357.83", "143.28"])
        self.assertEqual({p["date"] for p in graph.state["promises_to_confirm"]}, {"2028-02-21"})
        self.assertEqual(bank.ledger["promises"], [])
        self.assertEqual(bank.ledger["notes"], [])

    def test_repeated_refusal_to_talk_cancels_all_pending_shared_terms(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="floor", day={"kind": "iso", "value": "2028-02-19"})
        graph.user("refuse_talk")
        graph.user("refuse_talk")
        self.assertEqual(graph.node, "speak_farewell")
        self.assertEqual(graph.state["promises_to_confirm"], [])
        graph.finish()
        self.assertEqual(bank.ledger["promises"], [])
        self.assertEqual([note["reason"] for note in bank.ledger["notes"]], ["refused"] * 3)

    def test_new_voluntary_proposal_can_replace_a_previous_unpromised_disposition(self):
        graph, bank = self.graph(True)
        graph.user("paid", product_id="review-card")
        graph.user("payment", product_id="review-card", amount="310.42", day={"kind": "tomorrow"})
        self.assertNotIn("review-card", graph.state["unpromised_reasons"])
        self.assertIn({"product_id": "review-card", "amount": "310.42", "date": "2028-02-17"},
                      graph.state["promises_to_confirm"])
        self.assertEqual(bank.ledger["promises"], [])
        self.assertEqual(bank.ledger["notes"], [])

    def test_other_product_proposal_resumes_pending_offer_without_spending_an_attempt(self):
        graph, bank = self.graph(True)
        graph.user("paid", product_id="review-card")
        pending_offer = dict(graph.state["offers"]["review-loan"])
        self.assertEqual([p["attempts_used"] for p in graph.state["products"]], [0, 1, 0])
        graph.user("payment", product_id="review-card", amount="310.42", day={"kind": "tomorrow"})
        self.assertEqual(graph.state["current_product"], "review-loan")
        self.assertEqual(graph.state["offers"]["review-loan"], pending_offer)
        self.assertEqual([p["attempts_used"] for p in graph.state["products"]], [0, 1, 0])
        graph.user("refuse_payment")
        final_offer = dict(graph.state["offers"]["review-loan"])
        graph.user("correction", product_id="review-card", amount="340.18")
        self.assertEqual(graph.state["offers"]["review-loan"], final_offer)
        self.assertEqual([p["attempts_used"] for p in graph.state["products"]], [0, 2, 0])
        self.assertNotIn("review-loan", graph.state["unpromised_reasons"])
        self.assertEqual(bank.ledger["promises"], [])

    def test_collective_question_requires_product_or_all_scope_for_new_terms(self):
        graph, bank = self.graph(True)
        graph.user("payment", amount="400.12", day={"kind": "tomorrow"})
        self.assertIn("Which product", graph.vars["fb_speech"])
        self.assertEqual(graph.state["promises_to_confirm"], [])
        self.assertTrue(all(p["attempts_used"] == 0 for p in graph.state["products"]))
        graph.user("payment", product_id="review-loan")
        self.assertIn({"product_id": "review-loan", "amount": "400.12", "date": "2028-02-17"},
                      graph.state["promises_to_confirm"])
        self.assertEqual(bank.ledger["promises"], [])

    def test_product_clarification_preserves_terms_after_an_unknown_product_label(self):
        graph, bank = self.graph(True)
        graph.user("payment", product_id="the credit product", amount="310.42", day={"kind": "iso", "value": "2028-02-24"})
        self.assertIn("Which product", graph.vars["fb_speech"])
        graph.user("payment", product_id="review-card")
        self.assertIn({"product_id": "review-card", "amount": "310.42", "date": "2028-02-24"},
                      graph.state["promises_to_confirm"])
        self.assertEqual(bank.ledger["promises"], [])

    def test_combined_readback_ambiguous_correction_needs_target_then_fresh_confirmation(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "tomorrow"})
        graph.user("correction", amount="310.42")
        self.assertIn("Which product", graph.vars["fb_speech"])
        graph.user("confirm", explicit_yes=True)
        self.assertEqual(bank.ledger["promises"], [])
        graph.user("payment", product_id="review-card")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual({p["product_id"]: (p["amount"], p["date"]) for p in graph.state["promises_to_confirm"]},
                         {"review-card": ("310.42", "2028-02-17"), "review-loan": ("1084.71", "2028-02-17"),
                          "review-car": ("418.19", "2028-02-17")})
        self.assertEqual(bank.ledger["promises"], [])
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        self.assertEqual(bank.ledger["promises"][0]["amount"], 310.42)

    def test_explicit_ambiguity_flag_overrules_a_supplied_product_id(self):
        graph, bank = self.graph(True)
        graph.user("payment", product_id="review-card", product_ambiguous=True,
                   amount="310.42", day={"kind": "tomorrow"})
        self.assertIn("Which product", graph.vars["fb_speech"])
        self.assertEqual(graph.state["promises_to_confirm"], [])
        graph.user("payment", product_id="review-loan", amount="420.31", day={"kind": "iso", "value": "2028-02-24"})
        self.assertEqual(graph.state["promises_to_confirm"], [
            {"product_id": "review-loan", "amount": "420.31", "date": "2028-02-24"}])
        self.assertEqual(bank.ledger["promises"], [])

    def test_explicit_all_scope_resolves_held_ambiguity_and_new_terms_override_it(self):
        graph, bank = self.graph(True)
        graph.user("payment", amount="400.12", day={"kind": "tomorrow"})
        graph.user("correction", scope="all", amount_kind="floor", day={"kind": "iso", "value": "2028-02-24"})
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual([p["amount"] for p in graph.state["promises_to_confirm"]], ["219.17", "357.83", "143.28"])
        self.assertEqual({p["date"] for p in graph.state["promises_to_confirm"]}, {"2028-02-24"})
        self.assertEqual(bank.ledger["promises"], [])

    def test_plain_yes_to_collective_today_offer_still_accepts_every_product(self):
        graph, bank = self.graph(True)
        graph.user("confirm", explicit_yes=True)
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual(len(graph.state["promises_to_confirm"]), 3)
        self.assertEqual({p["date"] for p in graph.state["promises_to_confirm"]}, {"2028-02-16"})
        self.assertEqual(bank.ledger["promises"], [])

    def test_transfer_from_ambiguous_correction_drops_old_unrecorded_readback(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "tomorrow"})
        graph.user("correction", amount="310.42")
        graph.user("restructure")
        graph.finish()
        self.assertEqual(bank.ledger["promises"], [])
        self.assertEqual(bank.ledger["notes"], [])
        self.assertEqual((bank.ledger["outcome"], bank.ledger["reason"]), ("transferred", "restructuring"))

    def test_valid_product_id_cannot_override_missing_vague_or_conflicting_reference(self):
        references = (None, "", "that account", "credit card ending 9991", "personal loan",
                      "personal loan ending 7359", "credit card and personal loan")
        for at_readback in (False, True):
            for reference in references:
                with self.subTest(at_readback=at_readback, reference=reference):
                    graph, bank = self.graph(True)
                    if at_readback:
                        graph.user("payment", scope="all", amount_kind="full", day={"kind": "tomorrow"})
                    graph.user("correction" if at_readback else "payment", product_id="review-card",
                               product_reference=reference, amount="310.42", day={"kind": "iso", "value": "2028-02-24"})
                    self.assertIn("Which product", graph.vars["fb_speech"])
                    graph.user("confirm", explicit_yes=True)
                    self.assertEqual(bank.ledger["promises"], [])
                    graph.user("payment", product_id="review-card")
                    self.assertIn({"product_id": "review-card", "amount": "310.42", "date": "2028-02-24"},
                                  graph.state["promises_to_confirm"])
                    self.assertEqual(bank.ledger["promises"], [])

    def test_unique_account_label_or_actual_suffix_grounds_an_explicit_product(self):
        for reference in ("my credit card", "7359", "the credit card ending 7359"):
            with self.subTest(reference=reference):
                graph, bank = self.graph(True)
                graph.user("payment", product_id="review-card", product_reference=reference,
                           amount="310.42", day={"kind": "tomorrow"})
                self.assertIn({"product_id": "review-card", "amount": "310.42", "date": "2028-02-17"},
                              graph.state["promises_to_confirm"])
                self.assertEqual(bank.ledger["promises"], [])

    def test_duplicate_product_labels_require_a_unique_actual_identifier(self):
        products = [
            {"product_id": "card-east", "type": "credit_card", "card_last4": "6238",
             "past_due": 813.49, "minimum_payment": 213.67, "days_late": 4},
            {"product_id": "card-west", "type": "credit_card", "card_last4": "4726",
             "past_due": 901.22, "minimum_payment": 284.36, "days_late": 19}]
        graph, bank = self.graph(True, account_products=products)
        graph.user("payment", product_id="card-east", product_reference="my credit card",
                   amount="350.17", day={"kind": "tomorrow"})
        self.assertIn("Which product", graph.vars["fb_speech"])
        self.assertEqual(graph.state["promises_to_confirm"], [])
        graph.user("payment", product_id="card-west", product_reference="4726")
        self.assertIn({"product_id": "card-west", "amount": "350.17", "date": "2028-02-17"},
                      graph.state["promises_to_confirm"])
        self.assertEqual(bank.ledger["promises"], [])

    def test_hardship_clears_held_ambiguous_payment_without_reusing_its_terms(self):
        graph, bank = self.graph(True)
        graph.user("payment", amount="400.12", day={"kind": "tomorrow"})
        graph.user("hardship")
        graph.user("support_no")
        self.assertEqual(graph.node, "speak_farewell")
        self.assertEqual(graph.state["promises_to_confirm"], [])
        self.assertTrue(all(p["attempts_used"] == 0 for p in graph.state["products"]))
        graph.finish()
        self.assertEqual(bank.ledger["promises"], [])
        self.assertEqual([note["reason"] for note in bank.ledger["notes"]], ["hardship"] * 3)

    def test_hardship_new_voluntary_terms_replace_held_ambiguous_payment(self):
        graph, bank = self.graph(True)
        graph.user("payment", amount="310.42", day={"kind": "tomorrow"})
        graph.user("hardship", scope="all", amount="400.12", day={"kind": "iso", "value": "2028-02-24"})
        graph.user("support_no")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual([p["amount"] for p in graph.state["promises_to_confirm"]], ["400.12"] * 3)
        self.assertEqual({p["date"] for p in graph.state["promises_to_confirm"]}, {"2028-02-24"})
        self.assertTrue(all(p["attempts_used"] == 0 for p in graph.state["products"]))
        self.assertEqual(bank.ledger["promises"], [])

    def test_scoped_hardship_discards_unresolved_correction_without_changing_other_drafts(self):
        graph, bank = self.graph(True)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "tomorrow"})
        graph.user("correction", amount="310.42", day={"kind": "iso", "value": "2028-02-24"})
        graph.user("hardship", scope="one", product_id="review-card")
        graph.user("support_no")
        self.assertEqual(graph.node, "speak_confirm")
        self.assertEqual({p["product_id"]: (p["amount"], p["date"]) for p in graph.state["promises_to_confirm"]},
                         {"review-loan": ("1084.71", "2028-02-17"), "review-car": ("418.19", "2028-02-17")})
        self.assertEqual(graph.state["unpromised_reasons"], {"review-card": "hardship"})
        self.assertEqual(bank.ledger["notes"], [])

    def test_ambiguous_payment_blocks_ungrounded_dispositions_and_refusals(self):
        for action in ("refuse_payment", "unknown", "too_much", "paid", "dispute"):
            for fields in ({}, {"product_id": "review-card", "product_reference": "that account"}):
                with self.subTest(action=action, fields=fields):
                    graph, bank = self.graph(True)
                    graph.user("payment", amount="400.12", day={"kind": "tomorrow"})
                    graph.user(action, **fields)
                    self.assertIn("Which product", graph.vars["fb_speech"])
                    self.assertEqual(graph.state["unpromised_reasons"], {})
                    self.assertEqual(graph.state["promises_to_confirm"], [])
                    self.assertTrue(all(p["attempts_used"] == 0 for p in graph.state["products"]))
                    graph.user("payment", product_id="review-loan")
                    self.assertIn({"product_id": "review-loan", "amount": "400.12", "date": "2028-02-17"},
                                  graph.state["promises_to_confirm"])
                    self.assertEqual(bank.ledger["promises"], [])
                    self.assertEqual(bank.ledger["notes"], [])

    def test_grounded_disposition_cancels_ambiguous_terms_for_the_selected_product(self):
        for action in ("paid", "dispute"):
            with self.subTest(action=action):
                graph, bank = self.graph(True)
                graph.user("payment", amount="400.12", day={"kind": "tomorrow"})
                graph.user(action, product_id="review-loan")
                self.assertEqual(graph.state["unpromised_reasons"], {"review-loan": action})
                self.assertEqual(graph.state["promises_to_confirm"], [])
                self.assertNotIn("Which product", graph.vars["fb_speech"])
                self.assertEqual(bank.ledger["promises"], [])
                self.assertEqual(bank.ledger["notes"], [])

    def test_all_scope_disposition_resolves_every_product_and_defers_notes_until_close(self):
        for action in ("paid", "dispute"):
            with self.subTest(action=action):
                graph, bank = self.graph(True)
                graph.user("payment", amount="400.12", day={"kind": "tomorrow"})
                graph.user(action, scope="all")
                self.assertEqual(graph.node, "speak_farewell")
                self.assertEqual(graph.state["unpromised_reasons"],
                                 {"review-card": action, "review-loan": action, "review-car": action})
                self.assertEqual(graph.state["promises_to_confirm"], [])
                self.assertEqual(bank.ledger["notes"], [])
                graph.finish()
                self.assertEqual(bank.ledger["promises"], [])
                self.assertEqual([note["product_id"] for note in bank.ledger["notes"]],
                                 ["review-card", "review-loan", "review-car"])
                self.assertEqual([note["reason"] for note in bank.ledger["notes"]], [action] * 3)
                self.assertEqual(bank.ledger["outcome"], "paid" if action == "paid" else "no_promise")
