"""Regression probes for invalid partial callback proposals (Brief A rule 7).

The supplied bank policy bounds invalid *days or times*, including proposals
that do not yet contain both fields. No network or language model is involved.
"""
from copy import deepcopy

from builder.contracts import Contracts
from builder.inputs import read_json
from builder.local_bank import LocalBank
from builder.workflow import compile_workflow
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import LocalGraph, fake_plan


class StrictCounterexamples(OfflineCase):
    def graph(self, corrections=1, *, now="2028-02-28T11:00", start="10:00", end="19:00", first_proposal=None):
        raw = deepcopy(self.a_raw)
        raw["calendar"]["callback_start"] = start
        raw["calendar"]["callback_end"] = end
        # February 29 is deliberately a bank holiday in this altered fixture.
        raw["calendar"]["holidays"] = ["2028-02-29"]
        docs = self.documents[:1]
        contracts = Contracts(read_json(ROOT / "tools/openapi.json"))
        plan = fake_plan(docs, False, contracts)
        plan["plans"][0]["callback_corrections"] = corrections
        workflow, report = compile_workflow(fake_extraction(docs, raw), plan, docs, contracts)
        bank = LocalBank({"date_of_birth": "1984-07-21", "phone_last4": "0341"}, [], wrong_answer_limit=3)
        graph = LocalGraph(workflow, bank, {"now": now}, report["runtime_payload"]["bindings"])
        graph.user("other_person", **(first_proposal or {}))
        return graph, bank

    def assert_no_callback_close(self, graph, bank):
        self.assertEqual(graph.node, "speak_farewell")
        graph.finish()
        self.assertEqual([row["tool"] for row in bank.ledger["calls"]], ["close_call"])
        self.assertEqual((bank.ledger["outcome"], bank.ledger["reason"]), ("not_reached", "no_callback"))
        self.assertEqual(bank.ledger["callbacks"], [])

    def test_invalid_time_without_date_uses_supplied_correction_limit(self):
        for corrections in (0, 1, 2):
            with self.subTest(corrections=corrections):
                graph, bank = self.graph(corrections, start="11:30", end="17:45")
                for attempt in range(corrections + 1):
                    graph.user("callback", time="17:46")
                    if attempt < corrections:
                        self.assertEqual(graph.node, "speak_callback")
                self.assert_no_callback_close(graph, bank)

    def test_invalid_day_without_time_uses_supplied_correction_limit(self):
        for corrections in (0, 1, 2):
            with self.subTest(corrections=corrections):
                graph, bank = self.graph(corrections)
                for attempt in range(corrections + 1):
                    graph.user("callback", day={"kind": "iso", "value": "2028-02-29"})
                    if attempt < corrections:
                        self.assertEqual(graph.node, "speak_callback")
                self.assert_no_callback_close(graph, bank)

    def test_complete_invalid_then_invalid_time_only_exhausts_one_correction(self):
        graph, bank = self.graph()
        graph.user("callback", day={"kind": "iso", "value": "2028-03-02"}, time="12:00")
        self.assertEqual(graph.node, "speak_callback")
        graph.user("callback", time="23:00")
        self.assert_no_callback_close(graph, bank)

    def test_first_invalid_partial_gets_window_and_next_valid_proposal_is_recorded(self):
        graph, bank = self.graph(start="11:30", end="17:45")
        graph.user("callback", time="18:00")
        self.assertEqual(graph.node, "speak_callback")
        for expected in ("28 February 2028", "1 March 2028", "11:30", "17:45"):
            self.assertIn(expected, graph.vars["fb_speech"])
        graph.user("callback", day={"kind": "iso", "value": "2028-03-01"}, time="17:45")
        graph.finish()
        self.assertEqual(bank.ledger["callbacks"], [{"date": "2028-03-01", "time": "17:45"}])
        self.assertEqual((bank.ledger["outcome"], bank.ledger["reason"]), ("not_reached", "callback"))

    def test_valid_partial_is_not_itself_an_invalid_attempt(self):
        graph, bank = self.graph(corrections=0)
        graph.user("callback", day={"kind": "iso", "value": "2028-03-01"})
        self.assertEqual(graph.node, "speak_callback")
        self.assertEqual(bank.ledger["calls"], [])
        graph.user("callback", day={"kind": "iso", "value": "2028-03-01"}, time="10:00")
        graph.finish()
        self.assertEqual(bank.ledger["callbacks"], [{"date": "2028-03-01", "time": "10:00"}])
        self.assertEqual([row["tool"] for row in bank.ledger["calls"]], ["schedule_callback", "close_call"])

    def test_two_invalid_components_consume_only_one_correction(self):
        graph, bank = self.graph()
        graph.user("callback", day={"kind": "iso", "value": "2028-02-29"}, time="23:00")
        self.assertEqual(graph.node, "speak_callback")
        graph.user("callback", day={"kind": "iso", "value": "2028-03-01"}, time="19:00")
        graph.finish()
        self.assertEqual(bank.ledger["callbacks"], [{"date": "2028-03-01", "time": "19:00"}])
        self.assertEqual([row["tool"] for row in bank.ledger["calls"]], ["schedule_callback", "close_call"])

    def test_partial_invalid_offer_in_first_relative_reply_is_counted(self):
        graph, bank = self.graph(first_proposal={"time": "22:00"})
        self.assertIn("19:00", graph.vars["fb_speech"])
        graph.user("callback", time="23:00")
        self.assert_no_callback_close(graph, bank)

    def test_today_without_time_is_invalid_when_no_future_window_remains(self):
        for now in ("2028-02-28T19:00", "2028-02-28T19:00:01"):
            with self.subTest(now=now):
                graph, bank = self.graph(corrections=0, now=now)
                graph.user("callback", day={"kind": "today"})
                self.assert_no_callback_close(graph, bank)

    def test_valid_time_without_day_does_not_guess_today_or_consume_a_correction(self):
        graph, bank = self.graph(corrections=0, now="2028-02-28T18:00")
        graph.user("callback", time="10:00")
        self.assertEqual(graph.node, "speak_callback")
        self.assertEqual(bank.ledger["calls"], [])
        graph.user("callback", day={"kind": "iso", "value": "2028-03-01"}, time="10:00")
        graph.finish()
        self.assertEqual(bank.ledger["callbacks"], [{"date": "2028-03-01", "time": "10:00"}])
        self.assertEqual([row["tool"] for row in bank.ledger["calls"]], ["schedule_callback", "close_call"])

    # Live A054 DEV-A-CALLBACK-PARTIAL-VALID: "Tomorrow", then "Eleven in the
    # morning" was asked again until both parts arrived in one utterance.
    # 2028-02-28 is a Monday and 2028-02-29 a holiday, so 2028-03-01 is the
    # next business day in this fixture.
    def assert_scheduled(self, graph, bank, day, at):
        graph.finish()
        self.assertEqual(bank.ledger["callbacks"], [{"date": day, "time": at}])
        self.assertEqual([row["tool"] for row in bank.ledger["calls"]], ["schedule_callback", "close_call"])
        self.assertEqual((bank.ledger["outcome"], bank.ledger["reason"]), ("not_reached", "callback"))

    def test_valid_day_then_time_are_combined_without_a_correction(self):
        graph, bank = self.graph(corrections=0)
        graph.user("callback", day={"kind": "iso", "value": "2028-03-01"})
        self.assertEqual(graph.node, "speak_callback")
        self.assertEqual(bank.ledger["calls"], [])
        graph.user("callback", time="11:00")
        self.assert_scheduled(graph, bank, "2028-03-01", "11:00")

    def test_valid_time_then_day_are_combined_without_a_correction(self):
        graph, bank = self.graph(corrections=0)
        graph.user("callback", time="11:00")
        self.assertEqual(graph.node, "speak_callback")
        graph.user("callback", day={"kind": "iso", "value": "2028-03-01"})
        self.assert_scheduled(graph, bank, "2028-03-01", "11:00")

    def test_relatives_first_partial_reply_is_retained(self):
        graph, bank = self.graph(corrections=0, first_proposal={"day": {"kind": "iso", "value": "2028-03-01"}})
        self.assertEqual(graph.node, "speak_callback")
        graph.user("callback", time="11:00")
        self.assert_scheduled(graph, bank, "2028-03-01", "11:00")

    def test_later_valid_component_replaces_the_retained_one(self):
        graph, bank = self.graph(corrections=0)
        graph.user("callback", time="11:00")
        graph.user("callback", time="15:30")
        graph.user("callback", day={"kind": "today"})
        self.assert_scheduled(graph, bank, "2028-02-28", "15:30")

    def test_valid_day_survives_an_invalid_time_in_the_same_proposal(self):
        graph, bank = self.graph()
        graph.user("callback", day={"kind": "iso", "value": "2028-03-01"}, time="21:00")
        self.assertEqual(graph.node, "speak_callback")
        self.assertIn("19:00", graph.vars["fb_speech"])
        graph.user("callback", time="11:00")
        self.assert_scheduled(graph, bank, "2028-03-01", "11:00")

    def test_invalid_day_is_never_retained_for_a_later_time(self):
        graph, bank = self.graph()
        graph.user("callback", day={"kind": "iso", "value": "2028-02-29"})
        graph.user("callback", time="11:00")
        self.assertEqual(graph.node, "speak_callback")
        self.assertEqual(bank.ledger["calls"], [])

    def test_invalid_replacement_drops_the_earlier_valid_day(self):
        graph, bank = self.graph()
        graph.user("callback", day={"kind": "iso", "value": "2028-03-01"})
        graph.user("callback", day={"kind": "iso", "value": "2028-03-02"})
        graph.user("callback", time="11:00")
        self.assertEqual(graph.node, "speak_callback")
        self.assertEqual(bank.ledger["calls"], [])

    def test_past_combination_counts_once_and_keeps_the_earlier_time(self):
        graph, bank = self.graph(now="2028-02-28T18:00")
        graph.user("callback", time="10:00")
        graph.user("callback", day={"kind": "today"})
        self.assertEqual(graph.node, "speak_callback")
        self.assertIn("19:00", graph.vars["fb_speech"])
        graph.user("callback", day={"kind": "iso", "value": "2028-03-01"})
        self.assert_scheduled(graph, bank, "2028-03-01", "10:00")

    def test_past_combination_exhausts_a_zero_correction_limit(self):
        graph, bank = self.graph(corrections=0, now="2028-02-28T18:00")
        graph.user("callback", time="10:00")
        graph.user("callback", day={"kind": "today"})
        self.assert_no_callback_close(graph, bank)
