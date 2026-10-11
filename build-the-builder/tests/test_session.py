from copy import deepcopy
from dataclasses import replace

from builder.inputs import InvalidInput
from builder.local_bank import LocalBank, audit_ledger
from builder.rules import money_text
from builder.session import Session
from tests.support import OfflineCase


class PublicLedgerScenarios(OfflineCase):
    """Scripted semantic actions; these do NOT run or judge customer conversations."""

    def test_pub01_full_today(self):
        case, session, bank = self.setup_case(1)
        session.propose("p1", "420.00", "2026-10-14")
        self.confirm_and_record(session)
        self.assert_ledger(case, session, bank)

    def test_pub02_relative_callback(self):
        case, session, bank = self.setup_case(2, verified=False)
        session.schedule_callback("2026-10-15", "11:00")
        self.assert_ledger(case, session, bank, "callback")

    def test_pub03_three_wrong_answers(self):
        case, session, bank = self.setup_case(3, verified=False)
        session.confirm_person(True)
        for answer in ("1991-07-12", "1992-08-21", "1990-07-21"):
            session.verify("date_of_birth", answer)
        with self.assertRaises(InvalidInput):
            session.load_account()
        self.assert_ledger(case, session, bank, "failed")

    def test_pub04_counteroffer_on_deadline(self):
        case, session, bank = self.setup_case(4)
        session.propose("p1", "1250", session.deadline("p1").isoformat())
        self.assertEqual(session.persuasion_attempts, {})
        self.confirm_and_record(session)
        self.assert_ledger(case, session, bank)

    def test_pub05_hardship(self):
        case, session, bank = self.setup_case(5)
        session.set_hardship("p1")
        with self.assertRaises(InvalidInput):
            session.refuse("p1")
        session.mark_unpromised("p1", "hardship")
        self.assert_ledger(case, session, bank)

    def test_pub06_floor(self):
        case, session, bank = self.setup_case(6)
        session.propose("p1", money_text(session.products["p1"].floor), "2026-10-16")
        self.confirm_and_record(session)
        self.assert_ledger(case, session, bank)

    def test_pub07_holiday_adjusted_before_confirming(self):
        case, session, bank = self.setup_case(7)
        from datetime import date
        day = session.calendar.suggest_promise(date(2026, 10, 26), session.now.date(), session.deadline("p1"))
        session.propose("p1", "540", day.isoformat())
        self.confirm_and_record(session)
        self.assert_ledger(case, session, bank)

    def test_pub08_paid(self):
        case, session, bank = self.setup_case(8)
        session.mark_unpromised("p1", "paid")
        self.assert_ledger(case, session, bank)

    def test_pub09_restructuring(self):
        case, session, bank = self.setup_case(9)
        session.transfer("restructuring")
        self.assert_ledger(case, session, bank)

    def test_pub10_documented_failure_then_retry(self):
        case, session, bank = self.setup_case(10)
        session.propose("p1", "365", "2026-10-16")
        session.confirm(session.read_back(), True)
        self.assertFalse(session.record("p1"))
        self.assertEqual(session.recorded, {})
        self.assertFalse(any(e["event"] == "promise_recorded" for e in session.events))
        self.assertTrue(session.record("p1"))
        self.assert_ledger(case, session, bank)

    def test_pub11_combined_confirmation(self):
        case, session, bank = self.setup_case(11)
        for product in session.products.values():
            session.propose(product.id, money_text(product.past_due), "2026-10-16")
        self.confirm_and_record(session)
        self.assert_ledger(case, session, bank)

    def test_pub12_separate_attempt_counts_and_deferred_notes(self):
        case, session, bank = self.setup_case(12, verified=False)
        session.confirm_person(True)
        session.verify("date_of_birth", case["identity"]["date_of_birth"])
        session.verify("phone_last4", "4401")
        session.verify("phone_last4", "4410")
        session.load_account()
        session.propose("p1", "510", "2026-10-15")
        self.assertEqual([session.refuse("p2"), session.refuse("p2"), session.refuse("p2")], [1, 2, None])
        self.assertEqual([session.refuse("p3"), session.refuse("p3")], [1, None])
        self.assertEqual(bank.ledger["notes"], [])
        self.confirm_and_record(session)
        self.assert_ledger(case, session, bank)

    def test_pub13_shared_date_with_product_deadlines(self):
        case, session, bank = self.setup_case(13)
        from datetime import date
        for product in session.products.values():
            day = session.calendar.suggest_promise(date(2026, 10, 30), session.now.date(), session.deadline(product.id))
            session.propose(product.id, money_text(product.past_due), day.isoformat())
        snapshot = session.read_back()
        self.assertEqual([p.date for p in snapshot[1]], ["2026-10-30", "2026-10-23"])
        session.confirm(snapshot, True)
        session.record("p1")
        session.record("p2")
        self.assert_ledger(case, session, bank)


