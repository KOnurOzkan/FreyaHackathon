"""Two-turn pre-verification chat probe; no practice-bank session or security answer.

This diagnoses runtime startup, personalization, extraction and code execution.
It is not an official public-case result. Every known chat is ended and collected.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.artifact_checks import canonical_digest, test_version_evidence
from tools.live_smoke import load_credentials, redact


def probe(publication_path, env_path, output, case_id, turns=2):
    if turns not in (1, 2):
        raise ValueError("This probe stops before giving a security answer")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    private = load_credentials(env_path)
    import freya

    def save(name, value):
        (output / name).write_text(json.dumps(redact(value, private), indent=2, ensure_ascii=False), encoding="utf-8")

    def event(kind, **fields):
        row = redact({"event": kind, "at": datetime.now(timezone.utc).isoformat(), **fields}, private)
        with (output / "trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    publication = json.loads(Path(publication_path).read_text(encoding="utf-8"))
    case = next(c for c in json.loads((ROOT / "public-test-set.json").read_text()) if c["id"] == case_id)
    variables = dict(case["variables"])
    variables["customer_id"] = "synthetic_probe_" + secrets.token_hex(16)
    private.append(variables["customer_id"])
    messages = ["Hello?", "Yes, this is " + variables["first_name"] + " " + variables["last_name"] + "."][:turns]
    report = {"scope": "pre_verification_chat_only", "official_case_result": False,
              "publication": publication, "turns_requested": turns, "turns_received": 0, "errors": []}
    call_id = None
    try:
        saved = freya.api("GET", "/api/v2/workflow/" + publication["workflow_id"], workspaceId=freya.workspace_id())["workflow"]
        if canonical_digest(saved["workflow"]) != publication["published_definition_sha256"]:
            raise ValueError("Published definition changed before the chat probe")
        llm = saved["workflow"].get("agent_config", {}).get("llmConfig", {})
        if any(llm.get(k) != v for k, v in freya.AGENT_LLM.items()):
            raise ValueError("Required event runtime model changed")
        for index, message in enumerate(messages):
            body = {"workflowId": publication["workflow_id"], "message": message}
            if call_id:
                body["callId"] = call_id
            else:
                body["customerVariables"] = variables
            save("turn-" + str(index + 1) + "-attempted.json", {"message": message, "at": datetime.now(timezone.utc).isoformat()})
            response = freya.api("POST", "/api/v2/chat/message", body)
            if not call_id:
                call_id = response.get("callId")
            save("turn-" + str(index + 1) + ".json", response)
            report["turns_received"] += 1
            event("response_received", turn=index + 1, response=response.get("response"), call_id=call_id)
            if not call_id:
                raise ValueError("Chat response omitted the call ID; creation outcome needs investigation")
    except Exception as exc:
        report["errors"].append({"error_type": type(exc).__name__, "message": str(exc)})
        event("probe_error", **report["errors"][-1])
    finally:
        if call_id:
            report["call_id"] = call_id
            try:
                ended = freya.api("POST", "/api/v2/chat/end", {"callId": call_id})
                save("ended.json", ended)
                report["ended"] = ended.get("ended") is True
                event("chat_end_collected", ended=report["ended"])
            except Exception as exc:
                report["errors"].append({"stage": "end", "error_type": type(exc).__name__, "message": str(exc)})
            try:
                call = freya.get_call(call_id)
                save("call.json", call)
                # This is an index of the one probe call, NOT a simulation iteration.
                evidence = test_version_evidence(publication, {"runs": [{"callId": call_id}]}, {call_id: call})
                report["version_evidence"] = evidence
                event("call_collected", version_verified=evidence["verified"], messages=len(call.get("messages", [])))
            except Exception as exc:
                report["errors"].append({"stage": "collect", "error_type": type(exc).__name__, "message": str(exc)})
        save("report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--case", default="PUB-11")
    parser.add_argument("--turns", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    result = probe(args.publication, args.env, args.out, args.case, args.turns)
    return 0 if not result["errors"] and result.get("ended") and result.get("version_evidence", {}).get("verified") else 1


if __name__ == "__main__":
    raise SystemExit(main())
