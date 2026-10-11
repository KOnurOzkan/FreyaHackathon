"""Action guards for normalized events, not a natural-language phone agent.

The bank port accepts semantic field names. The workflow runtime adapter maps
these to the supplied OpenAPI and validates actual response envelopes. No HTTP is
performed here. Local farewell events do not establish spoken-transcript policy.
"""
from dataclasses import dataclass

from .inputs import InvalidInput
from .policy import NOTE_KINDS, TRANSFER_KINDS
from .rules import Calendar, Product, iso_date, money, money_text, supplied_now


def boolean(value):
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise InvalidInput("Tool result must contain an explicit boolean")


@dataclass(frozen=True)
class Proposal:
    product_id: str
    cents: int
    date: str


class Session:
    def __init__(self, policy, now, bank):
        self.policy, self.now, self.bank = policy, supplied_now(now), bank
        self.calendar = Calendar(policy)
        self.person_confirmed = False
        self.verified_fields = []
        self.wrong_left = policy.wrong_answer_limit
        self.products = {}
        self.account_facts = []
        self.drafts, self.recorded, self.reasons = {}, {}, {}
        self.write_attempts, self.persuasion_attempts = {}, {}
        self.hardship = set()
        self.revision = 0
        self.readback_snapshot = self.confirmed_snapshot = None
        self.transfer_kind = None
        self.callback_recorded = False
        self.sealed = self.closed = self.uncertain = False
        self.events = []

    @property
    def verified(self):
        return tuple(self.verified_fields) == self.policy.identity_fields

    def _active(self):
        if self.closed or self.sealed or self.uncertain or self.transfer_kind or self.callback_recorded:
            raise InvalidInput("Session no longer accepts customer actions")

    def _call(self, tool, values):
        if self.closed or self.uncertain:
            raise InvalidInput("Tool call forbidden after closing or an uncertain result")
        self.events.append({"event": "tool", "tool": tool})
        try:
            result = self.bank.call(tool, values)
            if not isinstance(result, dict):
                raise InvalidInput("Malformed tool result")
            boolean(result.get("ok"))
        except Exception:
            # An uncertain write cannot be safely retried or called a documented failure.
            self.uncertain = True
            raise
        return result

    def confirm_person(self, explicit_confirmation):
        self._active()
        if explicit_confirmation is not True:
            raise InvalidInput("An explicit person confirmation is required")
        self.person_confirmed = True

    def verify(self, field, answer):
        self._active()
        if not self.person_confirmed or self.wrong_left == 0 or self.verified:
            raise InvalidInput("Identity check is not permitted in this state")
        if field != self.policy.identity_fields[len(self.verified_fields)]:
            raise InvalidInput("Security questions must follow the policy order")
        result = self._call("verify_identity", {"field": field, "answer": answer})
        if not boolean(result["ok"]):
            return result
        remaining = result.get("wrong_answers_left")
        if type(remaining) is not int or not 0 <= remaining <= self.wrong_left:
            raise InvalidInput("Invalid authoritative identity counter")
        passed = boolean(result.get("verified"))
        hint = result.get("hint")
        if not passed and hint not in self.policy.noncounting_hints and remaining != self.wrong_left - 1:
            raise InvalidInput("Identity failure did not consume exactly one allowed answer")
        if (passed or hint in self.policy.noncounting_hints) and remaining != self.wrong_left:
            raise InvalidInput("Successful or hinted identity answer changed the wrong-answer counter")
        prospective = self.verified_fields + ([field] if passed else [])
        if "all_verified" in result and boolean(result["all_verified"]) != (tuple(prospective) == self.policy.identity_fields):
            raise InvalidInput("Identity result contradicts the required question sequence")
        self.wrong_left = remaining
        self.verified_fields = prospective
        return result

    def load_account(self, product_fields=None):
        self._active()
        if not self.verified or self.products:
            raise InvalidInput("Account lookup requires all checks and can occur only once")
        result = self._call("get_account", {})
        if not boolean(result["ok"]):
            raise InvalidInput("Account lookup failed; no account facts may be invented")
        raw = result.get("products")
        if not isinstance(raw, list) or not 1 <= len(raw) <= self.policy.max_products:
            raise InvalidInput("Account product count is outside the supported policy")
        products = [Product.from_account(p, self.policy, product_fields) for p in raw]
        if len({p.id for p in products}) != len(products):
            raise InvalidInput("Account contains duplicate product IDs")
        self.products = {p.id: p for p in products}
        self.account_facts = raw
        return products

    def _product(self, product_id):
        if product_id not in self.products:
            raise InvalidInput("Use an authoritative product ID returned by the account")
        return self.products[product_id]

    def deadline(self, product_id):
        p = self._product(product_id)
        return self.calendar.add_business_days(self.now.date(), self.policy.band_for(p.days_late).business_days)

    def propose(self, product_id, amount, day, *, voluntary=False):
        self._active()
        p = self._product(product_id)
        if product_id in self.recorded or self.write_attempts.get(product_id, 0):
            raise InvalidInput("A product whose recording began cannot be edited in this local core")
        if product_id in self.hardship and not voluntary:
            raise InvalidInput("Hardship permits only a voluntarily offered promise")
        cents, payment_day = money(amount), iso_date(day)
        if not p.floor <= cents <= p.past_due:
            raise InvalidInput("Promise amount is outside the product floor/full range")
        self.calendar.check_promise(payment_day, self.now.date(), self.deadline(product_id))
        self.drafts[product_id] = Proposal(product_id, cents, day)
        self.reasons.pop(product_id, None)
        self.revision += 1
        self.confirmed_snapshot = None

    def read_back(self):
        self._active()
        if not self.drafts:
            raise InvalidInput("There are no promises to read back")
        if set(self.products) - set(self.drafts) - set(self.reasons):
            raise InvalidInput("Resolve every product before the combined read-back")
        self.readback_snapshot = (self.revision, tuple(self.drafts[p] for p in self.products if p in self.drafts))
        return self.readback_snapshot

    def confirm(self, snapshot, explicit_yes):
        self._active()
        if explicit_yes is not True or snapshot != self.readback_snapshot or snapshot is None or snapshot[0] != self.revision:
            self.confirmed_snapshot = None
            raise InvalidInput("Clear confirmation of the current complete read-back is required")
        self.confirmed_snapshot = snapshot

    def record(self, product_id):
        self._active()
        if self.confirmed_snapshot is None or self.confirmed_snapshot[0] != self.revision:
            raise InvalidInput("Do not write an unconfirmed or changed promise")
        if product_id not in self.drafts or product_id in self.recorded:
            raise InvalidInput("No writable promise for this product")
        attempts = self.write_attempts.get(product_id, 0)
        if attempts >= self.policy.promise_attempt_limit:
            raise InvalidInput("Promise recording attempts exhausted")
        draft = self.drafts[product_id]
        p = self._product(product_id)
        self.calendar.check_promise(iso_date(draft.date), self.now.date(), self.deadline(product_id))
        if not p.floor <= draft.cents <= p.past_due:
            raise InvalidInput("Promise amount no longer valid")
        self.write_attempts[product_id] = attempts + 1
        result = self._call("create_promise", {"product_id": product_id,
                            "amount": float(money_text(draft.cents)), "date": draft.date})
        if boolean(result["ok"]):
            self.recorded[product_id] = draft
            self.events.append({"event": "promise_recorded", "product_id": product_id})
            return True
        if attempts + 1 == self.policy.promise_attempt_limit:
            self.reasons[product_id] = "service_error"
        return False

    def set_hardship(self, product_id):
        self._active()
        self._product(product_id)
        self.hardship.add(product_id)
        if product_id not in self.recorded:
            self.drafts.pop(product_id, None)
            self.revision += 1
            self.confirmed_snapshot = None

    def refuse(self, product_id):
        """Count a normalized flat refusal, not arbitrary text or a counteroffer."""
        self._active()
        p = self._product(product_id)
        if product_id in self.hardship or product_id in self.recorded:
            raise InvalidInput("Persuasion forbidden in this product state")
        self.drafts.pop(product_id, None)
        self.revision += 1
        self.confirmed_snapshot = None
        attempts = self.persuasion_attempts.get(product_id, 0)
        if attempts == self.policy.band_for(p.days_late).attempts:
            self.mark_unpromised(product_id, "refused")
            return None
        self.persuasion_attempts[product_id] = attempts + 1
        return attempts + 1

    def mark_unpromised(self, product_id, reason):
        self._active()
        self._product(product_id)
        if reason not in NOTE_KINDS or product_id in self.recorded:
            raise InvalidInput("Invalid unpromised-product disposition")
        if reason == "service_error" and self.write_attempts.get(product_id, 0) < self.policy.promise_attempt_limit:
            raise InvalidInput("Promise service failure requires exhausted documented attempts")
        self.reasons[product_id] = reason
        self.drafts.pop(product_id, None)
        self.revision += 1
        self.confirmed_snapshot = None

    def schedule_callback(self, day, at):
        self._active()
        if self.person_confirmed or self.callback_recorded:
            raise InvalidInput("Callback is only for an unreached customer and can be recorded once")
        candidate = supplied_now(day + "T" + at)
        self.calendar.check_callback(candidate, self.now)
        result = self._call("schedule_callback", {"date": day, "time": at})
        self.callback_recorded = boolean(result["ok"])
        return self.callback_recorded

    def transfer(self, kind):
        self._active()
        if kind not in TRANSFER_KINDS:
            raise InvalidInput("Unknown transfer reason")
        result = self._call("transfer_to_agent", {"queue": self.policy.transfer_queues[kind]})
        if boolean(result["ok"]):
            self.transfer_kind = kind
            self.drafts = {p: d for p, d in self.drafts.items() if p in self.recorded}
            self.reasons.clear()
            self.confirmed_snapshot = None
            return True
        return False

    def finish(self, early_reason=None):
        """Seal decisions, flush deferred notes, emit farewell event, then close once.

        This is an atomic local finalization boundary. Freya interruption behavior
        and actually speaking the farewell still require an integration test.
        """
        if self.closed or self.sealed or self.uncertain:
            raise InvalidInput("Cannot finalize a closed, sealed or uncertain session")
        if self.transfer_kind:
            kind = self.transfer_kind
        elif self.products:
            if early_reason is not None:
                raise InvalidInput("Account outcomes must be derived from product results")
            missing = set(self.products) - set(self.recorded) - set(self.reasons)
            if missing:
                raise InvalidInput("Every product needs a recorded promise or an explicit disposition")
            kind = "promise" if self.recorded else next((self.reasons[p] for p in self.products if self.reasons[p] != "paid"), "paid")
        else:
            allowed = {"declined", "failed"} if self.person_confirmed else {"declined", "callback", "no_callback", "wrong_number", "deceased", "unclear"}
            if self.verified or early_reason not in allowed:
                raise InvalidInput("Unsupported early closing reason")
            if early_reason == "failed" and self.wrong_left != 0:
                raise InvalidInput("Verification failure requires exhausted wrong answers")
            if (early_reason == "callback") != self.callback_recorded:
                raise InvalidInput("Callback outcome must match its successful tool result")
            kind = early_reason
        self.sealed = True
        if self.policy.notes_enabled and not self.transfer_kind:
            for product_id in self.products:
                if product_id not in self.recorded:
                    result = self._call("add_note", {"product_id": product_id,
                                       "reason": self.policy.note_reasons[self.reasons[product_id]]})
                    if not boolean(result["ok"]):
                        raise InvalidInput("Note failed; local run is incomplete, not a successful close")
        self.events.append({"event": "farewell_required_before_close"})
        try:
            result = self._call("close_call", dict(self.policy.outcomes[kind]))
        finally:
            self.closed = True
        if not boolean(result["ok"]):
            raise InvalidInput("Closing failed; do not claim success or automatically repeat it")
        return dict(self.policy.outcomes[kind])
