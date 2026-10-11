"""Resumable semantic controller; no HTTP, file access, or model calls.

The compiler embeds this module and the tested Session guards into code nodes.
A journal replays already completed calls locally and suspends before a new one.
Only the workflow engine executes a newly prepared bank request.
"""
import json
import re
from datetime import date, datetime, timedelta, time

from .inputs import InvalidInput
from .rules import Calendar, supplied_now, iso_date, money, money_text
from .session import Session, boolean
from .contracts import validate_value


class PendingBankCall(BaseException):
    def __init__(self, tool, body):
        self.tool, self.body = tool, body


class ReplayBank:
    def __init__(self, results):
        self.results, self.position = results, 0

    def call(self, tool, body):
        if self.position == len(self.results):
            raise PendingBankCall(tool, body)
        recorded = self.results[self.position]
        self.position += 1
        if recorded["tool"] != tool or recorded["body"] != body:
            raise InvalidInput("Bank journal does not match the current decision history")
        return recorded["result"]


class RuntimePolicy:
    def __init__(self, raw):
        for key in ("floor_fields", "wrong_answer_limit", "promise_attempt_limit", "max_products",
                    "notes_enabled", "outcomes", "note_reasons", "transfer_queues"):
            setattr(self, key, raw[key])
        self.identity_fields = tuple(raw["identity_fields"])
        self.noncounting_hints = frozenset(raw["noncounting_hints"])
        self.weekdays = frozenset(raw["calendar"]["weekdays"])
        self.holidays = frozenset(date.fromisoformat(x) for x in raw["calendar"]["holidays"])
        self.callback_start = time.fromisoformat(raw["calendar"]["callback_start"])
        self.callback_end = time.fromisoformat(raw["calendar"]["callback_end"])
        self.bands = raw["bands"]

    def band_for(self, days):
        if type(days) is not int:
            raise InvalidInput("Invalid days late")
        matches = [b for b in self.bands if b["minimum"] <= days <= b["maximum"]]
        if len(matches) != 1:
            raise InvalidInput("No unambiguous product band")
        return RuntimeBand(matches[0])


class RuntimeBand:
    def __init__(self, raw):
        self.business_days, self.attempts = raw["business_days"], raw["attempts"]


def product_reference_text(value):
    """Normalize spoken digit sequences without guessing omitted suffix digits."""
    tokens = re.sub(r"[\W_]+", " ", str(value).lower()).strip().split()
    words = dict(zip("zero one two three four five six seven eight nine".split(), "0123456789"))
    words["oh"] = "0"
    output, digits = [], ""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        digit = words.get(token, token if re.fullmatch(r"[0-9]+", token) else "")
        if token in {"double", "triple"} and index + 1 < len(tokens):
            following = tokens[index + 1]
            single = words.get(following, following if re.fullmatch(r"[0-9]", following) else "")
            if single:
                digit = single * (2 if token == "double" else 3)
                index += 1
        if digit:
            digits += digit
        else:
            if digits:
                output.append(digits)
                digits = ""
            output.append(token)
        index += 1
    if digits:
        output.append(digits)
    return " ".join(output)


def referenced_payment_day(spoken, previous):
    """Resolve a reference only against a date already offered or read back."""
    if type(previous) is not date:
        raise InvalidInput("No prior payment date to reference")
    phrase = re.sub(r"[,.!?]", "", spoken).replace("-", " ").strip()
    phrase = re.sub(r"\s+", " ", phrase)
    if phrase in {"that date", "that day", "same date", "same day", "the same date", "the same day",
                  "that same date", "that same day", "the date you offered", "the date we agreed",
                  "as we said", "as you said", "as agreed", "as previously agreed"}:
        return previous
    match = re.fullmatch(r"(?:on )?(?:(?:that|the|same) )*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?: (?:the )?(.+))?", phrase)
    if not match:
        raise InvalidInput("Unclear reference to a payment date")
    names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    if names.index(match[1]) != previous.weekday():
        raise InvalidInput("Referenced weekday differs from the prior payment date")
    if match[2]:
        ordinals = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
                    "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
                    "eighteenth", "nineteenth", "twentieth", "twenty first", "twenty second", "twenty third",
                    "twenty fourth", "twenty fifth", "twenty sixth", "twenty seventh", "twenty eighth",
                    "twenty ninth", "thirtieth", "thirty first")
        number = re.fullmatch(r"([0-9]{1,2})(?:st|nd|rd|th)?", match[2])
        day = int(number[1]) if number else ordinals.index(match[2]) + 1 if match[2] in ordinals else None
        if day != previous.day:
            raise InvalidInput("Referenced day differs from the prior payment date")
    return previous


def payment_day(value, now, calendar, previous=None):
    if not isinstance(value, dict):
        raise InvalidInput("A structured date is required")
    kind = value.get("kind")
    # The model's enum was observed confusing "this" with "next". Preserve the
    # quoted phrase and compute these literal calendar-week expressions here.
    spoken = str(value.get("source_text", "")).strip().lower()
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    matches = re.findall(r"\b(this|next)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", spoken)
    if len(matches) == 1:
        which, name = matches[0]
        return calendar.weekday_date(now.date(), weekdays.index(name), which == "next")
    if kind == "offer_date" or kind in {"this_weekday", "next_weekday"} and not matches:
        return referenced_payment_day(spoken, previous)
    if kind in {"this_weekday", "next_weekday"} or len(matches) > 1:
        raise InvalidInput("A single quoted this/next weekday phrase is required")
    if kind == "iso":
        return iso_date(value.get("value"))
    if kind == "today":
        return now.date()
    if kind == "tomorrow":
        return now.date() + timedelta(days=1)
    raise InvalidInput("Date is missing or vague")


