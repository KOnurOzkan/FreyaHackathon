"""Compare collected speech with the exact text the controller prepared.

No model, network or case-specific expected answers. This checks delivery fidelity,
not whether an extracted policy or customer intent was semantically correct.
"""
import json
from pathlib import Path
import re


def normalized(text):
    return " ".join(str(text).split())


def audit_call(workflow, call):
    nodes = workflow["nodes"]
    findings, segments = [], []
    current, variables, segment = None, {}, None

    def events():
        for message in call.get("messages", []):
            event = {}
            if message.get("role") == "system":
                try:
                    event = json.loads(message.get("message", ""))
                except (ValueError, TypeError):
                    pass
            yield message, event if isinstance(event, dict) else {}

    # Call-start placeholders are immutable; logged variables may first appear
    # after the opening. Never use later fb_speech for an earlier utterance.
    for _, event in events():
        for key in ("title", "last_name", "first_name"):
            value = event.get("variable_table", {}).get(key)
            if value is not None:
                variables[key] = value.get("value") if isinstance(value, dict) else value

    def flush():
        nonlocal segment
        if segment is None:
            return
        actual = normalized(" ".join(segment.pop("parts")))
        segment["observed"] = actual
        segments.append(segment)
        if not segment["expected"] or "{{" in segment["expected"] or actual != normalized(segment["expected"]):
            findings.append({"node": segment["node"], "seconds": segment["seconds"],
                             "expected": segment["expected"], "observed": actual,
                             "reason": "Spoken response differs from prepared text or delivery evidence is missing"})
        segment = None

    for message, event in events():
        for key, value in event.get("variable_table", {}).items():
            if key == "fb_speech":
                variables[key] = value.get("value") if isinstance(value, dict) else value
        if event.get("event") == "node_enter":
            flush()
            current = event.get("node")
            node = nodes.get(current, {})
            if node.get("node_type") == "conversation":
                entry = node.get("on_enter", {})
                text = entry.get("text", "") if entry.get("type") == "tts" else ""
                expected = re.sub(r"\{\{([a-z_]+)\}\}", lambda m: str(variables.get(m[1], m[0])), text)
                segment = {"node": current, "seconds": message.get("secondsFromStart"), "expected": expected, "parts": []}
        elif message.get("role") == "assistant":
            if segment is None:
                findings.append({"node": current, "observed": message.get("message", ""),
                                 "reason": "Assistant speech has no prepared conversation entry"})
            else:
                segment["parts"].append(message.get("message", ""))
        elif message.get("role") == "user":
            flush()
    flush()
    if not segments:
        findings.append({"reason": "No prepared speech delivery was collected"})
    return {"status": "passed" if not findings else "failed", "utterances_checked": len(segments),
            "findings": findings, "segments": segments}


def audit_batch(workflow, folder):
    folder = Path(folder)
    result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    reports, findings = {}, []
    for row in result.get("rows", []):
        case = row["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", case):
            raise ValueError("Invalid case filename")
        if row.get("never_started") and not row.get("call_id"):
            continue   # the simulation stopped before this call existed; nothing was spoken
        report = audit_call(workflow, json.loads((folder / ("call-" + case + ".json")).read_text(encoding="utf-8")))
        reports[case] = report
        findings.extend({"case": case, **item} for item in report["findings"])
    return {"status": "passed" if reports and not findings else "failed",
            "workflow_sha256": result["workflow_sha256"], "workflow_version": result.get("expected_version"),
            "findings": findings, "cases": reports,
            "scope": "Exact prepared-speech delivery; policy meaning and customer-intent accuracy require other checks"}
