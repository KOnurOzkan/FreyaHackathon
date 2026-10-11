"""Source-backed parameters for the supported conversation controller."""
from .contracts import validate_value
from .extraction import obj, array, string, INTEGER, REFERENCES, make_prompt, REQUEST_ROLES
from .inputs import InvalidInput, validate_evidence


class PlanStructureError(InvalidInput):
    """An incomplete citation/response shape, not unsupported bank behavior."""


COUNTERS = ("person_confirmation_attempts", "callback_corrections", "privacy_explanations",
            "talk_refusal_limit", "vague_date_clarifications", "floor_offers", "human_help_offers")
STRATEGIES = {
    "initial_payment": {"each_product_today", "all_products_today"},
    "shared_date": {"offer_deadline", "cap_per_product"},
    "confirmation": {"per_product", "combined"},
    "product_order": {"returned_order"},
    "first_persuasion": {"last_business_day_this_or_next_week"},
    "second_persuasion": {"floor_on_deadline"},
    "hardship_priority": {"before_persuasion_and_restructuring"},
    "numeric_date_order": {"day_month_year", "month_day_year", "year_month_day", "not_applicable"},
}
MESSAGES = ("opening", "recorded_promise", "deceased", "farewell", "identity_wrong",
            "identity_incomplete", "identity_failed", "privacy_explanation", "ai_disclosure",
            "scam_response", "paid_explanation", "recording_failed")
STAGES = ("person", "identity", "callback", "all_today", "negotiate", "confirm", "support", "human_help", "progress", "farewell")
# Plain sentence templates, not executable model output. Only these named slots
# may be substituted, and the controller supplies every value.
SPEECH_SLOTS = {
    "person_retry": {"title", "last_name"}, "callback_ask": set(),
    "callback_window": {"today", "next_business_day", "start", "end"},
    "account_intro": set(), "account_product": {"product", "amount", "days_late"},
    "floor_answer": {"product", "amount"}, "deadline_answer": {"product", "date"},
    "all_today": set(), "offer_today": {"product", "amount"},
    "offer": {"product", "amount", "date"}, "floor_notice": set(),
    "below_floor": set(), "amount_too_high": set(), "date_nonbusiness": {"requested"},
    "date_deadline": {"date"}, "date_past": set(), "date_clarify": set(),
    "clarify": set(), "clarify_product": set(),
    "confirm_item": {"product", "amount", "date"}, "confirm_question": set(),
    "adjustment": {"product", "requested", "date"}, "support": set(),
    "human_help": set(), "unknown_information": set(), "recorded_change": set(),
    "callback_success": {"date", "time"}, "callback_failed": set(),
    "transfer_success": set(), "verification_unavailable": set(),
    "dispute": set(), "refusal_talk": set(), "abort": set(),
}
PLAN_KEYS = set(COUNTERS) | set(STRATEGIES) | {"messages", "verification_questions", "transfer_forbids_notes", "stage_instructions", "identity_hint_messages", "speech_templates", "product_labels", "month_names"}


def plan_schema():
    fields = {key: INTEGER for key in COUNTERS}
    fields.update({key: string(values) for key, values in STRATEGIES.items()})
    fields.update({"messages": obj({key: string() for key in MESSAGES}),
                   "speech_templates": obj({key: string() for key in SPEECH_SLOTS}),
                   "product_labels": array(obj({"type": string(), "label": string(), "suffix_field": string(), "suffix_template": string()})),
                   "month_names": array(string()),
                   "identity_hint_messages": array(obj({"hint": string(), "message": string()})),
                   "stage_instructions": obj({key: string() for key in STAGES}),
                   "verification_questions": array(obj({"field": string(), "question": string(),
                                                         "normalization": string({"date", "last_four", "year", "digits", "text"})})),
                   "transfer_forbids_notes": {"type": "boolean"},
                   "evidence": array(obj({"section": string(PLAN_KEYS), "sources": REFERENCES}))})
    return obj({"plans": array(obj(fields)), "unsupported": array(obj({"message": string(), "sources": REFERENCES})),
                "documented_no_effect_failures": array(obj({"tool": string(REQUEST_ROLES), "status_code": INTEGER, "quote": string()}))})


