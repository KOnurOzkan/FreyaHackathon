from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from builder.inputs import read_documents, read_json
from builder.local_bank import LocalBank, audit_ledger
from builder.policy import Policy
from builder.session import Session

ROOT = Path(__file__).resolve().parents[1]


class OfflineCase(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.socket", side_effect=AssertionError("Offline tests must never open a network socket"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.documents = read_documents([ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"])
        self.a_raw = read_json(ROOT / "tests/fixtures/practice-a.policy.json")
        self.b_raw = read_json(ROOT / "tests/fixtures/practice-b.policy.json")
        self.a = Policy.load(self.a_raw, self.documents[:1])
        self.b = Policy.load(self.b_raw, self.documents)
        self.cases = read_json(ROOT / "public-test-set.json")

    def setup_case(self, number, verified=True, **bank_options):
        # IDs select test fixtures only. No builder code selects behavior by case ID.
        case = deepcopy(next(c for c in self.cases if c["id"] == f"PUB-{number:02d}"))
        policy = self.a if case["brief"] == "A" else self.b
        bank = LocalBank.from_case(case, policy, **bank_options)
        session = Session(policy, case["variables"]["now"], bank)
        if verified:
            session.confirm_person(True)
            for field in policy.identity_fields:
                session.verify(field, case["identity"][field])
            session.load_account()
        return case, session, bank

    def confirm_and_record(self, session):
        snapshot = session.read_back()
        session.confirm(snapshot, True)
        for product_id in session.products:
            if product_id in session.drafts:
                session.record(product_id)

    def assert_ledger(self, case, session, bank, early_reason=None):
        session.finish(early_reason)
        result = audit_ledger(case, bank.ledger)
        self.assertTrue(result["pass"], result["failed"])
        self.assertEqual(result["scope"], "local_ledger_only")