class Conversation:
    def __init__(self, policy, plan, now, bank):
        self.session = Session(policy, now, bank)
        self.plan = plan
        self.phase, self.hint = "person", "Confirm the person without disclosing account information."
        self.person_asks, self.privacy, self.talk_refusals, self.human_offers = 1, 0, 0, 0
        self.callback_invalid = 0
        self.callback_partial = {}
        self.identity_fragment = ""
        self.floor_offers, self.vague_dates, self.offers = {}, {}, {}
        self.prefer_floor = set()
        self.current = None
        self.collective_declined = False
        self.early_reason = None
        self.support_products = []
        self.support_offered = False
        self.support_proposal = None
        self.snapshot = None
        self.last_recorded = []
        self.pending_amounts = {}
        self.help_return = None
        self.identity_notice = ""
        self.closing_message = ""
        self.speech_mode = "person_retry"
        self.speech_prefix = []
        self.present_accounts = False
        self.offer_notices = {}
        self.adjustments = {}
        self.proposal_queue = []
        self.ambiguous_proposal = None
        self.ambiguous_phase = None

    def farewell(self, reason=None, hint=""):
        self.early_reason = reason
        self.phase = "farewell"
        self.hint = hint + " Give the required truthful final explanation, then thank the customer and say goodbye."
        if reason == "failed":
            self.closing_message = self.plan["messages"]["identity_failed"]
        elif reason == "deceased":
            self.closing_message = self.plan["messages"]["deceased"]

    def advance_product(self):
        s = self.session
        if self.ambiguous_proposal is not None:
            self.clarify_proposal_product()
            return
        if self.resume_proposals():
            return
        # Once hardship applies, unresolved products cannot receive a new offer.
        # Voluntary proposals have already become drafts before this boundary.
        for product in s.products:
            if product in s.hardship and product not in s.drafts and product not in s.reasons and product not in s.recorded:
                s.mark_unpromised(product, "hardship")
        unresolved = [p for p in s.products if p not in s.drafts and p not in s.reasons and p not in s.recorded]
        if unresolved:
            self.current = unresolved[0]
            self.phase = "negotiate"
            if self.collective_declined:
                attempt = s.persuasion_attempts.get(self.current, 0)
                if attempt:
                    # Resolving another product does not refuse this pending
                    # offer or authorize consuming another persuasion attempt.
                    self.speech_mode = "offer"
                    self.hint = "Resume the existing offer for this product without spending another persuasion attempt."
                else:
                    self.refusal()
            else:
                self.offers[self.current] = (s.products[self.current].past_due, s.now.date())
                self.speech_mode = "offer_today"
                self.hint = "Propose the full past-due payment today for the current product."
        elif s.drafts and any(p not in s.recorded and p not in s.reasons for p in s.drafts):
            self.snapshot = s.read_back()
            self.phase = "confirm"
            self.hint = "Read EVERY pending product, amount and date below, including date adjustments. Ask for clear confirmation."
        else:
            self.farewell()

    def refusal(self):
        s, product = self.session, self.current
        self.discard_proposals([product])
        self.pending_amounts.pop(product, None)
        if product in s.hardship:
            s.mark_unpromised(product, "hardship")
            self.advance_product()
            return
        attempt = s.refuse(product)
        if attempt is None:
            self.advance_product()
            return
        p = s.products[product]
        amount = p.floor if attempt > 1 or product in self.prefer_floor else p.past_due
        day = s.deadline(product) if attempt > 1 else s.calendar.week_end_offer(s.now.date())
        day = min(day, s.deadline(product))
        self.offers[product] = (amount, day)
        self.offer_notices.pop(product, None)
        # Every offer of the floor says what remains overdue (live PUB-06: a
        # repeated floor offer without it failed the brief's floor rule).
        notice = self.text("floor_notice")
        if amount == p.floor < p.past_due and notice not in self.speech_prefix:
            self.speech_prefix.append(notice)
        self.speech_mode = "offer"
        self.phase = "negotiate"
        self.hint = "Propose the current offer below. This is persuasion attempt " + str(attempt) + ". Never ask how much they can pay."

    def floor_offer(self, product, day=None):
        s = self.session
        self.pending_amounts.pop(product, None)
        self.current = product
        self.prefer_floor.add(product)
        count = self.floor_offers.get(product, 0)
        if count >= self.plan["floor_offers"]:
            self.refusal()
            return
        self.floor_offers[product] = count + 1
        self.offers[product] = (s.products[product].floor, day or self.offers.get(product, (0, s.now.date()))[1])
        # An amount change cannot introduce an invalid offered date.
        offered = self.offers[product][1]
        if offered < s.now.date():
            offered = s.now.date()
        self.offers[product] = (s.products[product].floor, s.calendar.suggest_promise(offered, s.now.date(), s.deadline(product)))
        self.speech_mode = "offer"
        self.offer_notices.pop(product, None)
        self.speech_prefix.append(self.text("floor_notice"))
        self.phase = "negotiate"
        self.hint = "Offer the allowed floor once and explain that the remainder stays overdue."

    def proposals(self, event, accept=False):
        s = self.session
        if not s.products:
            raise InvalidInput("Payment cannot be discussed before account lookup")
        origin = self.ambiguous_phase or self.phase
        accept_all = accept and self.phase == "all_today" and self.speech_mode == "offer_today"
        target_known = event.get("scope") == "all" or event.get("product_id") in s.products
        needs_reference = len(s.products) > 1 and (origin in {"all_today", "confirm"} or self.ambiguous_proposal is not None)
        ambiguous = (event.get("product_ambiguous") is True or
                     (event.get("product_id") is not None and event.get("product_id") not in s.products) or
                     (needs_reference and event.get("scope") != "all" and event.get("product_id") in s.products and
                      not self.grounded_product_reference(event["product_id"], event.get("product_reference"))) or
                     (not target_known and not accept_all and
                      (self.ambiguous_proposal is not None or len(s.products) > 1 and self.phase in {"all_today", "confirm"})))
        if ambiguous:
            self.ambiguous_phase = origin
            self.ambiguous_proposal = self.merge_proposal_terms(self.ambiguous_proposal or {}, event)
            for key in ("product_id", "scope", "product_ambiguous", "product_reference"):
                self.ambiguous_proposal.pop(key, None)
            self.clarify_proposal_product()
            return
        if self.ambiguous_proposal is not None:
            event = self.merge_proposal_terms(self.ambiguous_proposal, event)
            self.clear_ambiguous_proposal()
        ids = list(s.products) if event.get("scope") == "all" or (accept and self.phase == "all_today" and self.speech_mode == "offer_today") else [event.get("product_id") or self.current]
        if not ids or any(p not in s.products or p in s.recorded for p in ids):
            if self.phase == "confirm":
                self.phase = "negotiate"
                s.readback_snapshot = s.confirmed_snapshot = self.snapshot = None
            self.speech_mode = "clarify_product"
            self.hint = "Clarify which unrecorded product the customer means; recorded promises cannot be changed in this call."
            return
        self.collective_declined = self.collective_declined or origin == "all_today"
        correcting_readback = origin == "confirm"
        previous_drafts = {product: s.drafts.get(product) for product in ids}
        previous_queue = {item["product"]: item for item in self.proposal_queue}
        prepared = []
        try:
            # Resolve every target before changing drafts. A shared correction
            # may pause at one adjusted offer without losing later products or
            # their distinct previously agreed amounts.
            amount_kind = event.get("amount_kind", "offer")
            if not isinstance(amount_kind, str) or amount_kind not in {"full", "floor", "offer", "explicit"}:
                raise InvalidInput("Unknown proposed amount kind needs clarification")
            day_spec = event.get("day", {})
            if not isinstance(day_spec, dict):
                raise InvalidInput("A structured date is required")
            for product in ids:
                p = s.products[product]
                queued = previous_queue.get(product)
                prior = self.offers.get(product, (p.past_due, s.now.date()))
                if queued and not queued.get("waiting") and not accept:
                    prior = (queued["amount"], queued["day"])
                draft = previous_drafts[product]
                if draft is not None and not accept:
                    prior = (draft.cents, iso_date(draft.date))
                amount = p.floor if amount_kind == "floor" else prior[0] if amount_kind == "offer" else p.past_due
                if not accept and "amount_kind" not in event and product in self.pending_amounts:
                    amount = self.pending_amounts[product]
                if amount_kind == "explicit":
                    amount = money(event.get("amount"))
                elif "amount" in event and money(event["amount"]) != amount:
                    raise InvalidInput("The amount label contradicts the supplied numeric amount")
                if (accept or correcting_readback and draft is not None or
                        event.get("action") == "correction" and (draft is not None or queued)) and not day_spec:
                    day = prior[1]
                elif not day_spec or day_spec.get("kind") == "vague":
                    day = None
                else:
                    day = payment_day(day_spec, s.now, s.calendar, prior[1])
                shared = len(ids) > 1 and self.plan["shared_date"] == "cap_per_product"
                prepared.append(dict(product=product, amount=amount, day=day, shared=shared))
            contextual = (day_spec.get("kind") == "offer_date" or
                          day_spec.get("kind") in {"this_weekday", "next_weekday"} and
                          not re.search(r"\b(this|next)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                                        str(day_spec.get("source_text", "")).lower()))
            if contextual and len({item["day"] for item in prepared}) > 1:
                raise InvalidInput("A shared date reference needs one unambiguous prior date")
        except (InvalidInput, ValueError, TypeError):
            # An unsupported correction still withdraws the targeted old terms.
            # A later yes cannot accidentally confirm a stale read-back.
            for product in ids:
                s.drafts.pop(product, None)
            s.revision += 1
            self.discard_proposals(ids)
            self.phase, self.current = "negotiate", ids[0]
            raise
        for product in ids:
            s.drafts.pop(product, None)
            s.reasons.pop(product, None)
            if "amount_kind" in event:
                self.pending_amounts.pop(product, None)
        s.revision += 1
        self.discard_proposals(ids)
        replacements = {item["product"]: item for item in self.proposal_queue}
        replacements.update({item["product"]: item for item in prepared})
        self.proposal_queue = [replacements[p] for p in s.products if p in replacements]
        if len(ids) == 1:
            self.current = ids[0]
        self.phase = "negotiate"
        self.advance_product()

    def discard_proposals(self, products=None):
        """Withdraw queued terms, including their authority to a prior read-back."""
        if products is None:
            self.clear_ambiguous_proposal()
        self.proposal_queue = ([] if products is None else
                               [item for item in self.proposal_queue if item["product"] not in products])
        self.session.readback_snapshot = self.session.confirmed_snapshot = self.snapshot = None

    def clear_ambiguous_proposal(self):
        self.ambiguous_proposal = self.ambiguous_phase = None

    def merge_proposal_terms(self, prior, event):
        merged = {**prior, **event}
        if "amount_kind" in event and event["amount_kind"] != "explicit":
            merged.pop("amount", None)
        if event.get("scope") == "all":
            merged.pop("product_id", None)
        elif "product_id" in event:
            merged["scope"] = "one"
        return merged

    def clarify_proposal_product(self):
        self.session.readback_snapshot = self.session.confirmed_snapshot = self.snapshot = None
        self.phase, self.speech_mode = "negotiate", "clarify_product"
        self.hint = "Clarify which authoritative product or explicitly all products the proposed terms apply to; retain the amount and date while asking."

    def grounded_product_reference(self, product, reference):
        """Require one account-derived label or identifier in a quoted reference."""
        if not isinstance(reference, str) or not reference.strip():
            return False
        def normalize(value):
            return re.sub(r"[\W_]+", " ", str(value).lower()).strip()
        normalized = product_reference_text(reference)
        quoted = " " + normalized + " "
        numbers = re.findall(r"\b[0-9]+\b", normalized)
        def contains(value):
            phrase = normalize(value)
            return bool(phrase) and " " + phrase + " " in quoted
        labels, suffixes, label_types = set(), set(), set()
        has_suffix_marker = False
        for candidate, account in self.session.products.items():
            spec = next(item for item in self.plan["product_labels"] if item["type"] == account.type)
            if contains(spec["label"]) or contains(self.product_label(candidate)):
                labels.add(candidate)
                label_types.add(account.type)
            if spec["suffix_field"]:
                raw = next(item for item in self.session.account_facts if item["product_id"] == candidate)
                suffix = raw.get("_fb_wire", raw).get(spec["suffix_field"], "")
                if contains(suffix) and (not str(suffix).isdigit() or numbers == [str(suffix)]):
                    suffixes.add(candidate)
                marker = spec["suffix_template"].replace("[[suffix]]", " ")
                has_suffix_marker = has_suffix_marker or contains(marker)
        if len(label_types) > 1:
            return False
        if numbers or has_suffix_marker or suffixes:
            matches = labels & suffixes if labels else suffixes
        else:
            matches = labels
        return matches == {product}

    def resume_proposals(self):
        """Stage valid terms in account order, pausing only for a required answer."""
        s = self.session
        while self.proposal_queue:
            item = self.proposal_queue[0]
            product, amount, day = item["product"], item["amount"], item["day"]
            if product in s.recorded or product in s.reasons:
                self.proposal_queue.pop(0)
                continue
            if item.get("waiting"):
                self.current, self.phase = product, "negotiate"
                self.speech_mode, self.hint = item["mode"], item["hint"]
                return True
            p = s.products[product]
            if p.floor <= amount <= p.past_due:
                self.pending_amounts[product] = amount
            if amount < p.floor:
                self.speech_prefix.append(self.text("below_floor"))
                self.floor_offer(product, day)
                if item not in self.proposal_queue:
                    return True
            elif day is None:
                self.pending_amounts[product] = amount
                count = self.vague_dates.get(product, 0)
                if count < self.plan["vague_date_clarifications"]:
                    self.vague_dates[product] = count + 1
                    self.speech_mode = "date_clarify"
                    self.hint = "Ask for the exact payment day, without treating the proposal as a refusal."
                else:
                    self.offers[product] = (max(p.floor, min(amount, p.past_due)), s.deadline(product))
                    self.speech_mode = "offer"
                    self.offer_notices[product] = ("date_deadline", s.deadline(product))
                    self.hint = "Offer the product deadline as the payment date."
            elif amount > p.past_due:
                self.offers[product] = (p.past_due, day)
                self.speech_mode = "offer"
                self.speech_prefix.append(self.text("amount_too_high"))
                self.offers[product] = (p.past_due, s.calendar.suggest_promise(max(day, s.now.date()), s.now.date(), s.deadline(product)))
                self.phase, self.hint = "negotiate", "The amount exceeds these arrears; offer the actual full past-due amount."
            elif day < s.now.date():
                self.pending_amounts[product] = amount
                self.speech_mode = "date_past"
                self.phase, self.hint = "negotiate", "That date is in the past; ask for a valid payment date."
            else:
                adjusted = s.calendar.suggest_promise(day, s.now.date(), s.deadline(product))
                shared_cap = item["shared"] and day > s.deadline(product)
                if adjusted != day and not shared_cap:
                    self.offers[product] = (amount, adjusted)
                    self.speech_mode = "offer"
                    self.offer_notices[product] = ("date_deadline" if day > s.deadline(product) else "date_nonbusiness", day)
                    self.hint = "Explain the date restriction and offer the valid adjusted date below; obtain agreement."
                else:
                    s.propose(product, money_text(amount), adjusted.isoformat(), voluntary=product in s.hardship)
                    self.offers[product] = (amount, adjusted)
                    self.pending_amounts.pop(product, None)
                    self.offer_notices.pop(product, None)
                    if shared_cap:
                        self.adjustments[product] = day
                    else:
                        self.adjustments.pop(product, None)
                    self.proposal_queue.pop(0)
                    continue
            self.current, self.phase = product, "negotiate"
            item.update(waiting=True, mode=self.speech_mode, hint=self.hint)
            return True
        return False

    def consume_support_proposal(self, event):
        """Merge a correction into the held proposal once, without changing its target."""
        prior = self.support_proposal or {}
        self.support_proposal = None
        if ((event.get("product_id") and event["product_id"] != prior.get("product_id", self.current)) or
                ("scope" in event and event["scope"] != prior.get("scope", "one"))):
            prior = {}
        merged = {**prior, **event, "action": "payment"}
        if "product_id" in event and "scope" not in event:
            merged["scope"] = "one"
        if event.get("scope") == "all":
            merged.pop("product_id", None)
        if "amount_kind" in event and event["amount_kind"] != "explicit":
            merged.pop("amount", None)
        return merged

    def record_next(self):
        s = self.session
        remaining = [p for p in s.products if p in s.drafts and p not in s.recorded and p not in s.reasons]
        if not remaining:
            self.farewell()
            return
        product = remaining[0]
        while s.write_attempts.get(product, 0) < s.policy.promise_attempt_limit:
            if s.record(product):
                self.last_recorded = [product]
                self.phase = "progress"
                self.hint = "This promise was successfully recorded. Say the exact required recorded-promise message. Remaining work continues automatically after this acknowledgment."
                return
        self.last_recorded = []
        self.phase = "progress"
        self.hint = "Recording this promise failed after the allowed attempts. Say that clearly; retain successful promises. Then continue."

    def handle(self, event):
        s, plan = self.session, self.plan
        # A bounded vocabulary alias, not a guessed amount or consent. The
        # authoritative floor still comes from the returned product and policy.
        if event.get("amount_kind") == "minimum":
            event = {**event, "amount_kind": "floor"}
        if "amount" in event and "amount_kind" not in event:
            event = {**event, "amount_kind": "explicit"}
        if (s.products and "product_id" not in event and event.get("scope") != "all" and
                isinstance(event.get("product_reference"), str)):
            # A quoted reference that grounds to exactly one product supplies
            # the omitted ID; an ambiguous or ungrounded one still clarifies.
            match = [p for p in s.products if self.grounded_product_reference(p, event["product_reference"])]
            if len(match) == 1:
                event = {**event, "product_id": match[0]}
        self.identity_notice = ""
        self.speech_prefix = []
        self.present_accounts = False
        action = event.get("action", "unparsed")
        # Extraction labels cannot skip the offer -> read-back -> confirm order.
        # A yes at an offer can accept that offer, but cannot authorize a write.
        changed = any(k in event for k in ("day", "amount", "amount_kind"))
        if action == "confirm" and changed:
            action = "correction"
            event = {**event, "action": action}
        elif action == "confirm" and self.phase in {"negotiate", "all_today"} and event.get("explicit_yes") is True:
            if self.phase == "all_today" or self.speech_mode in {"offer", "offer_today"}:
                action = "accept_offer"
                event = {**event, "action": action}
        if action == "defer":
            # "You tell me" to a persuasion offer that already names amount and
            # date defers to that offer: read it back (recording still needs an
            # explicit yes). Elsewhere it is a non-answer (rule 30). Live PUB-06:
            # treating it as a non-answer made a second floor offer.
            current = self.current
            action = ("accept_offer" if self.phase == "negotiate" and self.speech_mode == "offer" and not changed
                      and current in self.offers and s.persuasion_attempts.get(current, 0) >= 1 else "unknown")
            event = {**event, "action": action}
        if s.closed:
            self.phase = "end"
            return
        if (self.phase == "negotiate" and self.speech_mode == "date_clarify" and
                action in {"confirm", "accept_offer", "unknown", "unparsed"} and not event.get("day")):
            # This reply supplies no exact day. Consume the configured date
            # clarification allowance; it neither accepts an old offer nor
            # spends a persuasion attempt. Keep the customer's pending amount.
            action = "vague_date"
            event = {**event, "action": action, "day": dict(kind="vague")}
        if (action in {"confirm", "accept_offer"} and not changed and
                self.phase in {"all_today", "negotiate"} and self.speech_mode not in {"offer", "offer_today"}):
            self.speech_mode = "clarify"
            self.hint = "A yes to a clarification does not accept a payment. Clarify the missing product, amount or date."
            return
        if action == "finalize":
            if self.phase != "farewell":
                raise InvalidInput("Farewell must precede final bank operations")
            s.finish(self.early_reason)
            self.phase = "end"
            return
        if action == "continue":
            if self.phase != "progress":
                raise InvalidInput("No acknowledged recording to continue")
            self.record_next()
            return
        if action == "unparsed":
            if self.phase == "person":
                # Uncertain extraction cannot create unlimited identity asks.
                action = "person_unclear"
            else:
                if self.phase in {"all_today", "negotiate"}:
                    self.speech_mode = "clarify"
                else:
                    self.speech_prefix.append(self.text("clarify"))
                self.hint = "The last response was not understood reliably. Ask a short clarification of the current question."
                self.identity_notice = plan["messages"]["identity_incomplete"]
                return
        if s.transfer_kind or s.callback_recorded:
            self.farewell(self.early_reason, "The previous transfer or callback succeeded. Answer any question truthfully, without performing another bank action.")
            return
        # Interrupts are handled centrally before any negotiation or confirmation.
        if action == "human":
            if self.human_offers < plan["human_help_offers"]:
                self.human_offers += 1
                self.help_return = (self.phase, self.hint, self.speech_mode)
                self.phase = "human_help"
                self.hint = "Ask once whether you can help before transferring to a human."
            elif s.transfer("human"):
                self.discard_proposals()
                self.farewell(hint="The requested human transfer succeeded.")
            else:
                raise InvalidInput("Human transfer failed; do not claim transfer success")
            return
        if self.phase == "human_help":
            self.phase, self.hint, self.speech_mode = self.help_return
            self.help_return = None
            if action == "help_yes":
                return
        # Resolve an explicit answer to the pending support offer before a
        # secondary question/plan intent. Preserve voluntary payment proposals.
        support_decision = event.get("support_decision")
        if action == "support_no" or event.get("support_no") is True:
            support_decision = "decline"
        elif action == "support_yes":
            support_decision = "accept"
        if self.support_offered and support_decision == "decline":
            proposal = (self.consume_support_proposal(event)
                        if event.get("day") or event.get("amount_kind") or event.get("amount") or self.support_proposal
                        else None)
            if proposal:
                try:
                    self.proposals(proposal)
                except (InvalidInput, ValueError, TypeError):
                    self.phase, self.speech_mode = "negotiate", "clarify"
                    self.hint = "Clarify the voluntary amount or date; do not substitute different terms."
            else:
                for product in self.support_products:
                    if product not in s.recorded and product not in s.drafts:
                        s.mark_unpromised(product, "hardship")
                self.advance_product()
            return
        if self.support_offered and support_decision == "accept":
            self.support_proposal = None
            if s.transfer("support"):
                self.discard_proposals()
                self.farewell(hint="The support transfer succeeded.")
            else:
                raise InvalidInput("Support transfer failed")
            return
        if action == "hardship" and s.products:
            self.support_products = list(s.products) if event.get("scope", "all") == "all" else [event.get("product_id") or self.current]
            if any(p not in s.products for p in self.support_products):
                self.speech_mode = "clarify_product"
                self.hint = "Clarify which product the customer means."
                return
            self.clear_ambiguous_proposal()
            self.discard_proposals(self.support_products)
            for product in self.support_products:
                if product not in s.recorded:
                    s.set_hardship(product)
            self.phase, self.hint = "support", "Give one short sympathetic sentence, stop persuasion, and offer customer support."
            self.support_offered = True
            self.support_proposal = None
            if event.get("day") or event.get("amount_kind") or event.get("amount"):
                # Ask one question at a time. Retain the voluntary proposal, but
                # resolve the offered transfer before asking for payment confirmation.
                self.support_proposal = {**event, "action": "payment"}
            return
        if action == "restructure" and s.products:
            if s.hardship:
                self.phase, self.hint = "support", "Hardship has priority. Offer customer support; do not describe a payment plan."
            elif s.transfer("restructuring"):
                self.discard_proposals()
                self.farewell(hint="The restructuring transfer succeeded; no new payment plan has been promised.")
            else:
                raise InvalidInput("Restructuring transfer failed")
            return
        if action == "refuse_talk":
            self.talk_refusals += 1
            if self.talk_refusals >= plan["talk_refusal_limit"]:
                self.discard_proposals()
                if not s.verified:
                    self.farewell("declined")
                else:
                    for product in s.products:
                        if product not in s.recorded:
                            s.mark_unpromised(product, "refused")
                    self.farewell()
            else:
                self.speech_prefix.append(self.text("refusal_talk"))
                self.hint = "Respect the refusal to talk; respond briefly according to the policy without disclosing unverified account facts."
            return
        if action == "question":
            kind = event.get("question_kind")
            if kind in {"minimum", "deadline", "balance", "ambiguous"}:
                # A fact query does not accept or refuse the pending payment,
                # use a floor offer, consume persuasion, or modify a read-back.
                self.speech_prefix.append(self.question_notice(event))
                self.hint = "Answer only the requested known facts without changing the pending decision."
                return
            if kind in {"ai", "scam"} and self.phase != "person":
                self.identity_notice = plan["messages"]["ai_disclosure" if kind == "ai" else "scam_response"]
                self.hint = "First say exactly: " + self.identity_notice + " Then resume the current question without changing state."
                return
            if event.get("question_kind") == "purpose" and self.phase == "identity":
                action = "privacy_refusal"
            elif kind == "purpose" and self.phase == "callback":
                self.speech_prefix.append(plan["messages"]["ai_disclosure"])
                return
            elif kind == "purpose" and s.verified:
                self.speech_prefix.append(self.text("account_intro"))
                return
            elif self.phase == "person":
                self.speech_prefix.append(plan["messages"]["ai_disclosure"] if kind in {"purpose", "ai"} else
                                          plan["messages"]["scam_response"] if kind == "scam" else self.text("unknown_information"))
                action = "person_unclear"
            else:
                self.speech_prefix.append(self.text("recorded_change" if kind == "modify_recorded" else "unknown_information"))
                self.hint = "Answer the customer's question using the policy and known facts only, then return to the current question."
                return
        if action in {"wrong_number", "deceased"} and not s.verified:
            s.person_confirmed = False
            self.farewell(action, plan["messages"]["deceased"] if action == "deceased" else "Disclose no account information.")
            return
        if self.phase == "person":
            if action == "person_yes":
                s.confirm_person(True)
                self.phase, self.hint = "identity", plan["verification_questions"][0]["question"]
            elif action == "other_person":
                self.phase, self.hint = "callback", "Disclose no calling purpose. Ask when the customer can be reached."
                if event.get("day") or event.get("time"):
                    self.handle({**event, "action": "callback"})
            elif self.person_asks < plan["person_confirmation_attempts"]:
                self.person_asks += 1
                self.hint = "Answer any customer question honestly under the opening/privacy rules, then ask for explicit person confirmation again."
            else:
                self.farewell("unclear")
            return
        if self.phase == "callback":
            if action == "callback" and (event.get("day") or event.get("time")):
                # A valid component waits for the missing one; a newly supplied
                # component replaces it. An invalid component is never retained.
                kept, given, bad = self.callback_partial, {}, False
                try:
                    if event.get("day"):
                        proposed = payment_day(event["day"], s.now, s.calendar)
                        if proposed not in {s.now.date(), s.calendar.next_business_day(s.now.date())}:
                            raise InvalidInput("Callback day is outside the allowed window")
                        if proposed == s.now.date() and s.now.time() >= s.policy.callback_end:
                            raise InvalidInput("No future callback time remains today")
                        given["day"] = proposed.isoformat()
                except (InvalidInput, ValueError, TypeError):
                    kept.pop("day", None)
                    bad = True
                try:
                    at = event.get("time")
                    if at:
                        if not isinstance(at, str) or not re.fullmatch(r"\d{2}:\d{2}", at):
                            raise InvalidInput("Callback time must be HH:MM")
                        if not s.policy.callback_start <= time.fromisoformat(at) <= s.policy.callback_end:
                            raise InvalidInput("Callback time is outside the allowed window")
                        given["time"] = at
                except (InvalidInput, ValueError, TypeError):
                    kept.pop("time", None)
                    bad = True
                merged = {**kept, **given}
                if not bad and len(merged) == 2:
                    try:
                        s.calendar.check_callback(supplied_now(merged["day"] + "T" + merged["time"]), s.now)
                    except (InvalidInput, ValueError, TypeError):
                        # Only the combination is invalid: drop this turn's parts.
                        bad, given = True, {}
                self.callback_partial = {**kept, **given}
                if bad:
                    # A day OR time can invalidate a proposal. Count the whole
                    # customer proposal once, even if both fields are invalid.
                    self.callback_invalid += 1
                    if self.callback_invalid > plan["callback_corrections"]:
                        self.farewell("no_callback")
                    else:
                        self.speech_mode = "callback_window"
                        self.hint = "Explain the allowed callback window below and ask for one corrected date/time."
                    return
                if len(merged) < 2:
                    self.speech_mode = "callback_ask"
                    self.hint = "Ask for a complete callback day and time. The supplied component is valid; do not invent the missing one."
                    return
                day, at = merged["day"], merged["time"]
                if s.schedule_callback(day, at):
                    self.speech_prefix.append(self.text("callback_success", date=self.spoken_date(day), time=at))
                    self.farewell("callback", "The callback was successfully scheduled.")
                else:
                    self.speech_prefix.append(self.text("callback_failed"))
                    self.farewell("no_callback", "The callback could not be scheduled.")
            else:
                self.speech_mode = "callback_ask"
                self.hint = "Ask for a specific callback day and time; the person on the line cannot answer security questions."
            return
        if self.phase == "identity":
            if action == "privacy_refusal":
                if self.privacy < plan["privacy_explanations"]:
                    self.privacy += 1
                    self.hint = "Explain that security questions protect the account, then repeat the current security question without discussing the debt."
                    self.identity_notice = plan["messages"]["privacy_explanation"]
                else:
                    self.farewell("declined")
            elif action == "identity_answer":
                question = plan["verification_questions"][len(s.verified_fields)]
                answer = str(event.get("answer", "")).strip()
                # A piece that cannot be used yet ("Second of November", "03")
                # waits for the next piece of the same answer. A usable answer
                # alone always wins; otherwise try the two pieces joined.
                held, normalized = self.identity_fragment, None
                candidates = [answer] if event.get("answer_complete", False) else []
                if held:
                    candidates += [a + sep + b for a, b in ((held, answer), (answer, held)) for sep in (" ", "/")]
                for candidate in candidates:
                    try:
                        normalized = normalize_security_answer(question["normalization"], candidate, plan["numeric_date_order"],
                                                               plan.get("month_names", ()))
                        break
                    except InvalidInput:
                        pass
                if normalized is None:
                    self.identity_fragment = answer
                    self.hint = "Ask for the missing part of this security answer without consuming a wrong-answer attempt; do not ask a different security question yet."
                    self.identity_notice = plan["messages"]["identity_incomplete"]
                    return
                self.identity_fragment = ""
                response = s.verify(question["field"], normalized)
                if not boolean(response["ok"]):
                    self.identity_notice = self.text("verification_unavailable")
                    self.hint = "The service did not accept this answer. Explain the failure honestly and ask for a readable answer to the same question."
                elif s.verified:
                    s.load_account()
                    self.current = next(iter(s.products))
                    self.present_accounts = True
                    self.speech_mode = "offer_today"
                    self.phase = "all_today" if plan["initial_payment"] == "all_products_today" else "negotiate"
                    for product, p in s.products.items():
                        self.offers[product] = (p.past_due, s.now.date())
                    self.hint = "Announce every returned product and its amount/days late before asking for payment. Ask for full payment today. Do not narrate these instructions or mention an effective policy."
                elif s.wrong_left == 0:
                    self.farewell("failed", "Give the source policy's required directions after verification fails; disclose no account information.")
                elif response.get("hint") in s.policy.noncounting_hints:
                    self.hint = "Follow the source policy's correction instructions for non-counting hint " + str(response.get("hint")) + ". Stay on the SAME security question; no attempt was consumed."
                    self.identity_notice = next(item["message"] for item in plan["identity_hint_messages"] if item["hint"] == response["hint"])
                else:
                    self.hint = plan["verification_questions"][len(s.verified_fields)]["question"]
                    if not boolean(response["verified"]):
                        self.hint = "The answer was not verified. " + self.hint
                        self.identity_notice = plan["messages"]["identity_wrong"]
            else:
                self.hint = "Stay on the current security question; account information is unavailable until all checks pass."
            return
        if self.ambiguous_proposal and action in {"refuse_payment", "unknown", "too_much", "paid", "dispute"}:
            if event.get("scope") == "all":
                self.clear_ambiguous_proposal()
            elif (event.get("product_id") in s.products and
                  self.grounded_product_reference(event["product_id"], event.get("product_reference"))):
                self.current = event["product_id"]
                self.clear_ambiguous_proposal()
            else:
                self.clarify_proposal_product()
                return
        if action in {"payment", "accept_offer", "correction", "vague_date"}:
            if self.support_proposal:
                event = self.consume_support_proposal(event)
            try:
                self.proposals(event, accept=action == "accept_offer")
            except (InvalidInput, ValueError, TypeError):
                if self.phase == "support":
                    self.phase = "negotiate"
                self.speech_mode = "clarify"
                self.hint = "Clarify the proposed amount or date before accepting it. Do not treat a proposal as a refusal."
        elif action == "confirm" and self.phase == "confirm":
            pending = [p for p in s.drafts if p not in s.recorded and p not in s.reasons]
            # A yes naming only some products does not confirm a combined read-back.
            partial = len(pending) > 1 and (event.get("confirm_scope", "all") != "all" or event.get("scope") != "all" and (
                event.get("product_id") or event.get("product_reference")))
            if event.get("explicit_yes") is not True or partial:
                self.hint = "Ask for a clear yes to the current complete read-back; do not record anything yet."
                return
            s.confirm(self.snapshot, True)
            self.record_next()
        elif action in {"paid", "dispute"} and s.products:
            product = event.get("product_id") or self.current
            ids = list(s.products) if event.get("scope") == "all" else [product]
            if any(p not in s.products or p in s.recorded for p in ids):
                self.speech_prefix.append(self.text("recorded_change" if product in s.recorded else "clarify_product"))
                self.hint = "Clarify the product. Recorded promises cannot be changed or cancelled during this call."
                return
            for product in ids:
                s.mark_unpromised(product, action)
            self.collective_declined = self.collective_declined or self.phase == "all_today"
            self.advance_product()
            self.hint = ("Acknowledge the paid/dispute claim under the policy. " + self.hint)
            if action == "paid":
                self.speech_prefix.append(plan["messages"]["paid_explanation"])
                self.hint = "First say exactly: " + plan["messages"]["paid_explanation"] + " " + self.hint
            else:
                self.speech_prefix.append(self.text("dispute"))
        elif action == "too_much" and s.products:
            if event.get("product_id", self.current) not in s.products:
                self.speech_mode = "clarify_product"
                self.hint = "Clarify which product the customer means."
                return
            if self.phase == "all_today":
                self.collective_declined = True
                self.current = next(iter(s.products))
                self.prefer_floor.add(self.current)
                self.refusal()   # its floor offer carries the remainder notice
                self.hint += " Explain that the remainder stays overdue."
            else:
                product = event.get("product_id") or self.current
                if (self.offers.get(product, (0, s.now.date()))[1] <= s.now.date()
                        and not s.persuasion_attempts.get(product)):
                    # The declined offer was for today: as in all_today, the
                    # floor is persuasion attempt 1, on this week's offer day.
                    # (Live PUB-06: a cut-off "yes" to a floor offer for today
                    # recorded today; the customer meant Friday.)
                    self.current = product
                    self.prefer_floor.add(product)
                    self.refusal()
                    self.hint += " Explain that the remainder stays overdue."
                else:
                    self.floor_offer(product)
        elif action in {"refuse_payment", "unknown"} and self.phase in {"all_today", "negotiate", "confirm"}:
            self.collective_declined = self.collective_declined or self.phase == "all_today"
            self.refusal()
        else:
            self.speech_prefix.append(self.text("clarify"))
            self.hint = "Clarify the customer's meaning and follow the current stage; do not claim an action that has not succeeded."

    def public_state(self):
        s = self.session
        state = {"phase": self.phase, "instruction": self.hint, "verified": s.verified,
                 "identity_index": len(s.verified_fields), "wrong_answers_left": s.wrong_left,
                 "callback_start": s.policy.callback_start.isoformat(timespec="minutes"),
                 "callback_end": s.policy.callback_end.isoformat(timespec="minutes"),
                 "callback_next_business_day": s.calendar.next_business_day(s.now.date()).isoformat()}
        if s.verified and s.products:
            state["current_product"] = self.current
            state["products"] = [{"product_id": p.id, "type": p.type, "label": self.product_label(p.id), "past_due": money_text(p.past_due),
                                  "floor": money_text(p.floor), "days_late": p.days_late,
                                  "deadline": s.deadline(p.id).isoformat(),
                                  "attempts_used": s.persuasion_attempts.get(p.id, 0)} for p in s.products.values()]
            state["offers"] = {p: {"amount": money_text(a), "date": d.isoformat()} for p, (a, d) in self.offers.items()}
            state["promises_to_confirm"] = [{"product_id": p, "amount": money_text(d.cents), "date": d.date}
                                              for p, d in s.drafts.items() if p not in s.recorded and p not in s.reasons]
            state["recorded_products"] = list(s.recorded)
            state["unpromised_reasons"] = s.reasons
            state["last_recorded"] = self.last_recorded
        elif self.phase == "identity" and not s.verified:
            state["current_security_question"] = self.plan["verification_questions"][len(s.verified_fields)]
        return state

    def text(self, key, **values):
        template = self.plan["speech_templates"][key]
        def replace(match):
            if match.group(1) not in values:
                raise InvalidInput("Missing prepared speech value")
            value = str(values[match.group(1)])
            if any(c in value for c in "{}[]\r\n"):
                raise InvalidInput("Unsafe speech value")
            return value
        return re.sub(r"\[\[([a-z_]+)\]\]", replace, template)

    def spoken_date(self, value):
        day = iso_date(value) if isinstance(value, str) else value
        return str(day.day) + " " + self.plan["month_names"][day.month - 1] + " " + str(day.year)

    def product_label(self, product):
        p = self.session.products[product]
        spec = next(item for item in self.plan["product_labels"] if item["type"] == p.type)
        label = spec["label"]
        if spec["suffix_field"]:
            raw = next(item for item in self.session.account_facts if item["product_id"] == product)
            suffix = str(raw.get("_fb_wire", raw).get(spec["suffix_field"], ""))
            if not suffix:
                return label
            if not re.fullmatch(r"[A-Za-z0-9-]{1,32}", suffix):
                raise InvalidInput("Required product identifier is missing or unsafe")
            label += " " + spec["suffix_template"].replace("[[suffix]]", suffix)
        return label

    def question_notice(self, event):
        """Prepared informational reply; this method never changes bank/state."""
        kind = event.get("question_kind")
        if kind == "ambiguous":
            return self.text("clarify")
        if kind in {"ai", "scam"}:
            return self.plan["messages"]["ai_disclosure" if kind == "ai" else "scam_response"]
        if kind == "other":
            return self.text("unknown_information")
        if kind == "purpose" and event.get("action") != "question":
            return self.text("account_intro") if self.session.verified else self.plan["messages"]["ai_disclosure"]
        if kind not in {"minimum", "deadline", "balance"}:
            return ""
        s = self.session
        if not s.verified or not s.products:
            return self.text("unknown_information")
        # The question can name a different product from its primary payment
        # decision. Never infer its target from the current index after a turn
        # has advanced. An unclear target receives every announced product's
        # facts in returned order instead of an invented selection.
        target = event.get("question_product_id")
        reference = event.get("question_product_reference")
        if target is None and event.get("action") == "question":
            target = event.get("product_id")
            reference = event.get("product_reference")
        grounded = (isinstance(target, str) and target in s.products and
                    self.grounded_product_reference(target, reference))
        products = [target] if grounded else list(s.products)
        parts = []
        for product in products:
            p = s.products[product]
            label = self.product_label(product)
            if kind == "minimum":
                parts.append(self.text("floor_answer", product=label, amount=money_text(p.floor)))
            elif kind == "deadline":
                parts.append(self.text("deadline_answer", product=label, date=self.spoken_date(s.deadline(product))))
            else:
                parts.append(self.text("account_product", product=label, amount=money_text(p.past_due), days_late=p.days_late))
        return " ".join(parts)

    def fixed_speech(self, variables=None):
        """Speakable source-derived text for stages with strict state semantics."""
        s = self.session
        variables = variables or {}
        parts = list(self.speech_prefix)
        if self.phase == "end":
            return ""
        if self.phase == "identity":
            question = self.plan["verification_questions"][len(self.session.verified_fields)]["question"]
            parts += [self.identity_notice, question]
        elif self.phase == "farewell":
            if s.transfer_kind:
                parts.append(self.text("transfer_success"))
            parts += [self.closing_message, self.plan["messages"]["farewell"]]
        elif self.phase == "progress":
            parts.append(self.plan["messages"]["recorded_promise" if self.last_recorded else "recording_failed"])
        elif self.phase == "person":
            parts.append(self.text("person_retry", title=variables.get("title", ""), last_name=variables.get("last_name", "")))
        elif self.phase == "callback":
            if self.speech_mode == "callback_window":
                parts.append(self.text("callback_window", today=self.spoken_date(s.now.date()),
                    next_business_day=self.spoken_date(s.calendar.next_business_day(s.now.date())),
                    start=s.policy.callback_start.isoformat(timespec="minutes"), end=s.policy.callback_end.isoformat(timespec="minutes")))
            else:
                parts.append(self.text("callback_ask"))
        elif self.phase in {"support", "human_help"}:
            parts.append(self.text(self.phase))
        elif self.phase == "confirm":
            if not self.snapshot or self.snapshot != s.readback_snapshot:
                raise InvalidInput("No current read-back to speak")
            for product in s.products:
                if product not in s.drafts or product in s.recorded or product in s.reasons:
                    continue
                draft = s.drafts[product]
                label = self.product_label(product)
                if product in self.adjustments:
                    parts.append(self.text("adjustment", product=label, requested=self.spoken_date(self.adjustments[product]), date=self.spoken_date(draft.date)))
                parts.append(self.text("confirm_item", product=label, amount=money_text(draft.cents), date=self.spoken_date(draft.date)))
            parts.append(self.text("confirm_question"))
        elif self.phase in {"all_today", "negotiate"}:
            if self.present_accounts:
                parts.append(self.text("account_intro"))
                for product, p in s.products.items():
                    parts.append(self.text("account_product", product=self.product_label(product), amount=money_text(p.past_due), days_late=p.days_late))
            if self.speech_mode in {"clarify", "clarify_product", "date_clarify", "date_past"}:
                parts.append(self.text(self.speech_mode))
            elif self.phase == "all_today":
                parts.append(self.text("all_today"))
            elif self.speech_mode in {"offer", "offer_today"}:
                amount, day = self.offers[self.current]
                note = self.offer_notices.get(self.current)
                if note:
                    parts.append(self.text(note[0], **({"date": self.spoken_date(day)} if note[0] == "date_deadline" else {"requested": self.spoken_date(note[1])})))
                parts.append(self.text(self.speech_mode, product=self.product_label(self.current), amount=money_text(amount), date=self.spoken_date(day)))
            else:
                parts.append(self.text(self.speech_mode))
        else:
            raise InvalidInput("Unsupported speech stage")
        return " ".join(part.strip() for part in parts if part.strip())


def fold(text):
    """Case- and accent-insensitive Latin letters, without unicodedata (sandbox).
    Only functions are emitted into the workflow, so the table lives here."""
    table = str.maketrans("çğıöşüâîûéèàä",
                          "cgiosuaiueeaa")
    return str(text).replace("İ", "I").lower().replace("̇", "").translate(table)


def normalize_security_answer(kind, answer, numeric_date_order, month_names=()):
    if kind == "date":
        return normalize_birth_date(answer, numeric_date_order, month_names)
    if kind == "last_four":
        return normalize_last_four(answer)
    if kind == "year":
        return normalize_year(answer)
    if kind == "digits":
        return normalize_digits(answer)
    return answer


def normalize_last_four(answer):
    return normalize_digits(answer, 4)


def normalize_year(answer):
    """A four-digit year, said as digits, digit by digit or as a spoken year."""
    try:
        return normalize_digits(answer, 4)
    except InvalidInput:
        return str(spoken_year(re.sub(r"[,.\-]", " ", str(answer).lower()).split()))


def normalize_digits(answer, length=None):
    """A complete numeric code; leading zeros are kept and nothing is guessed."""
    digits = dict(zip("zero one two three four five six seven eight nine".split(), "0123456789"))
    # Spoken digits in the case language are folded too (calls may not be in English).
    digits.update(zip("sifir bir iki uc dort bes alti yedi sekiz dokuz".split(), "0123456789"))
    digits["oh"] = "0"
    tokens = re.sub(r"[,.-]", " ", fold(answer)).split()
    result, copies = "", 1
    for token in tokens:
        if token in {"double", "triple"}:
            if copies != 1:
                raise InvalidInput("Ambiguous digit repetition")
            copies = 2 if token == "double" else 3
            continue
        value = digits.get(token, token)
        if not re.fullmatch(r"[0-9]+", value) or copies != 1 and len(value) != 1:
            raise InvalidInput("A complete literal digit sequence is required")
        result += value * copies
        copies = 1
    if copies != 1 or not result or length and len(result) != length:
        raise InvalidInput("A complete digit code of the required length is required")
    return result


def normalize_birth_date(answer, order, month_names=()):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", answer):
        return iso_date(answer).isoformat()
    # Accept complete literal month names when extraction preserves the speech.
    # Missing parts still need clarification; never infer a year or swap numbers.
    months = ("january", "february", "march", "april", "may", "june", "july", "august",
              "september", "october", "november", "december")
    # The plan's month names are in the case language; both lists are accepted.
    local = [fold(name) for name in month_names] if len(month_names) == 12 else []
    def month_of(token):
        hits = {n + 1 for n, name in enumerate(months) if token in {name, name[:3]}}
        hits |= {n + 1 for n, name in enumerate(local) if token == name}
        return hits.pop() if len(hits) == 1 else None
    tokens = re.sub(r"[,.\-]", " ", fold(answer.strip())).split()
    tokens = [t for t in tokens if t not in {"the", "of"}]
    found = [(i, month_of(token)) for i, token in enumerate(tokens) if month_of(token)]
    if len(found) == 1:
        index, month = found[0]
        candidates = []
        parts = [(tokens[:index], tokens[index + 1:])] if index else [(tokens[1:split], tokens[split:]) for split in range(2, len(tokens))]
        for day_tokens, year_tokens in parts:
            try:
                day = spoken_number(day_tokens)
                year = spoken_year(year_tokens)
                if 1 <= day <= 31 and 1000 <= year <= 9999:
                    candidates.append(date(year, month, day).isoformat())
            except (ValueError, InvalidInput):
                pass
        if len(set(candidates)) == 1:
            return candidates[0]
        raise InvalidInput("A complete unambiguous month-name birth date is required")
    words = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th|\.)?\s+([^\W\d_]+)\s*,?\s*(\d{4})", answer.strip())
    month_first = re.fullmatch(r"([^\W\d_]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})", answer.strip())
    if words or month_first:
        if words:
            day, month, year = words.groups()
        else:
            month, day, year = month_first.groups()
        match = month_of(fold(month))
        try:
            if match is None:
                raise ValueError("Unknown month")
            return date(int(year), match, int(day)).isoformat()
        except ValueError as exc:
            raise InvalidInput("Invalid month-name birth date") from exc
    parts = re.split(r"[/.\-]", answer)
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise InvalidInput("A complete normalized birth date is required")
    values = dict(zip(order.split("_"), map(int, parts)))
    try:
        return date(values["year"], values["month"], values["day"]).isoformat()
    except (KeyError, ValueError) as exc:
        raise InvalidInput("Invalid birth date") from exc


def spoken_number(tokens):
    """Small exact English number grammar; never infer missing date components."""
    if not tokens:
        raise InvalidInput("Missing number")
    if len(tokens) == 1 and re.fullmatch(r"\d+(?:st|nd|rd|th)?", tokens[0]):
        return int(re.sub(r"(?:st|nd|rd|th)$", "", tokens[0]))
    units = dict(zip(("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split(), range(20)))
    units.update(dict(zip(("first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth").split(), range(1,20))))
    tens = dict(zip(("twenty thirty forty fifty sixty seventy eighty ninety").split(), range(20,100,10)))
    tens.update({"twentieth":20,"thirtieth":30})
    tokens = [t for t in tokens if t != "and"]
    if len(tokens) == 1 and tokens[0] in units:
        return units[tokens[0]]
    if len(tokens) == 1 and tokens[0] in tens:
        return tens[tokens[0]]
    if len(tokens) == 2 and tokens[0] in tens and 1 <= units.get(tokens[1], -1) <= 9:
        return tens[tokens[0]] + units[tokens[1]]
    for scale, factor in (("thousand",1000),("hundred",100)):
        if tokens.count(scale) == 1:
            split = tokens.index(scale)
            leading = spoken_number(tokens[:split])
            trailing = spoken_number(tokens[split+1:]) if tokens[split+1:] else 0
            if 1 <= leading <= 9 and 0 <= trailing < factor:
                return leading * factor + trailing
    raise InvalidInput("Unsupported number form")


def spoken_year(tokens):
    try:
        value = spoken_number(tokens)
        if 1000 <= value <= 9999:
            return value
    except InvalidInput:
        pass
    values = set()
    for split in range(1, len(tokens)):
        try:
            century = spoken_number(tokens[:split])
            tail = tokens[split:]
            # "nineteen eighty-eight" or "nineteen oh five"; a bare pair such
            # as "eighty eight" is not a century plus year (never 8008).
            marked = tail[0] in {"oh", "zero"}
            year = spoken_number(tail[1:] if marked else tail)
            if 10 <= century <= 99 and (0 <= year <= 9 if marked else 10 <= year <= 99):
                values.add(century * 100 + year)
        except InvalidInput:
            pass
    if len(values) != 1:
        raise InvalidInput("A complete unambiguous year is required")
    return next(iter(values))


def replay(payload, now, history, results):
    bank = ReplayBank(results)
    controller = Conversation(RuntimePolicy(payload["policy"]), payload["plan"], now, bank)
    try:
        for event in history:
            controller.handle(event)
    except PendingBankCall as pending:
        return controller, {"tool": pending.tool, "body": pending.body}
    if bank.position != len(results):
        raise InvalidInput("Unused bank results in replay journal")
    return controller, None


def at_path(value, path):
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise InvalidInput("Required response field is missing")
        value = value[part]
    return value


def normalized_response(payload, tool, envelope):
    if not isinstance(envelope, dict):
        raise InvalidInput("Bank response is not an envelope")
    status = str(envelope.get("status_code", ""))
    if status in payload["plan"].get("failure_statuses", {}).get(tool, []):
        return {"ok": False}
    if not status.isdigit() or not 200 <= int(status) < 300:
        raise InvalidInput("Transport result is uncertain; no automatic replay of a write")
    data = envelope.get("data")
    if isinstance(data, str):
        data = json.loads(data)
    paths = payload["bindings"][tool]["response"]
    ok = boolean(at_path(data, paths["ok"]))
    response = {"ok": ok}
    if not ok:
        return response
    if tool == "verify_identity":
        for role in ("verified", "wrong_answers_left"):
            response[role] = at_path(data, paths[role])
        value = response["wrong_answers_left"]
        if type(value) is not int and isinstance(value, str) and value.isdigit():
            response["wrong_answers_left"] = int(value)
        # The live bank omits all_verified on a hint answer; the API does not
        # require either field, so both are optional cross-checks.
        for role in ("all_verified", "hint"):
            try:
                response[role] = at_path(data, paths[role])
            except InvalidInput:
                pass
    elif tool == "get_account":
        products = at_path(data, paths["products"])
        if isinstance(products, str):
            products = json.loads(products)
        if not isinstance(products, list):
            raise InvalidInput("Account products must be a list")
        response["products"] = []
        prefix = paths["products"] + "[]."
        for item in products:
            normalized = dict(item)
            # Preserve exact API fields separately from canonical role aliases.
            # Set the wrapper ourselves even if the API has a field of that name.
            normalized["_fb_wire"] = dict(item)
            for role in ("product_id", "type", "past_due", "days_late"):
                normalized[role] = at_path(item, paths[role][len(prefix):])
            response["products"].append(normalized)
    return response


def workflow_step(variables, payload, mode):
    """One code-node invocation. Exception boundary is supplied by the compiler."""
    history = json.loads(variables.get("fb_history") or "[]")
    results = json.loads(variables.get("fb_results") or "[]")
    if len(history) > 150 or len(results) > 150:
        raise InvalidInput("Controller call budget exhausted")
    if mode == "result":
        pending = json.loads(variables["fb_pending"])
        if str(variables.get("fb_request_number")) != str(len(results) + 1):
            raise InvalidInput("Missing or stale bank response")
        raw = variables.get("fb_response")
        if raw:
            envelope = json.loads(raw)
        else:
            prefix = "api_" + pending["tool"] + "_response."
            status = str(variables.get(prefix + "status_code", ""))
            if (variables.get(prefix + "status") != "error" or
                    status not in payload["plan"].get("failure_statuses", {}).get(pending["tool"], [])):
                raise InvalidInput("Missing or uncertain bank response")
            envelope = {"status_code": status}
        response = normalized_response(payload, pending["tool"], envelope)
        results.append({**pending, "result": response})
    else:
        raw = variables.get("fb_event") or ""
        person_reply = variables.get("fb_person_reply", "unparsed")
        person_callback = variables.get("fb_person_callback", "{}")
        variables["fb_person_reply"] = "unparsed"
        variables["fb_person_callback"] = "{}"
        question_kind = variables.get("fb_question_kind", "none")
        variables["fb_question_kind"] = "none"
        # Only multi-product confirm nodes extract this; absent means unconfirmed.
        confirm_scope = variables.get("fb_confirm_scope") or "none"
        if "fb_confirm_scope" in variables:
            variables["fb_confirm_scope"] = "none"
        # Freya validates code writes against extraction_schema too. Empty text
        # is rejected and leaves the previous decision live, so clear with a
        # valid no-action event instead.
        variables["fb_event"] = json.dumps({"action": "unparsed"})
        try:
            event = {"action": mode} if mode in {"continue", "finalize"} else json.loads(raw) if raw else {"action": "unparsed"}
        except (ValueError, TypeError):
            event = {"action": "unparsed"}
        if not isinstance(event, dict) or not isinstance(event.get("action"), str):
            event = {"action": "unparsed"}
        if mode == "user":
            # Reconstruct the authoritative phase from the journal, never an
            # extracted/public phase hint. Opening labels cannot verify security
            # or authorize writes, and stale general events cannot confirm a person.
            prior, waiting = replay(payload, variables.get("now"), history, results)
            if waiting is not None:
                raise InvalidInput("A bank result is pending; user speech cannot skip it")
            if prior.phase == "confirm":
                event["confirm_scope"] = confirm_scope
            if prior.phase == "person":
                allowed = ("person_yes", "person_unclear", "other_person", "wrong_number", "deceased",
                           "human", "refuse_talk", "question", "unparsed")
                event = {"action": person_reply if person_reply in allowed else "unparsed"}
                if event["action"] == "other_person":
                    try:
                        details = json.loads(person_callback)
                    except (ValueError, TypeError):
                        details = {}
                    if isinstance(details, dict):
                        event.update({k: details[k] for k in ("day", "time") if k in details})
        if mode == "user":
            # Neither language extractor is an authority over the other. A
            # missing flag is neutral; two different actual questions need
            # clarification, not a guessed financial answer or a bank write.
            kinds = ("ai", "scam", "purpose", "other", "minimum", "deadline", "balance", "modify_recorded")
            primary = event.get("question_kind")
            primary = primary if isinstance(primary, str) and primary in kinds else None
            secondary = question_kind if isinstance(question_kind, str) and question_kind in kinds else None
            resolved = primary or secondary or ("ambiguous" if "question_kind" in event else None)
            if primary and secondary and primary != secondary:
                resolved = "ambiguous"
                # A call-purpose question instead of answering security still
                # follows the brief's privacy-refusal limit; disagreement must
                # not create unlimited extra attempts at that same refusal.
                if prior.phase == "identity" and "purpose" in (primary, secondary) and event["action"] in {"question", "unknown", "unparsed"}:
                    resolved = "purpose"
            if resolved:
                event["question_kind"] = resolved
                if event["action"] in {"unknown", "unparsed"} or (resolved == "ambiguous" and event["action"] == "confirm" and
                        not any(k in event for k in ("day", "amount", "amount_kind"))):
                    event["action"] = "question"
        if mode == "user" and event["action"] in {"continue", "finalize"}:
            raise InvalidInput("User extraction cannot acknowledge assistant speech")
        history.append(event)
    # Persist completed responses before replay so a later failure can never resend them.
    variables["fb_history"] = json.dumps(history)
    variables["fb_results"] = json.dumps(results)
    controller, pending = replay(payload, variables.get("now"), history, results)
    public = controller.public_state()
    # Expose only normalized, verified facts and their prepared product labels.
    # Raw wire aliases may collide with semantic field names and are not needed
    # by the language extractor; the controller retains them for exact rendering.
    variables["fb_public"] = json.dumps(public, ensure_ascii=False)
    variables["fb_instruction"] = controller.hint
    latest = history[-1] if history else {}
    notice = controller.question_notice(latest)
    variables["fb_notice"] = notice
    speech = controller.fixed_speech(variables) if pending is None else ""
    if speech and latest.get("action") == "question" and latest.get("question_kind") in {"minimum", "deadline", "balance", "ambiguous"}:
        speech = notice
    elif speech and notice and notice not in speech:
        speech = notice + " " + speech
    variables["fb_speech"] = speech
    if pending is not None:
        tool = pending["tool"]
        roles = {"customer_id": variables.get("customer_id"), **pending["body"]}
        body = {payload["bindings"][tool]["request"][k]: v for k, v in roles.items()}
        validate_value(body, payload["requests"][tool])
        variables["fb_pending"] = json.dumps(pending)
        variables["fb_request_body"] = json.dumps(body)
        variables["fb_request_number"] = str(len(results) + 1)
        variables["fb_response"] = ""
        for role in payload["bindings"][tool]["request"]:
            variables["fb_arg_" + role] = roles.get(role, "")
        variables["fb_next"] = "api_" + tool
    else:
        variables["fb_next"] = "end" if controller.phase == "end" else "speak_" + controller.phase
