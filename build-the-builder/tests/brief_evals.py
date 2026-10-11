"""Synthetic unseen-brief evaluations; never imported by the production builder.

`prepare` writes source inputs and a separate oracle. `run` invokes the real
builder using ONLY the explicit brief/API paths. `check` evaluates the resulting
unedited artifacts. No Freya calls are made. Model runs consume Codex quota.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from unittest.mock import patch

from builder.compile_run import run_build
from builder.codex_provider import CodexProvider, ProviderError
from builder.contracts import Contracts
from builder.extract_run import run_extraction, write_json
from builder.inputs import InvalidInput, read_json, read_documents
from builder.local_bank import LocalBank
from builder.policy import Policy
from tests.workflow_support import LocalGraph

ROOT = Path(__file__).resolve().parents[1]
CASES = ("calendar_amendment", "api_identity_change", "conflicting_memos", "raw_packet")

CALENDAR_MEMO = """# Cedar Bank: approved policy amendment C-17
This signed amendment explicitly overrides conflicting provisions in Brief A and
Brief B. All other provisions of A and B remain in force. This amendment also
supersedes the earlier operations email C-16 in full.

1. The job covers 1 through 60 days late. Replace the complete overdue-band table:
   - 1 through 5 days late: deadline 9 business days after today, 1 persuasion attempt.
   - 6 through 60 days late: deadline 4 business days after today, 2 persuasion attempts.
2. Business days remain Monday through Friday. Replace the holiday list entirely
   with 2026-10-15, 2026-10-19 and 2026-11-02. No old holiday remains effective.
3. Replace the callback time window with 09:30 through 17:15 inclusive. A callback
   must still be later than supplied now, today or the next business day. Allow
   TWO correction opportunities after an invalid proposal, then close no_callback.
4. Allow FOUR wrong security answers in total, replacing every reference to three
   in A. Both ordered security questions and the noncounting swapped-date hint stay.
5. If create_promise returns a documented failure with nothing recorded, retry
   up to TWO times (three attempts total). After the third failure use service_error.
   This replaces the two-failure rule in A rule 37 and B rule 11. Unknown outcomes
   must not be retried. Keep every other product's successful promise.
6. Person confirmation permits THREE asks total, including the opening ask.
   After the third unclear response use not_reached / unclear. Answer questions
   first and preserve all privacy rules.
7. Replace the mandatory post-success sentence with exactly:
   "A confirmation of this promise will be sent by text."
8. If the inherited attempt-1 last-business-day offer would be after that product's
   deadline, propose that deadline instead. This explicitly resolves that date conflict.
"""

IDENTITY_MEMO = """# Cedar Bank: approved identity and wire-format amendment
This amendment explicitly overrides conflicting provisions of Brief A and Brief B.
All other provisions remain effective. The attached API is authoritative for wire
field names. Canonical names in earlier briefs describe the same semantic fields.

1. Replace A rule 11: ask the registered postcode first, then the four-digit birth
   year. The verify_identity field VALUES are postcode then birth_year. Send each
   as text, preserving what the customer confirms. Do not ask for date of birth or
   phone digits. These two checks must pass before fetching accounts.
2. A rule 13 is withdrawn: there are NO noncounting identity hints in this version.
   Three wrong answers total still exhaust verification.
3. The product floor property minimum_payment is now named card_floor;
   instalment_amount is now loan_floor. Product type VALUES do not change.
4. Replace the close_call outcome wire VALUES everywhere in A and B:
   promise -> commitment; paid -> settled; transferred -> escalated;
   no_promise -> unresolved; not_verified -> identity_incomplete;
   not_reached -> unavailable. The reasons and semantic conditions do not change.
5. Replace transfer queue VALUES everywhere: general -> customer_care;
   restructuring -> payment_options; support -> wellbeing. Close-call reasons
   remain human, restructuring and support respectively.
6. Replace the post-success sentence with exactly:
   "We will text you the details of the recorded promise."