def plan_prompt(documents, contracts, extraction):
    import json
    # This call does not get development fixtures, public customer cases, or credentials.
    return ("Extract conversation-controller parameters from the DATA in the following source bundle. "
            "Use no tools. The earlier extraction instructions below explain source conventions only; "
            "return this call's supplied dialogue-plan schema, not the earlier extraction format.\n" +
            make_prompt(documents, contracts) + "\nEARLIER SOURCE-BACKED INVENTORY:\n" +
            json.dumps(extraction["requirements"], ensure_ascii=False) + "\nDIALOGUE PLAN TASK:\n"
            "ALWAYS return exactly one effective plan. Use only strategies in the schema. When a source rule "
            "cannot be represented exactly by these controller strategies or counters, choose the closest "
            "supported behavior that never weakens privacy, identity verification, consent before any write, "
            "or honesty, and list each such approximation in unsupported with exact citations: they are "
            "reported gaps, not a reason to return no plan. Do not guess missing facts. Policy values and exact "
            "wording must come from input, never remembered practice rules. File order is not precedence. "
            "All plan sections require nonempty exact brief citations in evidence. "
            "Include exactly one evidence entry for EACH schema plan section, including booleans; "
            "check the evidence section list against all plan fields before returning.\n"
            "ALL customer speech is now prepared by code. speech_templates are short, complete SPEAKABLE sentences, "
            "never instructions. Use exactly the named [[slot]] placeholders specified below, each at least once, "
            "with no other placeholders or literal digits. Policy-specific verbal explanations still come from these sources. "
            "Offers and confirm_item describe a PROPOSED payment only, never a completed or recorded action. "
            "confirm_question asks the exact confirmation question required by the source. account_intro contains the "
            "required verified calling-purpose disclosure; account_product describes one returned account. "
            "floor_answer states the smallest recordable payment for [[product]] as [[amount]], using the source's "
            "floor rule; deadline_answer states the latest recordable payment date for [[product]] as [[date]], "
            "using the source's deadline rule. Both are factual answers only: no payment offer, question, demand, "
            "new term or claim of an agreed/recorded promise. Their values are supplied by verified runtime facts. all_today asks "
            "about paying all announced products today. offer_today includes the initial today question. offer names a "
            "product, amount and exact date. floor_notice explains what remains overdue. below_floor rejects a proposed "
            "amount below the allowed floor. amount_too_high explains that only these arrears are covered. date_nonbusiness "
            "explains [[requested]] is not a business day; date_deadline states the latest recordable date. Neither asks "
            "another question: code appends the valid offer. date_past and date_clarify ask for an exact valid date, "
            "never for an amount. adjustment explains a product's cap from [[requested]] to [[date]]. support includes "
            "sympathy and the source-authorized support offer. human_help asks the source-authorized help question. "
            "unknown_information admits missing information; recorded_change gives only source-authorized modification "
            "advice. callback_success and transfer_success are used ONLY after bank success. verification_unavailable "
            "describes a service failure without saying the answer was wrong. abort says technical trouble prevents "
            "reliable completion, without claiming any bank action failed or succeeded. No template may invent a "
            "customer fact, bank term, balance, date or product code. Unused template branches still need honest neutral "
            "wording, not invented policies. Template slot specification: " + json.dumps({k: sorted(v) for k,v in SPEECH_SLOTS.items()}) + "\n"
            "product_labels contains one entry per effective product type from the API/policy. label is its ordinary "
            "spoken name with no digits. suffix_field is an actual optional per-product API response field needed to "
            "identify the product (e.g. last digits), or an empty string. suffix_template is a phrase containing exactly "
            "[[suffix]] when suffix_field is nonempty, otherwise empty. Do not invent suffix values. month_names is "
            "the twelve calendar month names in order, in the source's conversation language. Cite the source product "
            "presentation and date obligations for these sections.\n"
            "person_confirmation_attempts counts ALL asks including the first opening ask. "
            "callback_corrections counts extra opportunities after an invalid callback proposal. "
            "privacy_explanations counts explanations allowed after security refusal. "
            "talk_refusal_limit counts explicit refusals to talk. vague_date_clarifications counts "
            "exact-date asks before offering the deadline. floor_offers is the single-offer limit "
            "outside numbered persuasion attempts. human_help_offers counts offers to help before "
            "an insistent human request is transferred. Extract these counters from source.\n"
            "messages.opening is a complete speakable opening plus the person-confirmation question. "
            "Use only supplied call-variable placeholders in double braces. Include all required "
            "initial disclosures, no account details. Words the sources reserve for completed bank actions "
            "(such as recorded, scheduled, confirmed) must not appear in any sense before such an action "
            "succeeds, because checks read them literally: disclose call recording with an active verb, for "
            "example 'We record this call.' recorded_promise and deceased preserve exact "
            "mandatory source wording when specified. farewell thanks and says goodbye without "
            "claiming unconfirmed actions. Compose concise wording when the brief specifies meaning "
            "rather than an exact sentence. verification_questions is in the policy's required order, "
            "with actual API identity field VALUES and speakable questions. Choose each answer's "
            "normalization from the source and API: date for a full calendar date, last_four for exactly "
            "four digits, year for a four-digit calendar year, digits for any other code made only of "
            "digits (leading zeros kept), and text for anything that may contain letters. Set numeric_date_order "
            "to not_applicable when NONE of those questions uses date normalization; a date-order "
            "rule is then unused and its absence is not missing policy information. If any question "
            "does normalize a date, extract a supported date order from the source.\n"
            "The extra messages are complete, concise SPEAKABLE sentences, not instructions. "
            "identity_wrong acknowledges a failed answer WITHOUT claiming exhaustion or specifying a count; "
            "identity_incomplete asks for clarification of an unreadable or incomplete answer WITHOUT "
            "claiming the bank rejected it or requesting a different question. identity_failed supplies "
            "all source-required directions after authoritative exhaustion, with no account/debt/payment "
            "disclosure or new question. privacy_explanation explains the source's security purpose. "
            "ai_disclosure answers a question about being an AI. scam_response follows the source's "
            "verification advice. paid_explanation gives all required processing-delay information, "
            "without asserting the customer's payment was verified. recording_failed truthfully says "
            "the pending promise could not be recorded, without claiming earlier successes were lost. "
            "These messages contain no customer facts, counters, tool names or internal policy wording. "
            "The runtime appends the current security question or farewell where appropriate. "
            "identity_hint_messages has exactly one entry for each effective noncounting hint; its message "
            "states the source-required correction, not a failed-attempt claim. Use [] if no hints apply.\n"
            "stage_instructions gives concise, source-backed dialogue instructions for EACH named stage. "
            "Carry every relevant narrative obligation into the stage where it must be spoken, rather than "
            "relying on the global inventory. Include required self/purpose disclosures, product naming and "
            "presentation order/granularity, mandatory questions and read-back wording, sympathy, explanations, "
            "failure directions and closing requirements where applicable. Preserve exact required phrases. "
            "Account-presentation obligations belong in all_today and the FIRST account presentation in "
            "negotiate; subsequent negotiation should not repeat that introduction. For unused stages give "
            "a short instruction to follow the current controller state. These are instructions, not invented "
            "customer data: reference authoritative runtime state for names, products, amounts and dates. "
            "Never strengthen an offer or request into a compulsory requirement, threat or invented term. "
            "Do not instruct dialogue to advance stages or perform bank actions; routing and controller "
            "own them. Cite the source obligations under stage_instructions evidence.\n"
            "The controller supports ordered identity checks, supplied-now dates, per-product "
            "limits, full/floor negotiation, voluntary hardship promises, paid/dispute continuation, "
            "combined or per-product confirmations, bounded documented promise failure retries, "
            "and deferred notes with transfer precedence. It does not implement arbitrary new "
            "negotiation strategies. Approximate an unsupported strategy with the closest supported one and "
            "list it in unsupported; never approximate silently. Keep evidence quotes short and exact.")


