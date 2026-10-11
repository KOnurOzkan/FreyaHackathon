"""Execute emitted code/hooks and fixed transitions, with no Freya or speech model.

Customer inputs are scripted semantic decisions, NOT transcript test results.
"""
from copy import deepcopy
import json

from builder.dialogue_plan import PLAN_KEYS, STAGES
from tests.test_extraction import short_reference


def fake_speech():
    return {
        "person_retry": "Am I speaking with [[title]] [[last_name]]?",
        "callback_ask": "When can the customer be reached?",
        "callback_window": "Please choose [[today]] or [[next_business_day]], from [[start]] to [[end]], later than now.",
        "account_intro": "I am calling from the collections team about a late payment.",
        "account_product": "Your [[product]] has [[amount]] past due and is [[days_late]] days late.",
        "floor_answer": "The smallest payment I can record for your [[product]] is [[amount]].",
        "deadline_answer": "The latest payment date I can record for your [[product]] is [[date]].",
        "all_today": "Can you pay all these amounts today?",
        "offer_today": "For your [[product]], can you pay [[amount]] today?",
        "offer": "For your [[product]], can you pay [[amount]] on [[date]]?",
        "floor_notice": "The rest stays overdue.", "below_floor": "I cannot record an amount below the minimum.",
        "amount_too_high": "That is more than these arrears.",
        "date_nonbusiness": "[[requested]] is not a business day.",
        "date_deadline": "The latest date I can record is [[date]].",
        "date_past": "That date is in the past. Which future date do you mean?",
        "date_clarify": "Which exact payment date do you mean?", "clarify": "Could you clarify your answer?",
        "clarify_product": "Which product do you mean?",
        "confirm_item": "Your proposed payment for [[product]] is [[amount]] on [[date]].",
        "confirm_question": "Do you confirm?",
        "adjustment": "For [[product]], [[requested]] is beyond the deadline, so the proposed date is [[date]].",
        "support": "I am sorry to hear that. Would you like to speak to customer support?",
        "human_help": "May I help you first?", "unknown_information": "I do not have that information.",
        "recorded_change": "You can change a recorded promise in the mobile app.",
        "callback_success": "Your callback is scheduled for [[date]] at [[time]].",
        "callback_failed": "I could not schedule the callback.", "transfer_success": "I am connecting you to the requested team.",
        "verification_unavailable": "The verification service could not process the answer.",
        "dispute": "I understand that you dispute this product.", "refusal_talk": "I understand you do not want to talk.",
        "abort": "A technical problem prevents me from confirming completion.",
    }


def fake_plan(documents, multiple, contracts):
    """Independent test parameters; never an input to the production builder."""
    plan = dict(person_confirmation_attempts=2, callback_corrections=1, privacy_explanations=1,
                talk_refusal_limit=2, vague_date_clarifications=1, floor_offers=1, human_help_offers=1,
                initial_payment="all_products_today" if multiple else "each_product_today",
                shared_date="cap_per_product" if multiple else "offer_deadline",
                confirmation="combined" if multiple else "per_product", product_order="returned_order",
                first_persuasion="last_business_day_this_or_next_week", second_persuasion="floor_on_deadline",
                hardship_priority="before_persuasion_and_restructuring", numeric_date_order="day_month_year",
                transfer_forbids_notes=True,
                speech_templates=fake_speech(),
                month_names="January February March April May June July August September October November December".split(),
                product_labels=[{"type": t, "label": t.replace("_", " "), "suffix_field": "card_last4" if t == "credit_card" else "",
                                 "suffix_template": "ending [[suffix]]" if t == "credit_card" else ""}
                                for t in ("credit_card", "personal_loan", "car_loan")],
                identity_hint_messages=[{"hint": "day_month_swapped", "message": "Please say the month as a word."}],
                stage_instructions={stage: "Follow the current controller instruction and source policy." for stage in STAGES},
                messages={"opening": "I am a digital assistant calling from Freya Bank. This call is recorded. Am I speaking with {{title}} {{last_name}}?",
                          "recorded_promise": "You will receive a text message confirming this promise.",
                          "deceased": "I am sorry for your loss.", "farewell": "Thank you. Goodbye.",
                          "identity_wrong": "That answer was not verified.",
                          "identity_incomplete": "Please give your complete answer again.",
                          "identity_failed": "I could not verify your identity. Please use the mobile app or a branch.",
                          "privacy_explanation": "These questions protect your account.",
                          "ai_disclosure": "I am Freya Bank's digital assistant.",
                          "scam_response": "You can verify this call by calling the bank's customer service number.",
                          "paid_explanation": "Payments can take one business day to appear.",
                          "recording_failed": "I could not record this promise."},
                verification_questions=[{"field": "date_of_birth", "question": "What is your date of birth?", "normalization": "date"},
                                        {"field": "phone_last4", "question": "What are the last four phone digits?", "normalization": "last_four"}])
    # Membership evidence only. These fixtures do not claim a semantic extraction test.
    plan["evidence"] = [{"section": key, "sources": [short_reference(documents[0])]} for key in sorted(PLAN_KEYS)]
    return {"plans": [plan], "unsupported": [], "documented_no_effect_failures": [
        {"tool": tool, "status_code": 500, "quote": op["responses"]["500"]["description"]}
        for tool, op in contracts.operations.items()]}


