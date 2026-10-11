"""Validated data for the supported offline rules, with source provenance.

This is not a complete representation of a bank's dialogue policy. The extractor
retains narrative obligations and amendment precedence separately.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, time

from .inputs import InvalidInput, validate_evidence

RULE_FIELDS = {"calendar", "bands", "floor_fields", "identity_fields", "wrong_answer_limit",
               "noncounting_hints", "promise_attempt_limit", "max_products", "notes_enabled",
               "outcomes", "note_reasons", "transfer_queues"}
NOTE_KINDS = {"refused", "hardship", "dispute", "paid", "service_error"}
TRANSFER_KINDS = {"human", "restructuring", "support"}
OUTCOME_KINDS = NOTE_KINDS | TRANSFER_KINDS | {"promise", "failed", "declined", "callback",
                                            "no_callback", "wrong_number", "deceased", "unclear"}


def positive_integer(value, name):
    if type(value) is not int or value < 1:
        raise InvalidInput(f"{name} must be a positive integer")


def string_map(value, name, keys=None):
    if not isinstance(value, dict) or not value or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in value.items()):
        raise InvalidInput(f"{name} must map strings to nonempty strings")
    if keys is not None and set(value) != keys:
        raise InvalidInput(f"{name} has missing or unsupported semantic roles")


@dataclass(frozen=True)
class Band:
    minimum: int
    maximum: int
    business_days: int
    attempts: int


@dataclass(frozen=True)
class Policy:
    weekdays: frozenset
    holidays: frozenset
    callback_start: time
    callback_end: time
    bands: tuple
    floor_fields: dict
    identity_fields: tuple
    wrong_answer_limit: int
    noncounting_hints: frozenset
    promise_attempt_limit: int
    max_products: int
    notes_enabled: bool
    outcomes: dict
    note_reasons: dict
    transfer_queues: dict

    @classmethod
    def load(cls, raw, documents):
        if not isinstance(raw, dict) or set(raw) != RULE_FIELDS | {"schema_version", "evidence"} or type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise InvalidInput("Unsupported offline policy shape/version")
        evidence = raw["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != RULE_FIELDS:
            raise InvalidInput("Every offline rule section needs source evidence")
        for references in evidence.values():
            if not isinstance(references, list) or not references:
                raise InvalidInput("Evidence must be a nonempty list")
            for reference in references:
                validate_evidence(reference, documents)
        calendar = raw["calendar"]
        if not isinstance(calendar, dict) or set(calendar) != {"weekdays", "holidays", "callback_start", "callback_end"}:
            raise InvalidInput("Unsupported calendar shape")
        weekdays = calendar["weekdays"]
        if not isinstance(weekdays, list) or not weekdays or any(type(x) is not int or x not in range(7) for x in weekdays) or len(set(weekdays)) != len(weekdays):
            raise InvalidInput("Calendar weekdays must be unique integers from 0 to 6")
        if not isinstance(calendar["holidays"], list):
            raise InvalidInput("Holidays must be a list of ISO dates")
        try:
            holidays = frozenset(date.fromisoformat(x) for x in calendar["holidays"])
            start = time.fromisoformat(calendar["callback_start"])
            end = time.fromisoformat(calendar["callback_end"])
            if start.tzinfo or end.tzinfo or start >= end:
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise InvalidInput("Invalid holiday or callback window") from exc
        bands = []
        if not isinstance(raw["bands"], list) or not raw["bands"]:
            raise InvalidInput("At least one overdue band is required")
        for item in raw["bands"]:
            if not isinstance(item, dict) or set(item) != {"minimum", "maximum", "business_days", "attempts"}:
                raise InvalidInput("Invalid overdue band")
            for key, value in item.items():
                positive_integer(value, key)
            if item["minimum"] > item["maximum"]:
                raise InvalidInput("Reversed overdue band")
            bands.append(Band(**item))
        bands.sort(key=lambda b: b.minimum)
        if any(a.maximum >= b.minimum for a, b in zip(bands, bands[1:])):
            raise InvalidInput("Overlapping overdue bands")
        string_map(raw["floor_fields"], "floor_fields")
        if raw["notes_enabled"] is not False or raw["note_reasons"] != {}:
            string_map(raw["note_reasons"], "note_reasons", NOTE_KINDS)
        string_map(raw["transfer_queues"], "transfer_queues", TRANSFER_KINDS)
        fields = raw["identity_fields"]
        if not isinstance(fields, list) or not fields or not all(isinstance(x, str) and x for x in fields) or len(fields) != len(set(fields)):
            raise InvalidInput("Identity fields must be ordered unique names")
        hints = raw["noncounting_hints"]
        if not isinstance(hints, list) or not all(isinstance(x, str) and x for x in hints):
            raise InvalidInput("Invalid noncounting identity hints")
        for key in ("wrong_answer_limit", "promise_attempt_limit", "max_products"):
            positive_integer(raw[key], key)
        if type(raw["notes_enabled"]) is not bool:
            raise InvalidInput("notes_enabled must be boolean")
        outcomes = raw["outcomes"]
        if not isinstance(outcomes, dict) or set(outcomes) != OUTCOME_KINDS:
            raise InvalidInput("Outcome mapping must cover every supported semantic role")
        for item in outcomes.values():
            if not isinstance(item, dict) or set(item) not in ({"outcome"}, {"outcome", "reason"}) or not all(isinstance(v, str) and v for v in item.values()):
                raise InvalidInput("Invalid outcome mapping")
        return cls(frozenset(weekdays), holidays, start, end, tuple(bands),
                   deepcopy(raw["floor_fields"]), tuple(fields), raw["wrong_answer_limit"],
                   frozenset(hints), raw["promise_attempt_limit"], raw["max_products"],
                   raw["notes_enabled"], deepcopy(outcomes), deepcopy(raw["note_reasons"]),
                   deepcopy(raw["transfer_queues"]))

    def band_for(self, days_late):
        if type(days_late) is not int:
            raise InvalidInput("Days late must be an integer")
        matches = [band for band in self.bands if band.minimum <= days_late <= band.maximum]
        if len(matches) != 1:
            raise InvalidInput("No unambiguous policy band for this product")
        return matches[0]
