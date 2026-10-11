from dataclasses import replace
from datetime import date, datetime

from builder.inputs import InvalidInput
from builder.policy import Band
from builder.rules import Calendar, Product, iso_date, money, numeric_birth_date, supplied_now
from tests.support import OfflineCase


class RuleTests(OfflineCase):
    def test_money_precision_and_invalid_values(self):
        self.assertEqual(money("2125.10"), 212510)
        self.assertEqual(money("0.29"), 29)
        for amount in ("1.001", "NaN", "Infinity", "-1", True, None):
            with self.subTest(amount=amount), self.assertRaises(InvalidInput):
                money(amount)

    def test_deadlines_exclude_today_weekends_and_holidays(self):
        calendar = Calendar(self.a)
        today = date(2026, 10, 14)
        self.assertEqual(calendar.add_business_days(today, 7), date(2026, 10, 23))
        self.assertEqual(calendar.add_business_days(today, 12), date(2026, 11, 2))
        self.assertEqual(calendar.next_business_day(date(2026, 12, 24)), date(2026, 12, 28))
        self.assertEqual(calendar.next_business_day(date(2026, 12, 31)), date(2027, 1, 4))

    def test_weekday_semantics_and_week_end_offer(self):
        calendar = Calendar(self.a)
        friday = date(2026, 10, 16)
        self.assertEqual(calendar.weekday_date(friday, 4), friday)
        self.assertEqual(calendar.weekday_date(friday, 4, following_week=True), date(2026, 10, 23))
        self.assertEqual(calendar.week_end_offer(friday), date(2026, 10, 23))
        self.assertEqual(calendar.week_end_offer(date(2026, 10, 14)), friday)

    def test_holiday_adjustment_and_deadline_cap_are_offers_only(self):
        calendar = Calendar(self.a)
        today, deadline = date(2026, 10, 14), date(2026, 11, 2)
        self.assertEqual(calendar.suggest_promise(date(2026, 10, 26), today, deadline), date(2026, 10, 27))
        self.assertEqual(calendar.suggest_promise(date(2026, 11, 30), today, deadline), deadline)
        for day in (date(2026, 10, 13), date(2026, 10, 26), date(2026, 11, 3)):
            with self.subTest(day=day), self.assertRaises(InvalidInput):
                calendar.check_promise(day, today, deadline)

    def test_callback_boundaries_future_and_holiday(self):
        calendar = Calendar(self.a)
        now = datetime(2026, 10, 23, 11)
        for day in (datetime(2026, 10, 23, 19), datetime(2026, 10, 27, 10)):
            calendar.check_callback(day, now)
        for day in (now, datetime(2026, 10, 23, 19, 1), datetime(2026, 10, 26, 11), datetime(2026, 10, 27, 9, 59)):
            with self.subTest(day=day), self.assertRaises(InvalidInput):
                calendar.check_callback(day, now)

    def test_numeric_birth_date_is_day_month_year(self):
        self.assertEqual(numeric_birth_date("03/04/1988"), "1988-04-03")
        self.assertEqual(numeric_birth_date("1988-03-14"), "1988-03-14")
        with self.assertRaises(InvalidInput):
            numeric_birth_date("31/02/1988")

    def test_no_clock_fallback_or_guessing_vague_dates(self):
        for value in (None, "", "tomorrow", "2026-10-14"):
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                supplied_now(value)
        with self.assertRaises(InvalidInput):
            iso_date("next week")

    def test_changed_calendar_bands_product_fields_and_floor(self):
        policy = replace(self.a, holidays=frozenset({date(2030, 1, 2)}),
                         bands=(Band(1, 99, 3, 4),), floor_fields={"overdraft": "required_floor"})
        product = Product.from_account({"opaque": "bank-issued-73", "category": "overdraft", "owed": "100.01", "late": 60, "required_floor": "12.34"},
                                       policy, {"id": "opaque", "type": "category", "past_due": "owed", "days_late": "late"})
        self.assertEqual(product.floor, 1234)
        self.assertEqual(product.id, "bank-issued-73")
        self.assertEqual(Calendar(policy).add_business_days(date(2030, 1, 1), policy.band_for(60).business_days), date(2030, 1, 7))
        self.assertEqual(policy.band_for(60).attempts, 4)

    def test_invalid_product_ids_floors_and_bands_rejected(self):
        for raw in ({"type": "credit_card", "past_due": 100, "minimum_payment": 20, "days_late": 2},
                    {"product_id": "x", "type": "credit_card", "past_due": 10, "minimum_payment": 20, "days_late": 2},
                    {"product_id": "x", "type": "credit_card", "past_due": 100, "minimum_payment": 20, "days_late": 46}):
            with self.subTest(raw=raw), self.assertRaises(InvalidInput):
                Product.from_account(raw, self.a)
