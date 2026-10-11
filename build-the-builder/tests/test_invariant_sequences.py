"""Bounded seeded semantic-event trials, independent of public customer cases.

The observer checks the external bank-call journal and customer-confirmed values.
It does not claim to exercise Freya routing, extraction, voice or interruption
delivery. Fail-closed aborts are permitted; unsafe bank writes are not.
"""
from collections import Counter
from copy import deepcopy
from datetime import date, timedelta
import random

from builder.contracts import Contracts
from builder.inputs import read_json
from builder.local_bank import LocalBank
from builder.workflow import compile_workflow
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import LocalGraph, fake_plan


class InvariantSequencesTests(OfflineCase):
    compiled = {}
    dates = ("2028-02-28", "2028-02-29", "2028-06-30", "2028-07-01",
             "2026-12-24", "2026-12-31", "2027-01-01", "2029-04-02")

    def graph(self, seed, *, verified=True, faults=(), lose_write=False):
        rng = random.Random(seed)
        count = seed % 3 + 1
        limit = seed % 4 + 1
        multiple = count > 1
        docs = self.documents if multiple else self.documents[:1]
        key = (multiple, limit)
        if key not in self.compiled:
            raw = deepcopy(self.b_raw if multiple else self.a_raw)
            raw["wrong_answer_limit"] = limit
            contracts = Contracts(read_json(ROOT / "tools/openapi.json"))
            self.compiled[key] = compile_workflow(fake_extraction(docs, raw),
                fake_plan(docs, multiple, contracts), docs, contracts)
        workflow, report = self.compiled[key]
        products = []
        for index, kind in enumerate(("credit_card", "personal_loan", "car_loan")[:count]):
            floor = rng.randrange(10000, 30000)
            products.append({"product_id": "sequence-" + str(seed) + "-" + str(index),
                "type": kind, "card_last4": "7931", "past_due": (floor + rng.randrange(10000, 80000)) / 100,
                "minimum_payment" if kind == "credit_card" else "instalment_amount": floor / 100,
                "days_late": (4, 19, 7)[index]})
        bank = LocalBank({"date_of_birth": "1986-11-23", "phone_last4": "7903"}, products,
                         wrong_answer_limit=limit, faults=faults)
        lost = [False]
        def adapter(tool, envelope):
            if lose_write and tool == "create_promise" and not lost[0] and envelope["data"].get("ok") is True:
                lost[0] = True
                return {"status_code": 504, "data": "No reliable bank result"}
            return envelope
        graph = LocalGraph(workflow, bank, {"now": self.dates[seed % len(self.dates)] + "T12:40"},
                           report["runtime_payload"]["bindings"], adapter)
        graph.sequence = []
        graph.wrong_limit = limit
        graph.authorized = {}
        graph.observed_calls = 0
        if verified:
            self.step(graph, bank, "person_yes")
            self.step(graph, bank, "identity_answer", answer="1986-11-23", answer_complete=True)
            self.step(graph, bank, "identity_answer", answer="7903", answer_complete=True)
        return graph, bank, limit

    def observe(self, graph, bank):
        """Interpret only actual call inputs/results, not controller counters."""
        calls = bank.ledger["calls"]
        verified, wrong, writes = set(), 0, {}
        closes = [i for i, row in enumerate(calls) if row["tool"] == "close_call"]
        self.assertLessEqual(len(closes), 1, graph.sequence)
        if closes:
            self.assertEqual(closes[0], len(calls) - 1, graph.sequence)
        transfer_at = next((i for i, row in enumerate(calls)
                            if row["tool"] == "transfer_to_agent" and row["result"].get("ok") is True), None)
        if transfer_at is not None:
            self.assertFalse(any(row["tool"] == "add_note" for row in calls), graph.sequence)
            self.assertFalse(any(row["tool"] == "create_promise" for row in calls[transfer_at+1:]), graph.sequence)
        for row in calls:
            tool, args, result = row["tool"], row["args"], row["result"]
            if tool == "verify_identity":
                self.assertLess(wrong, graph.wrong_limit, graph.sequence)
                if result.get("verified") is True:
                    verified.add(args["field"])
                elif result.get("ok") is True and not result.get("hint"):
                    wrong += 1
            if tool in {"get_account", "create_promise", "add_note"}:
                self.assertEqual(verified, set(bank.identity), graph.sequence)
            if tool == "create_promise":
                prior = writes.setdefault(args["product_id"], [])
                self.assertLess(len(prior), 2, graph.sequence)
                if prior:
                    self.assertIs(prior[-1]["result"].get("ok"), False, graph.sequence)
                    self.assertEqual(prior[-1]["args"], args, graph.sequence)
                prior.append(row)
        for row in calls[graph.observed_calls:]:
            if row["tool"] == "create_promise":
                args = row["args"]
                expected = graph.authorized.get(args["product_id"])
                self.assertIsNotNone(expected, graph.sequence)
                self.assertEqual((args["amount"], args["date"]), expected, graph.sequence)
        graph.observed_calls = len(calls)

    def step(self, graph, bank, action=None, **fields):
        graph.sequence.append({"action": action or "speech_done", **fields})
        if action == "confirm" and fields.get("explicit_yes") is True and not any(k in fields for k in ("day", "amount", "amount_kind")):
            if graph.node == "speak_confirm":
                graph.authorized = {p["product_id"]: (float(p["amount"]), p["date"])
                                    for p in graph.state["promises_to_confirm"]}
        elif action in {"payment", "correction", "hardship", "restructure", "support_yes"} or any(k in fields for k in ("day", "amount", "amount_kind")):
            graph.authorized = {}
        if action is None:
            graph.speech_done()
        else:
            graph.user(action, **fields)
        self.observe(graph, bank)

    def business_date(self, graph, offset=0):
        candidate = date.fromisoformat(graph.vars["now"][:10]) + timedelta(days=offset)
        holidays = set(self.a_raw["calendar"]["holidays"])
        while candidate.weekday() > 4 or candidate.isoformat() in holidays:
            candidate += timedelta(days=1)
        return candidate.isoformat()

    def test_seeded_interruptions_keep_external_bank_invariants(self):
        for seed in range(18):
            with self.subTest(seed=seed):
                graph, bank, _ = self.graph(seed, faults=[{"tool": "create_promise", "fail_first_n": seed % 3}],
                                           lose_write=seed % 5 == 0)
                rng = random.Random(4400 + seed)
                for _ in range(16):
                    phase = graph.node
                    if phase == "end":
                        break
                    if phase in {"speak_farewell", "speak_abort"}:
                        self.step(graph, bank)
                    elif phase == "speak_progress":
                        if rng.randrange(4) == 0:
                            self.step(graph, bank, "restructure")
                        else:
                            self.step(graph, bank)
                    elif phase == "speak_support":
                        self.step(graph, bank, rng.choice(("support_yes", "support_no")))
                    elif phase == "speak_human_help":
                        self.step(graph, bank, rng.choice(("human", "help_yes")))
                    elif phase == "speak_confirm":
                        choice = rng.randrange(6)
                        if choice < 4:
                            self.step(graph, bank, "confirm", explicit_yes=True)
                        elif choice == 4:
                            self.step(graph, bank, "human")
                        else:
                            self.step(graph, bank, "correction", day={"kind": "iso", "value": self.business_date(graph, 1)})
                    else:
                        choice = rng.randrange(7)
                        if choice < 3:
                            self.step(graph, bank, "payment", scope="all" if phase == "speak_all_today" else "current",
                                      amount_kind="full", day={"kind": "iso", "value": self.business_date(graph)})
                        elif choice == 3:
                            self.step(graph, bank, "refuse_payment")
                        elif choice == 4:
                            self.step(graph, bank, "hardship")
                        elif choice == 5:
                            self.step(graph, bank, "paid")
                        else:
                            self.step(graph, bank, "human")

    def test_identity_exhaustion_stays_bounded_under_out_of_order_customer_events(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                graph, bank, limit = self.graph(seed, verified=False)
                rng = random.Random(9300 + seed)
                # A premature payment or confirmation must not disclose or fetch an account.
                self.step(graph, bank, rng.choice(("payment", "confirm")), explicit_yes=True,
                          amount_kind="full", day={"kind": "today"})
                self.assertEqual(bank.ledger["calls"], [])
                self.step(graph, bank, "person_yes")
                for attempt in range(limit):
                    self.step(graph, bank, "identity_answer", answer="1982-10-22", answer_complete=True)
                self.assertEqual(sum(row["tool"] == "verify_identity" for row in bank.ledger["calls"]), limit)
                self.assertNotIn("get_account", [row["tool"] for row in bank.ledger["calls"]])
                self.step(graph, bank)
                self.assertEqual(bank.ledger["reason"], "failed")
                # A duplicated late customer event cannot reopen a closed call.
                before = len(bank.ledger["calls"])
                self.step(graph, bank, "identity_answer", answer="1986-11-23", answer_complete=True)
                self.assertEqual(len(bank.ledger["calls"]), before)

    def test_refusal_sequences_never_offer_beyond_each_product_attempt_limit(self):
        for seed in range(6):
            with self.subTest(seed=seed):
                graph, bank, _ = self.graph(seed)
                offers = Counter()
                # This declines the first today question. For B each product then
                # gets only its allowed future persuasion offers, in returned order.
                self.step(graph, bank, "refuse_payment")
                while graph.node == "speak_negotiate":
                    product = graph.state["current_product"]
                    offers[product] += 1
                    self.assertLessEqual(offers[product], 2, graph.sequence)
                    self.step(graph, bank, "refuse_payment")
                expected = {p["product_id"]: 1 if p["days_late"] <= 7 else 2 for p in bank.products}
                self.assertEqual(dict(offers), expected, graph.sequence)
                self.assertEqual(graph.node, "speak_farewell")
                self.step(graph, bank)
                self.assertEqual(bank.ledger["promises"], [])
                self.assertEqual((bank.ledger["outcome"], bank.ledger["reason"]), ("no_promise", "refused"))

    def test_changed_readback_requires_a_new_yes_and_writes_only_the_changed_values(self):
        for seed in range(9):
            with self.subTest(seed=seed):
                graph, bank, _ = self.graph(seed)
                initial_day = self.business_date(graph)
                self.step(graph, bank, "payment", scope="all", amount_kind="full",
                          day={"kind": "iso", "value": initial_day})
                self.assertEqual(graph.node, "speak_confirm")
                target = bank.products[(seed // 3) % len(bank.products)]
                floor = target.get("minimum_payment", target.get("instalment_amount"))
                changed_amount = round(floor + 7.63, 2)
                changed_day = self.business_date(graph, 4)
                # Even if extraction calls this a yes, the explicit changed
                # amount/date must invalidate permission to write the old values.
                self.step(graph, bank, "confirm", explicit_yes=True, product_id=target["product_id"],
                          amount_kind="explicit", amount=str(changed_amount),
                          day={"kind": "iso", "value": changed_day})
                self.assertEqual(bank.ledger["promises"], [])
                self.assertEqual(graph.node, "speak_confirm")
                self.step(graph, bank, "confirm", explicit_yes=True)
                for _ in range(len(bank.products) + 2):
                    if graph.node == "end":
                        break
                    self.step(graph, bank)
                actual = {row["product_id"]: (row["amount"], row["date"]) for row in bank.ledger["promises"]}
                expected = {p["product_id"]: (p["past_due"], initial_day) for p in bank.products}
                expected[target["product_id"]] = (changed_amount, changed_day)
                self.assertEqual(actual, expected, graph.sequence)
                self.assertEqual(graph.node, "end")