"""

REQUEST_NAMES = {"customer_id": "session_ref", "product_id": "product_ref", "amount": "promise_value",
                 "date": "on_date", "field": "check_kind", "answer": "check_answer", "time": "at_time",
                 "queue": "team_queue", "outcome": "call_result", "reason": "reason_code", "text": "memo"}
PRODUCT_NAMES = {"product_id": "reference", "type": "category", "past_due": "arrears", "days_late": "late_days",
                 "minimum_payment": "card_floor", "instalment_amount": "loan_floor"}
OUTCOMES = {"promise": "commitment", "paid": "settled", "transferred": "escalated", "no_promise": "unresolved",
            "not_verified": "identity_incomplete", "not_reached": "unavailable"}
QUEUES = {"general": "customer_care", "restructuring": "payment_options", "support": "wellbeing"}
PRODUCT_TYPES = {"credit_card": "revolving", "personal_loan": "unsecured", "car_loan": "vehicle"}
NOTE_CODES = {"refused": "declined_offer", "hardship": "financial_distress", "dispute": "contested",
              "paid": "already_settled", "service_error": "unavailable_service"}


def rename_properties(schema, names):
    properties = schema.get("properties", {})
    schema["properties"] = {names.get(k, k): {**v, "description": v.get("description", "") +
                                           " Semantic meaning: " + k + "."} for k, v in properties.items()}
    if "required" in schema:
        schema["required"] = [names.get(k, k) for k in schema["required"]]


def altered_api(original):
    spec = deepcopy(original)
    for path, item in spec["paths"].items():
        op = item["post"]
        request = op["requestBody"]["content"]["application/json"]["schema"]
        if path == "/verify_identity":
            request["properties"]["field"]["enum"] = ["postcode", "birth_year"]
            request["properties"]["answer"]["description"] = "Registered postcode or four-digit birth year, supplied as text."
        if path == "/close_call":
            request["properties"]["outcome"]["enum"] = list(OUTCOMES.values())
        if path == "/transfer_to_agent":
            request["properties"]["queue"]["enum"] = list(QUEUES.values())
        rename_properties(request, REQUEST_NAMES)
        for response in op["responses"].values():
            if "content" not in response:
                continue
            media = response["content"]["application/json"]
            schema = media["schema"]
            if path == "/get_account":
                rename_properties(schema["properties"]["products"]["items"], PRODUCT_NAMES)
            if path == "/verify_identity":
                schema["properties"]["hint"].pop("enum", None)
                schema["properties"]["hint"]["description"] = "Unused: no noncounting hints in this API version."
            rename_properties(schema, {"ok": "accepted", "products": "accounts"})
            media["schema"] = {"type": "object", "properties": {"payload": schema}, "required": ["payload"]}
    return spec


def raw_packet_case(folder, original_api, expected_base):
    """A single raw Markdown source, with obsolete correspondence placed last.

    The independent oracle is never included among the model's explicit inputs.
    This is a development fixture; it makes no claim to represent final briefs.
    """
    spec = altered_api(original_api)
    spec["paths"]["/verify_identity"]["post"]["summary"] = "Check one security answer. Four wrong answers in total end the check."
    spec["paths"]["/add_note"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["reason_code"]["enum"] = list(NOTE_CODES.values())
    product = spec["paths"]["/get_account"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["payload"]["properties"]["accounts"]["items"]
    product["properties"]["category"]["enum"] = list(PRODUCT_TYPES.values())
    header = """# Cedar Bank operations packet (one supplied source file)
The sections below are the actual policy records, not a chronological listing.
Signed amendment C-17 is the final authority wherever it conflicts with the base
Brief A, extension Brief B, the approved identity and wire-format amendment, or
operations email C-16. In particular its four-wrong-answer limit replaces the
three-wrong-answer sentence in the identity amendment too. The identity amendment
otherwise remains effective. Unchanged parts of A and B remain effective.
The code list below applies alongside C-17 and replaces only wire code values.

