"""Evaluation-only conversation variants over unchanged organizer bank fixtures.

Nothing from this module is a builder input. A scenario changes what the synthetic
customer says and the expected result; it cannot create new bank customers or
alter their identity, account products, injected faults, or API contract.
"""
from copy import deepcopy
from datetime import date, datetime, time
import hashlib
import json
import math
from pathlib import Path
import re

from builder.inputs import InvalidInput
from tools.artifact_checks import canonical_digest

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "freya-evaluation-cases-v1"
EDITABLE = {"id", "bank_case_id", "title", "family", "level", "script", "checks", "expected",
            "style", "third_party", "variable_overrides"}


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput("Scenario " + name + " must be a nonempty string")


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise InvalidInput("Scenario expected dates must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise InvalidInput("Scenario contains an invalid calendar date") from None


def _expected(value, products):
    required = {"outcome", "reason", "promises", "notes", "callbacks", "transfers"}
    if not isinstance(value, dict) or set(value) != required:
        raise InvalidInput("Scenario expected result must contain the complete ledger expectation")
    if value["outcome"] not in {"promise", "paid", "transferred", "no_promise", "not_verified", "not_reached"}:
        raise InvalidInput("Scenario has an unknown expected outcome")
    if value["reason"] is not None:
        _text(value["reason"], "expected reason")
    for key in required - {"outcome", "reason"}:
        if not isinstance(value[key], list) or any(not isinstance(row, dict) for row in value[key]):
            raise InvalidInput("Scenario expected ledger entries must be lists of objects")
    product_ids = {"p" + str(index + 1) for index in range(len(products))}
    for field in ("promises", "notes"):
        ids = [row.get("product_id") for row in value[field]]
        if any(item not in product_ids for item in ids) or len(set(ids)) != len(ids):
            raise InvalidInput("Scenario expected product IDs must identify distinct existing fixture products")
    for promise in value["promises"]:
        if set(promise) != {"product_id", "amount", "date"}:
            raise InvalidInput("Scenario promise expectation has unknown or missing fields")
        amount = promise["amount"]
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount <= 0:
            raise InvalidInput("Scenario expected amount must be a finite positive number")
        product = products[int(promise["product_id"][1:]) - 1]
        if amount > product["past_due"]:
            raise InvalidInput("Scenario expected payment exceeds the unchanged fixture's past-due amount")
        _day(promise["date"])
    for note in value["notes"]:
        if set(note) - {"product_id", "reason", "text"} or note.get("reason") not in {"refused", "hardship", "dispute", "paid", "service_error"}:
            raise InvalidInput("Scenario note expectation has an unknown field or reason")
    for callback in value["callbacks"]:
        if set(callback) != {"date", "time"}:
            raise InvalidInput("Scenario callback expectation has unknown or missing fields")
        _day(callback["date"])
        try:
            if not re.fullmatch(r"\d{2}:\d{2}", callback["time"]):
                raise ValueError
            time.fromisoformat(callback["time"])
        except (ValueError, TypeError):
            raise InvalidInput("Scenario callback time must be valid HH:MM") from None
    for transfer in value["transfers"]:
        if set(transfer) != {"queue"} or transfer["queue"] not in {"general", "support", "restructuring"}:
            raise InvalidInput("Scenario transfer expectation has an unknown queue")
    if value["transfers"] and value["notes"]:
        raise InvalidInput("A transferred scenario cannot expect any account notes")


def validate_cases(cases, available):
    if not isinstance(cases, list) or not cases:
        raise InvalidInput("Scenario input must be a nonempty JSON list")
    lookup = {row["id"]: row for row in available}
    seen, normalized = set(), []
    for case in cases:
        if not isinstance(case, dict):
            raise InvalidInput("Every scenario must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", case_id) or case_id in seen:
            raise InvalidInput("Scenario IDs must be unique safe filenames")
        seen.add(case_id)
        bank_id = case.get("bank_case_id", case_id)
        if bank_id not in lookup:
            raise InvalidInput("Every scenario must reference an existing organizer practice bank case")
        base = lookup[bank_id]
        if case_id in lookup and case_id != bank_id:
            raise InvalidInput("A scenario ID cannot impersonate a different public case")
        if (set(case) - (set(base) | EDITABLE) or
                any(case.get(key) != base.get(key) for key in (set(case) | set(base)) - EDITABLE)):
            raise InvalidInput("Scenario cannot alter the fixture identity, products, faults, brief, or original variables")
        if case_id in lookup and {key: value for key, value in case.items() if key != "bank_case_id"} != base:
            raise InvalidInput("Changed scenarios need a distinct ID instead of replacing a public case")
        for field in ("title", "family", "level", "script"):
            _text(case.get(field), field)
        if not isinstance(case.get("checks"), list) or not case["checks"]:
            raise InvalidInput("A scenario needs explicit conversation checks")
        for check in case["checks"]:
            _text(check, "conversation check")
        if "third_party" in case and type(case["third_party"]) is not bool:
            raise InvalidInput("Scenario third_party must be a boolean")
        if "style" in case:
            _text(case["style"], "style")
        overrides = case.get("variable_overrides", {})
        if not isinstance(overrides, dict) or set(overrides) - {"now"}:
            raise InvalidInput("Only the supplied now may be explicitly overridden")
        if "now" in overrides:
            try:
                now = overrides["now"]
                if not isinstance(now, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", now):
                    raise ValueError
                datetime.fromisoformat(now)
            except ValueError:
                raise InvalidInput("Scenario now override must be a valid YYYY-MM-DDTHH:MM") from None
        _expected(case.get("expected"), base["products"])
        normalized.append({**deepcopy(case), "bank_case_id": bank_id})
    return normalized


def load_case_inputs(path=None, selected=None, *, root=ROOT):
    public_path = root / "public-test-set.json"
    public = json.loads(public_path.read_text(encoding="utf-8"))
    source = Path(path) if path else public_path
    raw = source.read_bytes()
    cases = validate_cases(json.loads(raw.decode("utf-8-sig")), public)
    if selected is not None:
        wanted = selected.split(",") if isinstance(selected, str) else list(selected)
        lookup = {row["id"]: row for row in cases}
        if not wanted or len(set(wanted)) != len(wanted) or any(case not in lookup for case in wanted):
            raise InvalidInput("Select distinct case IDs present in the evaluation case file")
        cases = [lookup[case] for case in wanted]
    return {"format": FORMAT, "evaluation_only": True, "source_path": str(source.resolve()),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "public_test_set_sha256": hashlib.sha256(public_path.read_bytes()).hexdigest(), "cases": cases}


def bank_case_ids(cases):
    return list(dict.fromkeys(case.get("bank_case_id", case["id"]) for case in cases))


def apply_session_overrides(case, response):
    variables = response.get("variables")
    if not isinstance(variables, dict) or not isinstance(variables.get("customer_id"), str) or not variables["customer_id"]:
        raise InvalidInput("Practice bank did not return a usable session")
    return {**response, "variables": {**variables, **case.get("variable_overrides", {})}}


def recovery_case_inputs(source, started, *, root=ROOT):
    path = Path(source) / "case-inputs.json"
    if not path.exists():
        if started.get("case_inputs_sha256"):
            raise InvalidInput("The recorded evaluation case snapshot is missing")
        return load_case_inputs(selected=list(started.get("cases") or [started["case"]]), root=root)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if (snapshot.get("format") != FORMAT or snapshot.get("evaluation_only") is not True or
            canonical_digest(snapshot) != started.get("case_inputs_sha256")):
        raise InvalidInput("The evaluation case snapshot no longer matches the simulation")
    public_path = root / "public-test-set.json"
    if snapshot.get("public_test_set_sha256") != hashlib.sha256(public_path.read_bytes()).hexdigest():
        raise InvalidInput("The original public fixture set changed since this simulation")
    normalized = validate_cases(snapshot.get("cases"), json.loads(public_path.read_text(encoding="utf-8")))
    expected = started.get("cases") or {}
    if {row["id"] for row in normalized} != set(expected):
        raise InvalidInput("The saved scenario selection does not match the simulated personas")
    mapping = {case["id"]: case["bank_case_id"] for case in normalized}
    if mapping != started.get("bank_cases"):
        raise InvalidInput("The saved scenario-to-bank fixture mapping changed")
    return snapshot
