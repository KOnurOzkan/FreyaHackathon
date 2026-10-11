"""Exact local arithmetic. All calendar operations receive the call's supplied now."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

from .inputs import InvalidInput


def money(value):
    """Return integer cents; reject excess precision instead of silently rounding."""
    if isinstance(value, bool):
        raise InvalidInput("Invalid monetary amount")
    try:
        number = Decimal(str(value))
        cents = number * 100
        if not number.is_finite() or number < 0 or cents != cents.to_integral_value():
            raise InvalidOperation()
        return int(cents)
    except (InvalidOperation, ValueError) as exc:
        raise InvalidInput("Amount must be finite, nonnegative and have at most two decimals") from exc


def money_text(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def supplied_now(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", value):
        raise InvalidInput("Supply call-local now as YYYY-MM-DDTHH:MM[:SS]; no clock fallback")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInput("Invalid supplied now") from exc


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise InvalidInput("Expected an exact ISO date, not a guessed or vague date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInput("Invalid calendar date") from exc


def numeric_birth_date(value):
    """Only unambiguous structured formats; word-form interpretation belongs to extraction."""
    if not isinstance(value, str):
        raise InvalidInput("Date of birth must be supplied as text")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return iso_date(value).isoformat()
    match = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", value.strip())
    if not match:
        raise InvalidInput("Date needs day/month/four-digit-year or ISO form")
    day, month, year = map(int, match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise InvalidInput("Invalid day/month/year date") from exc


class Calendar:
    def __init__(self, policy):
        self.policy = policy

    def business_day(self, day):
        return day.weekday() in self.policy.weekdays and day not in self.policy.holidays

    def add_business_days(self, day, count):
        if type(count) is not int or not 0 <= count <= 1000:
            raise InvalidInput("Business-day count must be between 0 and 1000")
        # Bounded even for pathological holiday inputs; no infinite loops in runtime helpers.
        for _ in range(36600):
            if not count:
                return day
            day += timedelta(days=1)
            if self.business_day(day):
                count -= 1
        raise InvalidInput("Calendar calculation exceeded supported horizon")

    def next_business_day(self, day):
        return self.add_business_days(day, 1)

    def week_end_offer(self, today):
        monday = today - timedelta(days=today.weekday())
        for offset in (0, 7):
            days = [monday + timedelta(days=offset + i) for i in range(7)]
            valid = [d for d in days if self.business_day(d)]
            if valid and valid[-1] > today:
                return valid[-1]
        raise InvalidInput("No future business day in this or next calendar week")

    def weekday_date(self, today, weekday, following_week=False):
        if type(weekday) is not int or weekday not in range(7):
            raise InvalidInput("Invalid weekday")
        return today - timedelta(days=today.weekday()) + timedelta(days=weekday + (7 if following_week else 0))

    def check_promise(self, candidate, today, deadline):
        if candidate < today or candidate > deadline or not self.business_day(candidate):
            raise InvalidInput("Promise date is past, nonbusiness, or beyond the product deadline")

    def suggest_promise(self, candidate, today, deadline):
        """Return an offer, not permission to record it without a new read-back."""
        if candidate < today:
            raise InvalidInput("A past proposal needs clarification")
        if candidate > deadline:
            return deadline
        adjusted = candidate if self.business_day(candidate) else self.next_business_day(candidate)
        return min(adjusted, deadline)

    def check_callback(self, candidate, now):
        if candidate <= now or candidate.date() not in {now.date(), self.next_business_day(now.date())}:
            raise InvalidInput("Callback must be future, today or next business day")
        if not self.policy.callback_start <= candidate.time() <= self.policy.callback_end:
            raise InvalidInput("Callback outside inclusive time window")


@dataclass(frozen=True)
class Product:
    id: str
    type: str
    past_due: int
    floor: int
    days_late: int

    @classmethod
    def from_account(cls, raw, policy, fields=None):
        """Bind account fields explicitly when an API renames them."""
        fields = fields or {"id": "product_id", "type": "type", "past_due": "past_due", "days_late": "days_late"}
        try:
            product_id, kind = raw[fields["id"]], raw[fields["type"]]
            if not isinstance(product_id, str) or not product_id:
                raise InvalidInput("Account must return a nonempty product ID")
            # Workflow normalization owns this raw-wire container. A legitimate
            # floor may have a name such as "past_due" that is also an internal
            # semantic key; read it from the untouched API item, never its alias.
            wire = raw.get("_fb_wire", raw)
            due, floor = money(raw[fields["past_due"]]), money(wire[policy.floor_fields[kind]])
            if not 0 < floor <= due:
                raise InvalidInput("Product floor must be positive and no greater than arrears")
            days = raw[fields["days_late"]]
            policy.band_for(days)
            return cls(product_id, kind, due, floor, days)
        except (KeyError, TypeError) as exc:
            raise InvalidInput("Account product does not match the configured field bindings") from exc