class LocalGraph:
    def __init__(self, workflow, bank, variables, bindings, response_adapter=None):
        self.workflow, self.bank = workflow, bank
        self.vars = {**variables, "customer_id": "synthetic-local-customer"}
        self.bindings = bindings
        self.node = workflow["initial_node"]
        self.timeline = []
        self.response_adapter = response_adapter
        self.max_code_seconds = 0

    def user(self, action, **fields):
        # These fixtures already select authoritative semantic product IDs.
        # Supply matching reference evidence unless a test explicitly exercises
        # an absent, vague or contradictory extractor reference.
        if fields.get("product_id") and "product_reference" not in fields:
            product = next((p for p in self.state.get("products", [])
                            if p["product_id"] == fields["product_id"]), None)
            if product:
                fields["product_reference"] = product["label"]
        if any(s["name"] == "fb_person_reply" for s in self.workflow["nodes"][self.node].get("extraction_schema", [])):
            # Semantic fixtures must populate the actual phase-specific wire
            # contract. Raw-wire regression tests bypass this helper deliberately.
            self.vars["fb_person_reply"] = action
            self.vars["fb_person_callback"] = json.dumps({k: fields[k] for k in ("day", "time") if k in fields})
            self.vars["fb_question_kind"] = fields.get("question_kind", "none")
        else:
            schema_names = {s["name"] for s in self.workflow["nodes"][self.node].get("extraction_schema", [])}
            if "fb_confirm_scope" in schema_names:
                # Multi-product read-back: a plain fixture confirm covers everything
                # unless the test states a narrower scope.
                self.vars["fb_confirm_scope"] = fields.pop("confirm_scope", "all")
            self.vars["fb_event"] = json.dumps({"action": action, **fields})
        self.advance("user_turn")

    def speech_done(self):
        node = self.workflow["nodes"][self.node]
        targets = [t["target_node"] for t in node.get("transitions", []) if t.get("is_tool_call")]
        if node.get("passthrough"):
            targets = [node["passthrough_default_transition"]]
        if len(targets) != 1:
            raise AssertionError("This stage must wait for customer input")
        self.timeline.append({"speech_completion": self.node})
        self.advance(targets[0])

    def finish(self):
        for _ in range(8):
            if self.node == "end":
                return
            self.speech_done()
        raise AssertionError("Unbounded closing/recording loop")

    @property
    def state(self):
        return json.loads(self.vars.get("fb_public") or "{}")

    def advance(self, target):
        import time
        for _ in range(100):
            self.node = target
            node = self.workflow["nodes"][target]
            if node["node_type"] in {"conversation", "end_call"}:
                return
            tool = node["pre_actions"][0]
            fn = tool["function"]
            if node["node_type"] == "code":
                start = time.perf_counter()
                exec(fn["parameters"]["properties"]["code"]["default"], {"vars": self.vars})
                self.max_code_seconds = max(self.max_code_seconds, time.perf_counter() - start)
                target = self.vars["fb_next"]
                if not any(t["target_node"] == target for t in node["transitions"]):
                    raise AssertionError("Code selected a missing transition")
            else:
                config = {"body": {}}
                exec(tool["pre_process_code"], {"vars": self.vars, "config": config})
                name = fn["name"]
                reverse = {v: k for k, v in self.bindings[name]["request"].items()}
                values = {reverse[k]: v for k, v in config["body"].items()}
                if values.pop("customer_id", None) != self.vars["customer_id"]:
                    raise AssertionError("Missing or incorrect synthetic customer ID")
                self.timeline.append({"tool": name, "args": deepcopy(values)})
                result = self.bank.call(name, values)
                envelope = {"status_code": 500 if result.get("ok") is False and "injected" in result.get("error", "") else 200,
                            "data": result, "header": {"test_header": "discard"}}
                if self.response_adapter:
                    envelope = self.response_adapter(name, envelope)
                # Only `result` is documented for post-hooks. Hand variables off
                # through the node's response_mapping, as Freya does.
                # Live Freya skips post hooks on HTTP errors and exposes status
                # through node response variables instead. Exercise that path.
                self.vars['api_' + name + '_response.status_code'] = envelope.get('status_code', '')
                self.vars['api_' + name + '_response.status'] = 'success' if 200 <= envelope.get('status_code', 0) < 300 else 'error'
                if self.vars['api_' + name + '_response.status'] == 'success':
                    exec(tool["post_process_code"], {"result": envelope})
                for mapping in node.get("response_mapping", []):
                    value = envelope
                    for part in mapping["path"].split("."):
                        if not isinstance(value, dict) or part not in value:
                            break
                        value = value[part]
                    else:
                        self.vars[mapping["variable"]] = value
                target = node["transitions"][-1]["target_node"]
        raise AssertionError("Unbounded action-node loop")
