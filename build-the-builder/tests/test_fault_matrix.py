"""Fault injection at every emitted bank boundary, including lost write results."""
from copy import deepcopy
import json

from builder.contracts import Contracts
from builder.inputs import read_json
from builder.workflow import compile_workflow
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import LocalGraph, fake_plan


class BankBoundaryTests(OfflineCase):
    compiled = None

    def setUp(self):
        super().setUp()
        if BankBoundaryTests.compiled is None:
            contracts = Contracts(read_json(ROOT / "tools/openapi.json"))
            BankBoundaryTests.compiled = compile_workflow(fake_extraction(self.documents, self.b_raw),
                fake_plan(self.documents, True, contracts), self.documents, contracts)

    def graph(self, target, corruption, number=11, verified=True, occurrence=1):
        case, _, bank = self.setup_case(number, verified=False)
        workflow, report = self.compiled
        hits = [0]
        def adapt(tool, envelope):
            if tool == target:
                hits[0] += 1
                if hits[0] == occurrence:
                    return corruption(deepcopy(envelope))
            return envelope
        graph = LocalGraph(workflow, bank, case["variables"], report["runtime_payload"]["bindings"], adapt)
        if verified:
            graph.user("person_yes")
            for value in case["identity"].values():
                graph.user("identity_answer", answer=value, answer_complete=True)
        return case, graph, bank

    def drive(self, target, graph, case):
        if target == "verify_identity":
            graph.user("person_yes")
            graph.user("identity_answer", answer=next(iter(case["identity"].values())), answer_complete=True)
        elif target == "get_account":
            graph.user("person_yes")
            for value in case["identity"].values():
                graph.user("identity_answer", answer=value, answer_complete=True)
        elif target == "schedule_callback":
            graph.user("other_person")
            graph.user("callback", day={"kind": "today"}, time="18:00")
        elif target == "transfer_to_agent":
            graph.user("restructure")
        elif target == "create_promise":
            graph.user("accept_offer")
            graph.user("confirm", explicit_yes=True)
        else:
            for product in list(graph.state["products"]):
                graph.user("paid", product_id=product["product_id"])
            graph.speech_done()

    def test_missing_or_uncertain_result_aborts_at_each_of_seven_tools(self):
        corruptions = {
            "missing_ok": lambda e: {**e, "data": {}},
            "ambiguous_ok": lambda e: {**e, "data": {**e["data"], "ok": "maybe"}},
            "unknown_http_failure": lambda e: {**e, "status_code": 503},
            "malformed_json": lambda e: {**e, "data": "{not json"},
        }
        for target in ("verify_identity", "get_account", "create_promise", "add_note",
                       "schedule_callback", "transfer_to_agent", "close_call"):
            for label, corrupt in corruptions.items():
                with self.subTest(tool=target, fault=label):
                    case, graph, bank = self.graph(target, corrupt,
                        verified=target not in {"verify_identity", "get_account", "schedule_callback"})
                    self.drive(target, graph, case)
                    self.assertEqual(graph.node, "speak_abort")
                    calls = [c["tool"] for c in bank.ledger["calls"]]
                    self.assertEqual(calls[-1], target)
                    self.assertEqual(calls.count(target), 1)
                    before = len(calls)
                    graph.finish()
                    self.assertEqual(len(bank.ledger["calls"]), before)
                    # A committed but unacknowledged promise is never reported as success.
                    if target == "create_promise":
                        self.assertEqual(len(bank.ledger["promises"]), 1)
                        self.assertFalse(any(item["tool"] == "create_promise"
                            for item in json.loads(graph.vars["fb_results"])))

    def test_lost_second_promise_result_preserves_first_without_retry_or_notes(self):
        _, graph, bank = self.graph("create_promise", lambda e: {**e, "status_code": 503}, occurrence=2)
        graph.user("accept_offer")
        graph.user("confirm", explicit_yes=True)
        self.assertEqual(graph.node, "speak_progress")
        graph.speech_done()
        self.assertEqual(graph.node, "speak_abort")
        self.assertEqual(len(bank.ledger["promises"]), 2)  # Second commit happened, reply lost.
        self.assertEqual(bank.ledger["notes"], [])
        journal = json.loads(graph.vars["fb_results"])
        self.assertEqual(sum(x["tool"] == "create_promise" for x in journal), 1)
        graph.finish()
        self.assertEqual(sum(c["tool"] == "create_promise" for c in bank.ledger["calls"]), 2)
        self.assertIsNone(bank.ledger["outcome"])

    def test_contradictory_identity_counters_never_fetch_account(self):
        for changes in ({"all_verified": True}, {"wrong_answers_left": 99},
                        {"verified": False, "wrong_answers_left": 3}, {"verified": True, "wrong_answers_left": 2}):
            with self.subTest(changes=changes):
                case, graph, bank = self.graph("verify_identity",
                    lambda e: {**e, "data": {**e["data"], **changes}}, verified=False)
                self.drive("verify_identity", graph, case)
                self.assertEqual(graph.node, "speak_abort")
                self.assertEqual([c["tool"] for c in bank.ledger["calls"]], ["verify_identity"])

    def test_invalid_account_products_never_reach_a_write(self):
        for label in ("duplicate_id", "missing_floor", "negative_balance", "four_products"):
            with self.subTest(fault=label):
                def corrupt(envelope):
                    products = envelope["data"]["products"]
                    if label == "duplicate_id":
                        products[1]["product_id"] = products[0]["product_id"]
                    elif label == "missing_floor":
                        products[0].pop("minimum_payment")
                    elif label == "negative_balance":
                        products[0]["past_due"] = -1
                    else:
                        products.append({**products[0], "product_id": "fourth"})
                    return envelope
                case, graph, bank = self.graph("get_account", corrupt, verified=False)
                self.drive("get_account", graph, case)
                self.assertEqual(graph.node, "speak_abort")
                self.assertEqual(bank.ledger["promises"], [])
                self.assertEqual(bank.ledger["notes"], [])