def complete_plan_prompt(documents, contracts, extraction):
    return plan_prompt(documents, contracts, extraction) + (
        "\nAlso populate documented_no_effect_failures ONLY for non-success HTTP statuses whose "
        "supplied API response description explicitly guarantees that nothing was recorded and "
        "permits retry. Quote that response description exactly; this evidence is from the API, "
        "not a brief. An empty list is correct if there is no such explicit guarantee. Unknown "
        "transport outcomes must never be classified as known no-effect failures.")


def validate_plan(raw, documents, policy, contracts=None):
    try:
        validate_value(raw, plan_schema())
    except InvalidInput as exc:
        # Best effort: malformed model output gets the one structural repair
        # call (with this feedback) instead of stopping the build.
        raise PlanStructureError(str(exc)) from exc
    by_id = {d.id: d for d in documents}

    def evidence(references):
        if not references:
            raise InvalidInput("Dialogue plan evidence cannot be empty")
        for ref in references:
            if ref["document"] not in by_id:
                raise InvalidInput("Unknown dialogue plan source")
            validate_evidence({**ref, "sha256": by_id[ref["document"]].sha256}, documents)

    # Best effort (user decision, 11 October): an unsupported rule is approximated
    # by the closest supported behavior and reported, never a reason to emit no
    # agent. compile_run records these gaps in the manifest and trace.
    if len(raw["plans"]) != 1:
        raise PlanStructureError("Return exactly one effective dialogue plan; approximate any rule the "
                                 "supported strategies cannot represent and list it in unsupported")
    plan = raw["plans"][0]
    sections = [item["section"] for item in plan["evidence"]]
    if set(sections) != PLAN_KEYS or len(sections) != len(set(sections)):
        missing = sorted(PLAN_KEYS - set(sections))
        duplicates = sorted({section for section in sections if sections.count(section) > 1})
        raise PlanStructureError("Every dialogue parameter section needs unique source evidence; "
                                 "missing sections: " + repr(missing) + "; duplicate sections: " + repr(duplicates))
    for item in plan["evidence"]:
        try:
            evidence(item["sources"])
        except InvalidInput as exc:
            raise PlanStructureError("Dialogue evidence section " + item["section"] + ": " + str(exc)) from exc
    for key in COUNTERS:
        lower = 1 if key in {"person_confirmation_attempts", "talk_refusal_limit"} else 0
        if not lower <= plan[key] <= 20:
            raise PlanStructureError("Dialogue counter outside the supported controller range")
    questions = plan["verification_questions"]
    if [q["field"] for q in questions] != list(policy.identity_fields):
        raise PlanStructureError("Dialogue questions do not match the extracted verification sequence")
    if any(q["normalization"] == "date" for q in questions) and plan["numeric_date_order"] == "not_applicable":
        raise PlanStructureError("Date-normalized security questions require a source-backed numeric date order")
    if any(not q["question"].strip() for q in questions) or any(not value.strip() for value in plan["messages"].values()):
        raise PlanStructureError("Dialogue messages must not be empty")
    hints = plan["identity_hint_messages"]
    if (len({h["hint"] for h in hints}) != len(hints) or
            {h["hint"] for h in hints} != set(policy.noncounting_hints) or
            any(not h["message"].strip() for h in hints)):
        raise PlanStructureError("Every effective noncounting hint needs its own source-backed message")
    if any(not value.strip() or len(value) > 6000 for value in plan["stage_instructions"].values()):
        raise PlanStructureError("Every dialogue stage needs concise, nonempty source-backed instructions")
    import re
    for key, slots in SPEECH_SLOTS.items():
        template = plan["speech_templates"][key]
        found = set(re.findall(r"\[\[([a-z_]+)\]\]", template))
        remainder = re.sub(r"\[\[[a-z_]+\]\]", "", template)
        if (found != slots or not template.strip() or len(template) > 1600 or
                re.search(r"[0-9{}\[\]]", remainder)):
            raise PlanStructureError("Speech template must use exactly its declared slots and no baked numeric facts: " + key)
        if key in {"offer", "offer_today", "confirm_item", "confirm_question"} and re.search(r"\b(recorded|scheduled|successfully|confirmed)\b", remainder, re.I):
            raise PlanStructureError("Pending-payment speech cannot claim completion: " + key)
        if key in {"floor_answer", "deadline_answer"} and ("?" in remainder or
                re.search(r"\b(recorded|scheduled|successfully|confirmed)\b", remainder, re.I)):
            raise PlanStructureError("Account fact answers cannot ask for payment or claim completion: " + key)
    months = plan["month_names"]
    if len(months) != 12 or len(set(months)) != 12 or any(not re.fullmatch(r"[^\W\d_]+", m, re.UNICODE) for m in months):
        raise PlanStructureError("Twelve distinct month names are required")
    labels = plan["product_labels"]
    if len(labels) != len(policy.floor_fields) or {p["type"] for p in labels} != set(policy.floor_fields):
        raise PlanStructureError("Every supported product type needs exactly one spoken label")
    for item in labels:
        if not item["label"].strip() or re.search(r"[0-9{}\[\]]", item["label"]):
            raise PlanStructureError("Invalid product label")
        if item["suffix_field"]:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item["suffix_field"]):
                raise PlanStructureError("Suffix must name a product field")
            if set(re.findall(r"\[\[([a-z_]+)\]\]", item["suffix_template"])) != {"suffix"}:
                raise PlanStructureError("Suffix template needs only its literal suffix slot")
            if re.search(r"[0-9{}\[\]]", item["suffix_template"].replace("[[suffix]]", "")):
                raise PlanStructureError("Suffix template contains a baked fact or unsupported placeholder")
            if contracts is not None:
                def has_product_field(schema):
                    if not isinstance(schema, dict):
                        return False
                    if schema.get("type") == "array" and item["suffix_field"] in schema.get("items", {}).get("properties", {}):
                        return True
                    return any(has_product_field(v) for v in schema.values() if isinstance(v, dict))
                if not has_product_field(contracts.operations["get_account"]["responses"]):
                    raise PlanStructureError("Product suffix field is absent from the supplied API")
        elif item["suffix_template"]:
            raise PlanStructureError("A suffix template requires a response field")
    # Best effort: combinations the controller does not implement use its
    # supported behavior instead of stopping the build; each is reported.
    coercions = []
    if policy.notes_enabled and not plan["transfer_forbids_notes"]:
        plan = {**plan, "transfer_forbids_notes": True}
        coercions.append("Notes around a transfer are not supported; no notes are written on any transfer.")
    if policy.max_products > 1 and plan["confirmation"] != "combined":
        plan = {**plan, "confirmation": "combined"}
        coercions.append("Per-product confirmation is not supported with several products; one combined read-back is used.")
    failures = {}
    for item in raw["documented_no_effect_failures"]:
        description = (contracts.operations[item["tool"]]["responses"].get(str(item["status_code"]), {}).get("description", "")
                       if contracts is not None else "")
        if not 400 <= item["status_code"] <= 599 or not item["quote"].strip() or item["quote"] != description:
            # Unverifiable: treat that status as an uncertain outcome (no retry).
            coercions.append("Ignored an unverifiable no-effect failure claim for " + item["tool"] + " status " +
                             str(item["status_code"]) + "; such a failure stops the call instead of retrying.")
            continue
        failures.setdefault(item["tool"], []).append(str(item["status_code"]))
    return {**plan, "failure_statuses": failures, "best_effort_coercions": coercions}