## Current code list
Product type VALUES: credit_card is now revolving; personal_loan is now unsecured;
car_loan is now vehicle. Their substantive policy and floor meanings do not change.
add_note reason VALUES: refused is now declined_offer; hardship is financial_distress;
dispute is contested; paid is already_settled; service_error is unavailable_service.
These five mappings change note API values only, not close_call reasons or how the
semantic final outcome is selected. The attached API specifies all field names.

"""
    calendar = CALENDAR_MEMO.replace(
        "Both ordered security questions and the noncounting swapped-date hint stay.",
        "This clause changes only the wrong-answer limit. The security questions and\n"
        "   hint behavior are those specified in the approved identity amendment.")
    sections = [header, calendar, IDENTITY_MEMO,
        (folder / "brief-a.md").read_text(encoding="utf-8"),
        (folder / "brief-b.md").read_text(encoding="utf-8"),
        "# Superseded operations email C-16\nThis email amends A and B: use 11:00 through 18:00 callbacks.\n"
        "Allow only two wrong answers and a single promise-write attempt.\n"
        "This email is obsolete: signed C-17 above explicitly supersedes it in full.\n"]
    (folder / "packet.md").write_text("\n\n---\n\n".join(sections), encoding="utf-8")
    policy = deepcopy(expected_base)
    policy.update(calendar={"weekdays": [0, 1, 2, 3, 4], "holidays": ["2026-10-15", "2026-10-19", "2026-11-02"],
                  "callback_start": "09:30", "callback_end": "17:15"},
        bands=[{"minimum": 1, "maximum": 5, "business_days": 9, "attempts": 1},
               {"minimum": 6, "maximum": 60, "business_days": 4, "attempts": 2}],
        identity_fields=["postcode", "birth_year"], noncounting_hints=[], wrong_answer_limit=4, promise_attempt_limit=3,
        floor_fields={"revolving": "card_floor", "unsecured": "loan_floor", "vehicle": "loan_floor"},
        note_reasons=NOTE_CODES.copy(),
        transfer_queues={"human": "customer_care", "restructuring": "payment_options", "support": "wellbeing"})
    for value in policy["outcomes"].values():
        value["outcome"] = OUTCOMES[value["outcome"]]
    expected = {"status": "compiled_locally", "policy": policy,
        "plan": {"callback_corrections": 2, "person_confirmation_attempts": 3,
                 "recorded_promise": "A confirmation of this promise will be sent by text."},
        "calendars": [
            {"now": "2026-10-14T11:00", "next_business": "2026-10-16", "requested": "2026-10-28",
             "deadlines": ["2026-10-29", "2026-10-22", "2026-10-22"]},
            {"now": "2027-12-30T16:00", "next_business": "2027-12-31", "requested": "2028-01-10",
             "deadlines": ["2028-01-12", "2028-01-05", "2028-01-05"]},
            {"now": "2028-02-28T10:05", "next_business": "2028-02-29", "requested": "2028-03-08",
             "deadlines": ["2028-03-10", "2028-03-03", "2028-03-03"]}]}
    return spec, expected, ["packet.md"]


def prepare_pack(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    base = read_json(ROOT / "tests/fixtures/practice-b.policy.json")
    expected_base = {k: v for k, v in base.items() if k not in {"schema_version", "evidence"}}
    api = read_json(ROOT / "tools/openapi.json")
    api["servers"] = [{"url": "https://synthetic-bank.invalid/v1"}]
    cases = []
    for name in CASES:
        folder = output / name
        folder.mkdir()
        for label in ("a", "b"):
            source = (ROOT / f"briefs/brief-{label}.md").read_text(encoding="utf-8")
            (folder / f"brief-{label}.md").write_text(source.replace("Freya Bank", "Cedar Bank"), encoding="utf-8")
        spec = deepcopy(api)
        expected = {"policy": deepcopy(expected_base), "status": "compiled_locally"}
        briefs = ["brief-a.md", "brief-b.md"]
        if name == "calendar_amendment":
            (folder / "amendment.md").write_text(CALENDAR_MEMO, encoding="utf-8")
            (folder / "old-email.md").write_text(
                "# Operations email C-16\nThis email amends the callback window in A and B to 11:00 through 18:00.\n",
                encoding="utf-8")
            # Deliberately put an explicitly superseded email LAST. Order is not authority.
            briefs += ["amendment.md", "old-email.md"]
            expected["policy"].update(calendar={"weekdays": [0, 1, 2, 3, 4],
                "holidays": ["2026-10-15", "2026-10-19", "2026-11-02"], "callback_start": "09:30", "callback_end": "17:15"},
                bands=[{"minimum": 1, "maximum": 5, "business_days": 9, "attempts": 1},
                       {"minimum": 6, "maximum": 60, "business_days": 4, "attempts": 2}],
                wrong_answer_limit=4, promise_attempt_limit=3)
            expected["plan"] = {"callback_corrections": 2, "person_confirmation_attempts": 3,
                "recorded_promise": "A confirmation of this promise will be sent by text."}
            expected["deadlines"] = ["2026-10-29", "2026-10-22"]
            spec["paths"]["/verify_identity"]["post"]["summary"] = "Check one security answer. Four wrong answers in total end the check."
        elif name == "api_identity_change":
            (folder / "amendment.md").write_text(IDENTITY_MEMO, encoding="utf-8")
            briefs += ["amendment.md"]
            spec = altered_api(api)
            expected["policy"].update(identity_fields=["postcode", "birth_year"], noncounting_hints=[],
                floor_fields={"credit_card": "card_floor", "personal_loan": "loan_floor", "car_loan": "loan_floor"},
                transfer_queues={"human": "customer_care", "restructuring": "payment_options", "support": "wellbeing"})
            for value in expected["policy"]["outcomes"].values():
                value["outcome"] = OUTCOMES[value["outcome"]]
            expected["plan"] = {"recorded_promise": "We will text you the details of the recorded promise."}
            expected["deadlines"] = ["2026-11-02", "2026-10-23"]
        elif name == "raw_packet":
            spec, expected, briefs = raw_packet_case(folder, api, expected_base)
        else:
            for label, window in (("x", "08:00 through 12:00"), ("y", "14:00 through 18:00")):
                (folder / f"memo-{label}.md").write_text(
                    f"# Undated approved callback memo {label.upper()}\nThis memo overrides A rule 6: the ONLY permitted callback window is {window} inclusive.\n"
                    "All other base rules remain. No effective date or priority relative to other memos is supplied.\n", encoding="utf-8")
            briefs += ["memo-x.md", "memo-y.md"]
            expected = {"status": "blocked", "blocker_kind": "contradiction"}
        Contracts(spec)
        write_json(folder / "openapi.json", spec)
        write_json(folder / "expected.json", expected)
        cases.append({"name": name, "briefs": briefs, "api": "openapi.json", "expected": "expected.json"})
    manifest = {"purpose": "synthetic_development_evaluations", "version": 4, "official_freya_tests": False,
                "oracle_sent_to_builder": False, "cases": cases}
    write_json(output / "pack.json", manifest)
    return manifest


def differences(expected, actual, path="policy"):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [path + ": expected object"]
        return [error for key, value in expected.items()
                for error in differences(value, actual.get(key), path + "." + key)]
    if expected != actual:
        return [path + ": expected " + repr(expected) + ", got " + repr(actual)]
    return []


def runtime_audit(folder, run, expected):
    """Exercise actual emitted code/hooks against an independently chosen scenario."""
    compiled = run / "compiled"
    workflow = read_json(compiled / "workflow.json")
    payload = read_json(compiled / "runtime-payload.json")
    raw = read_json(compiled / "policy.json")
    documents = read_documents([folder / name for name in read_json(run / "eval-inputs.json")["briefs"]])
    policy = Policy.load(raw, documents)
    api_changed = folder.name == "api_identity_change"
    identity = {"postcode": "AB12 3CD", "birth_year": "1986"} if api_changed else {
        "date_of_birth": "1986-04-03", "phone_last4": "4826"}
    products = [{"product_id": "synthetic-first", "type": "credit_card", "past_due": 360,
                 "days_late": 3, "minimum_payment": 60},
                {"product_id": "synthetic-second", "type": "personal_loan", "past_due": 900,
                 "days_late": 40, "instalment_amount": 225}]
    if api_changed:
        products = [{PRODUCT_NAMES.get(k, k): v for k, v in p.items()} for p in products]
        # The local bank stores authoritative IDs under its own canonical internal key.
        products = [{**p, "product_id": p["reference"]} for p in products]
    def adapt(tool, envelope):
        if not api_changed:
            return envelope
        body = envelope["data"]
        body = {({"ok": "accepted", "products": "accounts"}.get(k, k)): v for k, v in body.items()}
        return {**envelope, "data": {"payload": body}}

    def fresh(verified=False, faults=()):
        bank = LocalBank(identity, products, wrong_answer_limit=policy.wrong_answer_limit, faults=faults)
        graph = LocalGraph(workflow, bank, {"now": "2026-10-14T11:00", "title": "Mx", "last_name": "Sample"},
                           payload["bindings"], adapt)
        if verified:
            graph.user("person_yes")
            for answer in identity.values():
                graph.user("identity_answer", answer=answer, answer_complete=True)
        return graph, bank
    graph, bank = fresh()
    errors = []
    with patch("socket.socket", side_effect=AssertionError("No network in generated-code evaluations")):
        graph.user("person_yes")
        for answer in identity.values():
            graph.user("identity_answer", answer=answer, answer_complete=True)
        if graph.node != "speak_all_today":
            return ["runtime: verification did not reach all-product announcement: " + graph.node]
        errors += differences(expected["deadlines"], [p["deadline"] for p in graph.state["products"]], "runtime.deadlines")
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "iso", "value": "2026-10-28"})
        if graph.node != "speak_confirm" or bank.ledger["promises"]:
            return errors + ["runtime: combined readback/confirmation guard failed"]
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        dates = ["2026-10-28", expected["deadlines"][1]]
        errors += differences(dates, [p["date"] for p in bank.ledger["promises"]], "runtime.promise_dates")
        errors += differences(expected["policy"]["outcomes"]["promise"]["outcome"], bank.ledger["outcome"], "runtime.outcome")
        calls = [c["tool"] for c in bank.ledger["calls"]]
        if calls.count("close_call") != 1 or calls[-1] != "close_call" or bank.ledger["notes"]:
            errors.append("runtime: final-close/no-notes invariant failed")
        # Exercise changed counters through the emitted controller, not just JSON equality.
        graph, bank = fresh()
        asks = expected["plan"].get("person_confirmation_attempts", 2)
        for index in range(asks):
            graph.user("person_unclear")
            errors += differences("speak_person" if index < asks - 1 else "speak_farewell", graph.node,
                                  "runtime.person_asks." + str(index + 1))
        graph.finish()
        errors += differences(expected["policy"]["outcomes"]["unclear"],
            {"outcome": bank.ledger["outcome"], "reason": bank.ledger["reason"]}, "runtime.unclear_outcome")

        graph, bank = fresh()
        graph.user("other_person")
        corrections = expected["plan"].get("callback_corrections", 1)
        for _ in range(corrections):
            graph.user("callback", day={"kind": "today"}, time="08:00")
            errors += differences("speak_callback", graph.node, "runtime.callback_corrections")
        graph.user("callback", day={"kind": "today"}, time=expected["policy"]["calendar"]["callback_end"])
        graph.finish()
        errors += differences(1, len(bank.ledger["callbacks"]), "runtime.callback_inclusive_end")

        graph, bank = fresh()
        graph.user("person_yes")
        for index in range(expected["policy"]["wrong_answer_limit"]):
            graph.user("identity_answer", answer="WRONG" if api_changed else "1970-01-01", answer_complete=True)
            errors += differences("speak_identity" if index < expected["policy"]["wrong_answer_limit"] - 1 else "speak_farewell",
                                  graph.node, "runtime.wrong_answer_limit")
        graph.finish()
        if any(c["tool"] == "get_account" for c in bank.ledger["calls"]):
            errors.append("runtime: identity exhaustion disclosed an account")

        limit = expected["policy"]["promise_attempt_limit"]
        for failures in (limit - 1, limit):
            graph, bank = fresh(True, [{"tool": "create_promise", "product_id": "synthetic-first", "fail_first_n": failures}])
            graph.user("accept_offer")
            graph.user("confirm", explicit_yes=True)
            graph.finish()
            writes = [c for c in bank.ledger["calls"] if c["tool"] == "create_promise" and
                      c["args"]["product_id"] == "synthetic-first"]
            errors += differences(limit, len(writes), "runtime.write_attempt_limit")
            errors += differences(2 if failures < limit else 1, len(bank.ledger["promises"]), "runtime.partial_success")
            errors += differences([] if failures < limit else [{"product_id": "synthetic-first", "reason": "service_error"}],
                                  bank.ledger["notes"], "runtime.deferred_failure_note")

        graph, bank = fresh(True)
        graph.user("paid", product_id="synthetic-first")
        graph.user("restructure")
        graph.finish()
        errors += differences([{"queue": expected["policy"]["transfer_queues"]["restructuring"]}],
                              bank.ledger["transfers"], "runtime.transfer_queue")
        errors += differences([], bank.ledger["notes"], "runtime.transfer_forbids_notes")
    return errors


def raw_runtime_audit(folder, run, expected):
    """Three accounts, remapped wire codes and independent fixed date oracles."""
    compiled = run / "compiled"
    workflow = read_json(compiled / "workflow.json")
    payload = read_json(compiled / "runtime-payload.json")
    identity = {"postcode": "ZT84 7QP", "birth_year": "1982"}
    semantic_products = [
        {"product_id": "random-r-482", "type": "revolving", "past_due": 317.49, "days_late": 2, "minimum_payment": 98.33},
        {"product_id": "random-u-716", "type": "unsecured", "past_due": 904.12, "days_late": 12, "instalment_amount": 200.06},
        {"product_id": "random-v-053", "type": "vehicle", "past_due": 1257.89, "days_late": 58, "instalment_amount": 301.28}]
    products = [{**{PRODUCT_NAMES.get(k, k): v for k, v in p.items()}, "product_id": p["product_id"]}
                for p in semantic_products]

    def adapt(tool, envelope):
        body = {({"ok": "accepted", "products": "accounts"}.get(k, k)): v for k, v in envelope["data"].items()}
        return {**envelope, "data": {"payload": body}}

    def fresh(now, faults=(), verified=True):
        bank = LocalBank(identity, products, wrong_answer_limit=4, faults=faults)
        graph = LocalGraph(workflow, bank, {"now": now, "title": "Mx", "last_name": "Synthetic"},
                           payload["bindings"], adapt)
        if verified:
            graph.user("person_yes")
            for answer in identity.values():
                graph.user("identity_answer", answer=answer, answer_complete=True)
        return graph, bank

    errors = []
    for case in expected["calendars"]:
        prefix = "runtime." + case["now"]
        graph, bank = fresh(case["now"])
        if graph.node != "speak_all_today":
            errors.append(prefix + ": verification did not reach the all-product announcement")
            continue
        errors += differences(case["deadlines"], [p["deadline"] for p in graph.state["products"]], prefix + ".deadlines")
        for amount in ("317.49", "904.12", "1257.89"):
            if amount not in graph.vars["fb_speech"]:
                errors.append(prefix + ": product balance absent from initial announcement: " + amount)
        graph.user("payment", scope="all", amount_kind="full", day={"kind": "iso", "value": case["requested"]})
        if graph.node != "speak_confirm" or bank.ledger["promises"]:
            errors.append(prefix + ": shared-date proposal skipped combined confirmation")
            continue
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        expected_dates = [case["requested"], *case["deadlines"][1:]]
        errors += differences(expected_dates, [p["date"] for p in bank.ledger["promises"]], prefix + ".promise_dates")
        errors += differences([p["product_id"] for p in semantic_products],
                              [p["product_id"] for p in bank.ledger["promises"]], prefix + ".account_order")
        errors += differences("commitment", bank.ledger["outcome"], prefix + ".outcome")
        calls = [c["tool"] for c in bank.ledger["calls"]]
        if calls.count("close_call") != 1 or calls[-1] != "close_call" or bank.ledger["notes"]:
            errors.append(prefix + ": close-once-last/no-notes invariant failed")

        graph, bank = fresh(case["now"], [{"tool": "create_promise", "product_id": "random-u-716", "fail_first_n": 3}])
        graph.user("accept_offer")
        graph.user("confirm", explicit_yes=True)
        graph.finish()
        errors += differences(["random-r-482", "random-v-053"],
                              [p["product_id"] for p in bank.ledger["promises"]], prefix + ".partial_success")
        errors += differences([{"product_id": "random-u-716", "reason": "unavailable_service"}],
                              bank.ledger["notes"], prefix + ".changed_note_code")
        errors += differences(3, len([c for c in bank.ledger["calls"] if c["tool"] == "create_promise" and
                                     c["args"]["product_id"] == "random-u-716"]), prefix + ".changed_retry_limit")

        graph, bank = fresh(case["now"])
        graph.user("paid", product_id="random-r-482")
        graph.user("dispute", product_id="random-u-716")
        graph.user("paid", product_id="random-v-053")
        graph.finish()
        errors += differences([{"product_id": p, "reason": r} for p, r in
            [("random-r-482", "already_settled"), ("random-u-716", "contested"), ("random-v-053", "already_settled")]],
            bank.ledger["notes"], prefix + ".mixed_notes")
        errors += differences(["unresolved", "dispute"], [bank.ledger["outcome"], bank.ledger["reason"]],
                              prefix + ".first_nonpaid_semantic_outcome")

        graph, bank = fresh(case["now"])
        graph.user("paid", product_id="random-r-482")
        graph.user("accept_offer", product_id="random-u-716")
        graph.user("restructure")
        graph.finish()
        errors += differences([], bank.ledger["notes"], prefix + ".transfer_no_deferred_notes")
        errors += differences([], bank.ledger["promises"], prefix + ".transfer_no_agreed_writes")
        errors += differences([{"queue": "payment_options"}], bank.ledger["transfers"], prefix + ".transfer_code")

        graph, bank = fresh(case["now"])
        for product in semantic_products:
            graph.user("paid", product_id=product["product_id"])
        graph.finish()
        errors += differences("settled", bank.ledger["outcome"], prefix + ".all_paid")

        graph, bank = fresh(case["now"], verified=False)
        graph.user("person_yes")
        for index in range(4):
            graph.user("identity_answer", answer="WRONG", answer_complete=True)
            errors += differences("speak_identity" if index < 3 else "speak_farewell", graph.node,
                                  prefix + ".security_answer_" + str(index + 1))
        graph.finish()
        errors += differences(["verify_identity"] * 4 + ["close_call"],
                              [c["tool"] for c in bank.ledger["calls"]], prefix + ".security_cutoff_privacy")
        errors += differences(["identity_incomplete", "failed"],
                              [bank.ledger["outcome"], bank.ledger["reason"]], prefix + ".security_outcome")

        graph, bank = fresh(case["now"], verified=False)
        graph.user("other_person")
        for _ in range(2):
            graph.user("callback", day={"kind": "today"}, time="08:00")
            errors += differences("speak_callback", graph.node, prefix + ".callback_correction_budget")
        graph.user("callback", day={"kind": "iso", "value": case["next_business"]}, time="17:15")
        graph.finish()
        errors += differences([{"date": case["next_business"], "time": "17:15"}], bank.ledger["callbacks"],
                              prefix + ".callback_calendar_window")
        errors += differences(["schedule_callback", "close_call"],
                              [c["tool"] for c in bank.ledger["calls"]], prefix + ".callback_privacy")
    return errors


def check_case(pack, name, run):
    folder, run = Path(pack) / name, Path(run)
    expected = read_json(folder / "expected.json")
    errors = []
    if expected["status"] == "blocked":
        manifest = read_json(run / "manifest.json")
        errors += differences("blocked", manifest["status"], "extraction.status")
        blockers = read_json(run / "blockers.json") if (run / "blockers.json").exists() else []
        if not any(b.get("kind") == expected["blocker_kind"] for b in blockers):
            errors.append("Expected a source-backed contradiction blocker")
        if (run / "policy.json").exists() or (run / "compiled/workflow.json").exists():
            errors.append("A blocked case emitted an accepted policy or workflow")
    else:
        compiled = run / "compiled"
        actual_hash = hashlib.sha256((compiled / "workflow.json").read_bytes()).hexdigest()
        manifest = read_json(compiled / "manifest.json")
        if manifest.get("workflow_sha256") != actual_hash:
            errors.append("Generated workflow changed after compilation")
        raw = read_json(run / "compiled/policy.json")
        errors += differences(expected["policy"], raw)
        plan = read_json(run / "compiled/dialogue-plan.json")["plans"][0]
        for key, value in expected["plan"].items():
            actual = plan["messages"][key] if key == "recorded_promise" else plan[key]
            errors += differences(value, actual, "plan." + key)
        if name in {"api_identity_change", "raw_packet"}:
            bindings = read_json(run / "compiled/bindings.json")
            for tool, binding in bindings.items():
                for role, field in binding["request"].items():
                    errors += differences(REQUEST_NAMES[role], field, tool + "." + role)
                errors += differences("payload.accepted", binding["response"]["ok"], tool + ".ok")
        errors += (raw_runtime_audit if name == "raw_packet" else runtime_audit)(folder, run, expected)
    return {"status": "passed" if not errors else "failed", "case": name, "failed": errors,
            "scope": "synthetic_contradiction_detection" if expected["status"] == "blocked" else "synthetic_policy_semantics_and_emitted_code",
            "generated_code_scenarios": 0 if expected["status"] == "blocked" else 21 if name == "raw_packet" else 7,
            "official_freya_tests": False,
            "speech_extraction_routing_models_tested": False, "oracle_sent_to_builder": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "check"))
    parser.add_argument("--pack")
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--out", required=True)
    parser.add_argument("--run")
    parser.add_argument("--model")
    parser.add_argument("--codex-exe")
    parser.add_argument("--codex-home")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare_pack(args.out)))
        return 0
    if not args.pack or not args.case:
        parser.error("--pack and --case are required")
    if args.command == "run":
        if not args.model:
            parser.error("--model is required for explicit quota-consuming runs")
        case = next(c for c in read_json(Path(args.pack) / "pack.json")["cases"] if c["name"] == args.case)
        folder = Path(args.pack) / args.case
        provider = CodexProvider(args.model, args.codex_exe, args.codex_home)
        started = time.monotonic()
        try:
            function = run_extraction if args.case == "conflicting_memos" else run_build
            function([folder / p for p in case["briefs"]], folder / case["api"], args.out, provider, args.timeout)
        except (InvalidInput, ProviderError, OSError, ValueError, KeyError, TypeError) as exc:
            print(json.dumps({"status": "builder_failed", "error_type": type(exc).__name__,
                              "message": str(exc) if isinstance(exc, (InvalidInput, ProviderError)) else "Inspect run manifest"}))
            return 2
        write_json(Path(args.out) / "eval-inputs.json", case)
        report = check_case(args.pack, args.case, args.out)
        report["seconds"] = round(time.monotonic() - started, 3)
        write_json(Path(args.out) / "evaluation.json", report)
    else:
        if not args.run:
            parser.error("--run is required")
        report = check_case(args.pack, args.case, args.run)
        write_json(Path(args.out), report)
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
