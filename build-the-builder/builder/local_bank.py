"""Small in-memory bank test double. It is not Freya's official bank implementation.

Business-day, amount and persuasion policy is deliberately NOT enforced here:
tests must show the Session guards reject bad actions before they reach the bank.
"""
from copy import deepcopy
from datetime import date


class LocalBank:
    def __init__(self, identity, products, *, wrong_answer_limit, faults=(), swap_hint=None):
        self.identity = deepcopy(identity)
        self.products = deepcopy(products)
        for i, product in enumerate(self.products):
            product.setdefault("product_id", f"p{i + 1}")
        self.wrong_left = wrong_answer_limit
        self.verified = set()
        self.faults = deepcopy(list(faults))
        self.fault_hits = [0] * len(self.faults)
        self.swap_hint = swap_hint
        self.ledger = {"outcome": None, "reason": None, "promises": [], "notes": [],
                       "callbacks": [], "transfers": [], "calls": []}
        self.closed = False

    @classmethod
    def from_case(cls, case, policy, **kwargs):
        return cls(case["identity"], case["products"], wrong_answer_limit=policy.wrong_answer_limit,
                   faults=case.get("faults", ()), **kwargs)

    def call(self, tool, body):
        result = self._execute(tool, body)
        self.ledger["calls"].append({"tool": tool, "args": deepcopy(body), "result": deepcopy(result)})
        return result

    def _execute(self, tool, body):
        if self.closed:
            return {"ok": False, "error": "call already closed"}
        for i, fault in enumerate(self.faults):
            if fault["tool"] == tool and ("product_id" not in fault or fault["product_id"] == body.get("product_id")) and self.fault_hits[i] < fault["fail_first_n"]:
                self.fault_hits[i] += 1
                return {"ok": False, "error": "injected failure; nothing recorded"}
        if tool == "verify_identity":
            field, answer = body["field"], body["answer"]
            if field not in self.identity or self.wrong_left == 0:
                return {"ok": False, "error": "invalid field or exhausted identity check"}
            expected = self.identity[field]
            passed, hint = answer == expected, None
            if not passed and self.swap_hint:
                try:
                    given, actual = date.fromisoformat(answer), date.fromisoformat(expected)
                    if (given.year, given.month, given.day) == (actual.year, actual.day, actual.month):
                        hint = self.swap_hint
                except (TypeError, ValueError):
                    pass
            if passed:
                self.verified.add(field)
            elif not hint:
                self.wrong_left -= 1
            result = {"ok": True, "verified": passed, "all_verified": self.verified == set(self.identity),
                      "wrong_answers_left": self.wrong_left}
            if hint:
                # Observed live (A055 DEV-A-IDENTITY-SWAPPED-NONCOUNTING): a hint
                # answer carries no all_verified field.
                del result["all_verified"]
                result["hint"] = hint
            return result
        if tool in {"get_account", "create_promise", "add_note"} and self.verified != set(self.identity):
            return {"ok": False, "error": "identity not verified"}
        if tool == "get_account":
            return {"ok": True, "products": deepcopy(self.products)}
        if tool in {"create_promise", "add_note"} and body.get("product_id") not in {p["product_id"] for p in self.products}:
            return {"ok": False, "error": "unknown product_id"}
        if tool == "create_promise":
            if any(p["product_id"] == body["product_id"] for p in self.ledger["promises"]):
                return {"ok": False, "error": "promise already exists"}
            self.ledger["promises"].append(deepcopy(body))
            return {"ok": True, "promise_id": "local-" + str(len(self.ledger["promises"]))}
        if tool in {"add_note", "schedule_callback", "transfer_to_agent"}:
            target = {"add_note": "notes", "schedule_callback": "callbacks", "transfer_to_agent": "transfers"}[tool]
            self.ledger[target].append(deepcopy(body))
            return {"ok": True}
        if tool == "close_call":
            self.ledger["outcome"] = body["outcome"]
            self.ledger["reason"] = body.get("reason")
            self.closed = True
            return {"ok": True}
        return {"ok": False, "error": "unknown tool"}


def audit_ledger(case, ledger):
    """Use the kit's pure scorer plus an explicit exactly-once closing check.

    This is a ledger check only, NEVER the official transcript-inclusive score.
    Importing score does not invoke its HTTP helper or its command-line entry point.
    """
    from tools.score import score_ledger
    result = score_ledger(case, ledger)
    if sum(c["tool"] == "close_call" for c in ledger["calls"]) != 1:
        result["failed"].append("close_call must occur exactly once")
    result["pass"] = not result["failed"]
    result["scope"] = "local_ledger_only"
    return result