class GuardTests(OfflineCase):
    def test_refusal_revokes_an_earlier_confirmation(self):
        _, session, bank = self.setup_case(1)
        session.propose("p1", "420", "2026-10-14")
        session.confirm(session.read_back(), True)
        session.refuse("p1")
        with self.assertRaises(InvalidInput):
            session.record("p1")
        self.assertEqual(bank.ledger["promises"], [])

    def test_hardship_revokes_a_pending_nonvoluntary_promise(self):
        _, session, bank = self.setup_case(1)
        session.propose("p1", "420", "2026-10-14")
        session.confirm(session.read_back(), True)
        session.set_hardship("p1")
        with self.assertRaises(InvalidInput):
            session.record("p1")
        self.assertEqual(bank.ledger["promises"], [])

    def test_combined_readback_waits_for_every_product(self):
        _, session, bank = self.setup_case(11)
        session.propose("p1", "220", "2026-10-16")
        with self.assertRaises(InvalidInput):
            session.read_back()

    def test_service_error_cannot_be_invented_without_attempts(self):
        _, session, bank = self.setup_case(1)
        with self.assertRaises(InvalidInput):
            session.mark_unpromised("p1", "service_error")

    def test_successful_callback_prevents_identity_or_other_actions(self):
        _, session, bank = self.setup_case(2, verified=False)
        session.schedule_callback("2026-10-15", "11:00")
        with self.assertRaises(InvalidInput):
            session.confirm_person(True)
        with self.assertRaises(InvalidInput):
            session.transfer("human")
        self.assertEqual(session.finish("callback"), {"outcome": "not_reached", "reason": "callback"})

    def test_identity_requires_person_confirmation_and_order(self):
        case, session, bank = self.setup_case(1, verified=False)
        for action in (lambda: session.confirm_person("yes"), lambda: session.verify("date_of_birth", "1988-03-14"), session.load_account):
            with self.assertRaises(InvalidInput):
                action()
        session.confirm_person(True)
        with self.assertRaises(InvalidInput):
            session.verify("phone_last4", "4471")
        self.assertEqual(bank.ledger["calls"], [])

    def test_hint_does_not_consume_wrong_answer(self):
        case, session, bank = self.setup_case(13, verified=False, swap_hint="day_month_swapped")
        session.confirm_person(True)
        result = session.verify("date_of_birth", "1991-08-01")
        self.assertEqual(result["hint"], "day_month_swapped")
        self.assertEqual(session.wrong_left, 3)
        self.assertFalse(session.verified)
        session.verify("date_of_birth", "1991-01-08")
        session.verify("phone_last4", "6604")
        self.assertTrue(session.verified)

    def test_changed_identity_fields_and_answer_limit(self):
        policy = replace(self.a, identity_fields=("postcode", "birth_year"), wrong_answer_limit=2)
        bank = LocalBank({"postcode": "AB12", "birth_year": "1980"}, [], wrong_answer_limit=2)
        session = Session(policy, "2030-01-01T11:00", bank)
        session.confirm_person(True)
        session.verify("postcode", "wrong")
        session.verify("postcode", "wrong")
        self.assertEqual(session.wrong_left, 0)
        with self.assertRaises(InvalidInput):
            session.verify("postcode", "AB12")
        session.finish("failed")

    def test_string_false_never_treated_as_true(self):
        case, session, bank = self.setup_case(1, verified=False)
        original = bank.call
        def strings(tool, values):
            result = original(tool, values)
            return {k: str(v).lower() if type(v) is bool else v for k, v in result.items()}
        bank.call = strings
        session.confirm_person(True)
        session.verify("date_of_birth", "1900-01-01")
        self.assertFalse(session.verified)
        with self.assertRaises(InvalidInput):
            session.load_account()

    def test_correction_invalidates_confirmation_for_all_products(self):
        _, session, bank = self.setup_case(11)
        for p in session.products.values():
            session.propose(p.id, money_text(p.past_due), "2026-10-16")
        old = session.read_back()
        session.confirm(old, True)
        session.propose("p1", "66", "2026-10-19")
        with self.assertRaises(InvalidInput):
            session.record("p2")
        with self.assertRaises(InvalidInput):
            session.confirm(old, True)
        self.assertEqual(bank.ledger["promises"], [])
        session.confirm(session.read_back(), True)
        self.assertTrue(session.record("p2"))

    def test_below_floor_unknown_id_and_holiday_blocked_before_bank(self):
        _, session, bank = self.setup_case(1)
        count = len(bank.ledger["calls"])
        for args in (("p1", "125.99", "2026-10-14"), ("invented", "420", "2026-10-14"), ("p1", "420", "2026-10-26")):
            with self.subTest(args=args), self.assertRaises(InvalidInput):
                session.propose(*args)
        self.assertEqual(len(bank.ledger["calls"]), count)

    def test_duplicate_write_edit_and_unconfirmed_write_rejected(self):
        _, session, bank = self.setup_case(1)
        session.propose("p1", "420", "2026-10-14")
        with self.assertRaises(InvalidInput):
            session.record("p1")
        self.confirm_and_record(session)
        with self.assertRaises(InvalidInput):
            session.record("p1")
        with self.assertRaises(InvalidInput):
            session.propose("p1", "126", "2026-10-15")
        self.assertEqual(len(bank.ledger["promises"]), 1)

    def test_double_failure_keeps_other_promises_and_notes_failed_product(self):
        _, session, bank = self.setup_case(13)
        bank.faults = [{"tool": "create_promise", "product_id": "p2", "fail_first_n": 2}]
        bank.fault_hits = [0]
        for p in session.products.values():
            session.propose(p.id, money_text(p.past_due), "2026-10-16")
        session.confirm(session.read_back(), True)
        session.record("p1")
        self.assertFalse(session.record("p2"))
        self.assertFalse(session.record("p2"))
        with self.assertRaises(InvalidInput):
            session.record("p2")
        self.assertEqual(session.finish(), {"outcome": "promise"})
        self.assertEqual([p["product_id"] for p in bank.ledger["promises"]], ["p1"])
        self.assertEqual(bank.ledger["notes"], [{"product_id": "p2", "reason": "service_error"}])

    def test_uncertain_write_never_retried_or_claimed_successful(self):
        _, session, bank = self.setup_case(1)
        session.propose("p1", "420", "2026-10-14")
        session.confirm(session.read_back(), True)
        original = bank.call
        def lost_response(tool, body):
            result = original(tool, body)
            if tool == "create_promise":
                raise TimeoutError("response lost after commit")
            return result
        bank.call = lost_response
        with self.assertRaises(TimeoutError):
            session.record("p1")
        with self.assertRaises(InvalidInput):
            session.record("p1")
        with self.assertRaises(InvalidInput):
            session.finish()
        self.assertEqual(len(bank.ledger["promises"]), 1)
        self.assertEqual(session.recorded, {})

    def test_unexpected_port_error_also_prevents_retry(self):
        _, session, bank = self.setup_case(1)
        session.propose("p1", "420", "2026-10-14")
        session.confirm(session.read_back(), True)
        def failed_adapter(tool, body):
            raise RuntimeError("port failed after sending")
        bank.call = failed_adapter
        with self.assertRaises(RuntimeError):
            session.record("p1")
        with self.assertRaises(InvalidInput):
            session.record("p1")
        self.assertTrue(session.uncertain)

    def test_transfer_preserves_recorded_discards_agreed_and_writes_no_notes(self):
        _, session, bank = self.setup_case(11)
        session.mark_unpromised("p3", "paid")
        session.propose("p1", "220", "2026-10-16")
        session.propose("p2", "640", "2026-10-16")
        session.confirm(session.read_back(), True)
        session.record("p1")
        session.transfer("restructuring")
        with self.assertRaises(InvalidInput):
            session.record("p2")
        self.assertEqual(session.finish(), {"outcome": "transferred", "reason": "restructuring"})
        self.assertEqual(bank.ledger["notes"], [])
        self.assertEqual(len(bank.ledger["promises"]), 1)

    def test_no_promise_aggregation_uses_account_order_not_note_order(self):
        _, session, bank = self.setup_case(11)
        session.mark_unpromised("p3", "refused")
        session.mark_unpromised("p1", "paid")
        session.mark_unpromised("p2", "dispute")
        self.assertEqual(session.finish(), {"outcome": "no_promise", "reason": "dispute"})
        self.assertEqual([n["product_id"] for n in bank.ledger["notes"]], ["p1", "p2", "p3"])

    def test_all_paid_aggregation(self):
        _, session, bank = self.setup_case(11)
        for product_id in session.products:
            session.mark_unpromised(product_id, "paid")
        self.assertEqual(session.finish(), {"outcome": "paid"})

    def test_voluntary_hardship_promise(self):
        _, session, bank = self.setup_case(1)
        session.set_hardship("p1")
        with self.assertRaises(InvalidInput):
            session.propose("p1", "420", "2026-10-14")
        session.propose("p1", "420", "2026-10-14", voluntary=True)
        self.confirm_and_record(session)
        self.assertEqual(session.finish(), {"outcome": "promise"})

    def test_close_once_last_and_farewell_order(self):
        case, session, bank = self.setup_case(1)
        session.mark_unpromised("p1", "paid")
        session.finish()
        for action in (session.finish, session.load_account, lambda: session.transfer("human")):
            with self.assertRaises(InvalidInput):
                action()
        self.assertEqual(sum(c["tool"] == "close_call" for c in bank.ledger["calls"]), 1)
        self.assertEqual(session.events[-2:], [{"event": "farewell_required_before_close"}, {"event": "tool", "tool": "close_call"}])
        bank.ledger["calls"].append(deepcopy(bank.ledger["calls"][-1]))
        result = audit_ledger(case, bank.ledger)
        self.assertIn("close_call must occur exactly once", result["failed"])

    def test_changed_outcome_names_are_policy_data(self):
        _, session, bank = self.setup_case(1)
        outcomes = deepcopy(session.policy.outcomes)
        outcomes["paid"] = {"outcome": "settled_elsewhere_v2", "reason": "recent_payment_v2"}
        session.policy = replace(session.policy, outcomes=outcomes)
        session.mark_unpromised("p1", "paid")
        self.assertEqual(session.finish(), outcomes["paid"])

    def test_failed_note_seals_session_and_does_not_claim_finished(self):
        _, session, bank = self.setup_case(13)
        for product_id in session.products:
            session.mark_unpromised(product_id, "paid")
        bank.faults = [{"tool": "add_note", "fail_first_n": 1}]
        bank.fault_hits = [0]
        with self.assertRaises(InvalidInput):
            session.finish()
        with self.assertRaises(InvalidInput):
            session.transfer("human")
        self.assertIsNone(bank.ledger["outcome"])
