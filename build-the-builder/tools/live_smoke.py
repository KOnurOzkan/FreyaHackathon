"""Explicit development-only Freya publish/test commands. No model-provider calls.

Loads only the two Freya credential roles from the explicitly supplied local file.
Never print raw HTTP errors, credentials, or synthetic per-call customer secrets.
"""
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import tempfile
import urllib.error
import urllib.request
import http.client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from artifact_checks import canonical_digest, test_version_evidence, verify_export, verify_published_export
from tools.scenario_inputs import load_case_inputs, bank_case_ids, apply_session_overrides
from tools.call_artifacts import persist_call_snapshot


class SmokeError(RuntimeError):
    pass


def read_retry(operation, event, attempts=3, *, deadline=None):
    """Retry only read operations after transient connection failures."""
    for attempt in range(attempts):
        if deadline is not None and time.monotonic() >= deadline:
            raise SmokeError("Read observation budget exceeded; collect the existing simulation")
        try:
            return operation()
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.RemoteDisconnected, RuntimeError) as exc:
            # freya.py wraps HTTP failures in RuntimeError. Restrict retries to
            # explicit transient statuses; invalid credentials/requests still stop.
            if isinstance(exc, RuntimeError) and not re.search(r" -> (?:429|500|502|503|504):", str(exc)):
                raise
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in {429, 500, 502, 503, 504}:
                raise
            event("read_connection_error", attempt=attempt + 1, error_type=type(exc).__name__)
            if attempt == attempts - 1:
                raise
            if deadline is not None and time.monotonic() + 2 >= deadline:
                raise SmokeError("Read retry would exceed the remaining observation budget") from None
            time.sleep(2)


def bounded_urlopen(original, deadline):
    """Reduce existing per-request timeouts to the phase's remaining budget."""
    def open_request(*args, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SmokeError("Network observation budget exceeded; collect the existing simulation")
        args = list(args)
        timeout = args[2] if len(args) > 2 else kwargs.get("timeout")
        timeout = min(timeout, remaining) if isinstance(timeout, (int, float)) else remaining
        if len(args) > 2:
            args[2] = timeout
        else:
            kwargs["timeout"] = timeout
        return original(*args, **kwargs)
    return open_request


def persona_cache_key(case_id, spec):
    # The complete persona spec includes the requested speaking style. Existing
    # default-style cache entries remain reusable; styles never share a persona.
    return case_id + ":" + hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]


def observe_iteration(read, personas, save, event, *, deadline, seconds=45, interval=2):
    """Observe one iteration, preserving every snapshot and conflicting verdict.

    Aggregate completion does not guarantee that judge results have settled.
    Require two matching complete observations, within this bounded read window.
    Missing verdicts never become passes, and explicit verdict changes block a
    pass even if the final response happens to say pass.
    """
    end = min(deadline, time.monotonic() + seconds)
    expected = set(personas)
    outcomes, call_ids = {p: set() for p in expected}, {p: set() for p in expected}
    previous, repeats, count, latest, stable = None, 0, 0, None, False
    while time.monotonic() < end:
        latest = read()
        count += 1
        save("iteration-observation-" + str(count).zfill(3) + ".json", latest)
        runs = latest.get("runs", [])
        complete = (isinstance(runs, list) and len(runs) == len(expected)
                    and all(isinstance(r, dict) for r in runs)
                    and {r.get("personaId") for r in runs} == expected)
        signature = []
        if complete:
            for run in runs:
                persona = run["personaId"]
                outcome, call_id = run.get("outcome"), run.get("callId")
                if outcome:
                    outcomes[persona].add(str(outcome))
                if call_id:
                    call_ids[persona].add(call_id)
                complete = complete and bool(call_id) and outcome in {"pass", "fail", "inconclusive", "error"}
                signature.append((persona, call_id, outcome, run.get("reason")))
            complete = complete and len({r.get("callId") for r in runs}) == len(expected)
        signature = sorted(signature, key=lambda item: item[0])
        repeats = repeats + 1 if complete and signature == previous else 1 if complete else 0
        previous = signature if complete else None
        if repeats >= 2:
            stable = True
            break
        if time.monotonic() + interval >= end:
            break
        time.sleep(interval)
    if latest is None:
        raise SmokeError("No budget remains to observe the simulation's existing verdicts")
    evidence = {"stable": stable, "snapshots": count,
                "personas": {p: {"outcomes": sorted(outcomes[p]), "call_ids": sorted(call_ids[p]),
                                 "conflict": len(outcomes[p]) > 1 or len(call_ids[p]) > 1} for p in sorted(expected)}}
    save("verdict-observation.json", evidence)
    event("verdicts_observed", stable=stable, snapshots=count,
          conflicts=sum(row["conflict"] for row in evidence["personas"].values()))
    return latest, evidence


def scored_row(case, ledger, run, call, score_ledger, observation=None):
    """Keep bank, judge and collection evidence separate without weakening passes."""
    verdict = score_ledger(case, ledger)
    failed = list(verdict["failed"])
    ledger_pass = not failed
    outcome, reason = run.get("outcome"), run.get("reason")
    if outcome != "pass":
        failed.append("conversation: " + str(reason or ("official outcome " + str(outcome) if outcome else "no verdict")))
    ended = bool(call.get("endedAt"))
    if not ended:
        failed.append("Call has not reported an end time; this snapshot is incomplete")
    issues = []
    observed = (observation or {}).get("personas", {}).get(run.get("personaId"), {})
    if observation is not None and not observation.get("stable"):
        issues.append("Official verdict observations did not stabilize within the read budget")
    if observed.get("conflict"):
        issues.append("Conflicting official verdict or call mappings were observed; manual review is required")
    failed.extend(issues)
    return {"id": case["id"], "level": case.get("level"), "family": case.get("family"),
            "pass": not failed, "failed": failed, "ledger_pass": ledger_pass,
            "official_outcome": outcome, "official_reason": reason, "call_ended": ended,
            "call_id": run.get("callId"), "evaluation_issues": issues,
            "official_observed_outcomes": observed.get("outcomes", [outcome] if outcome else []),
            "tools": [c["tool"] for c in ledger["calls"]]}


def load_credentials(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
        if match and match[1] in {"FREYA_API_KEY", "FREYA_BANK_TEAM_KEY"}:
            value = match[2]
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[match[1]] = value
    if not values.get("FREYA_API_KEY", "").startswith("fak_"):
        raise SmokeError("Workspace API key is missing or has an unexpected format")
    if len(values.get("FREYA_BANK_TEAM_KEY", "")) < 16:
        raise SmokeError("Practice team key must be configured with at least 16 characters")
    os.environ.update(values)
    # These local practice commands use the kit endpoints and default event model.
    # No persistent settings are changed and no provider API credentials are used.
    for key in ("FREYA_AGENT_PROVIDER", "FREYA_AGENT_MODEL", "FREYA_API_URL", "FREYA_BANK_HOST", "FREYA_WORKSPACE_ID"):
        os.environ.pop(key, None)
    return list(values.values())


def redact(value, secrets):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            label = key.lower().replace("_", "").replace("-", "")
            sensitive = any(k in label for k in ("apikey", "authorization", "teamkey", "accesstoken", "refreshtoken", "secret", "customerid"))
            result[key] = "[REDACTED]" if sensitive else redact(item, secrets)
        return result
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def reject_embedded_secrets(value, secrets):
    """Workflow schemas legitimately contain fields named customer_id or apiKey.

Never redact schema keys or edit an official export. Reject actual known secret
values instead. Runtime logs continue to use the stricter key/value redactor.
"""
    if isinstance(value, dict):
        for key, item in value.items():
            reject_embedded_secrets(key, secrets)
            reject_embedded_secrets(item, secrets)
    elif isinstance(value, list):
        for item in value:
            reject_embedded_secrets(item, secrets)
    elif isinstance(value, str) and any(secret and secret in value for secret in secrets):
        raise SmokeError("Workflow artifact contains a known secret value; refusing to save it")


def missing_values(expected, actual, path="workflow"):
    """Saved definitions may add defaults but must retain the supplied behavior."""
    differences = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [path + ": object changed"]
        for key, value in expected.items():
            if key not in actual:
                if value not in ({}, []):
                    differences.append(path + "." + key + ": missing")
            else:
                differences.extend(missing_values(value, actual[key], path + "." + key))
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return [path + ": list changed"]
        for index, value in enumerate(expected):
            differences.extend(missing_values(value, actual[index], path + "[" + str(index) + "]"))
    elif expected != actual:
        differences.append(path + ": value changed")
    return differences


def select_cases(available, requested):
    ids = requested.split(",")
    if not ids or len(set(ids)) != len(ids) or any(not item for item in ids):
        raise SmokeError("Supply distinct, comma-separated case IDs")
    lookup = {case["id"]: case for case in available}
    if any(item not in lookup for item in ids):
        raise SmokeError("A requested case ID is not in the supplied test set")
    return [lookup[item] for item in ids]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("publish", "simulate", "export"))
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--slot", default="builder-practice-b")
    parser.add_argument("--case", help="Exact case IDs, comma separated; defaults to PUB-11, or all supplied custom cases")
    parser.add_argument("--cases", help="Evaluation-only scenario JSON; existing bank fixtures remain unchanged")
    parser.add_argument("--concurrency", type=int, default=5, choices=range(1, 6),
                        help="Concurrent calls within the one workspace simulation (default 5)")
    parser.add_argument("--style", help="Optional customer speaking style from the kit's STYLES")
    parser.add_argument("--budget", type=int, default=900)
    parser.add_argument("--untested", action="store_true",
                        help="Export phase only: export the verified publication without practice test evidence (final runs)")
    args = parser.parse_args()
    if not 60 <= args.budget <= 1800:
        parser.error("Budget must be 60 to 1800 seconds")
    if args.untested and args.phase != "export":
        parser.error("--untested applies only to the export phase")
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    secrets = []
    deadline = time.monotonic() + args.budget
    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = bounded_urlopen(original_urlopen, deadline)
    overrides = []

    def patch_attribute(module, name, value):
        overrides.append((module, name, hasattr(module, name), getattr(module, name, None)))
        setattr(module, name, value)

    def save(name, value):
        path = output / name
        with path.open("x", encoding="utf-8") as stream:
            json.dump(redact(value, secrets), stream, indent=2, ensure_ascii=False)

    def save_definition(name, value):
        reject_embedded_secrets(value, secrets)
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)

    def event(kind, **fields):
        entry = redact({"event": kind, "at": datetime.now(timezone.utc).isoformat(), **fields}, secrets)
        with (output / "trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")
        print(json.dumps(entry), flush=True)

    try:
        secrets.extend(load_credentials(args.env))
        import freya
        import run_cases
        if args.style and args.style not in run_cases.STYLES:
            raise SmokeError("Unknown customer speaking style; use " + ", ".join(sorted(run_cases.STYLES)))
        original_api = freya.api
        def observed_api(method, path, body=None, **params):
            try:
                if method == "GET":
                    return read_retry(lambda: original_api(method, path, body, **params), event, deadline=deadline)
                return original_api(method, path, body, **params)
            except Exception as exc:
                event("api_error", method=method, path=path, error_type=type(exc).__name__, message=str(exc))
                raise
        patch_attribute(freya, "api", observed_api)
        from tools.call_download import get_call as download_call
        downloaded_calls = {}
        call_case_ids = {}
        def collected_call(call_id):
            if call_id not in downloaded_calls:
                call = read_retry(lambda: download_call(freya, call_id, budget=max(1, deadline-time.monotonic())), event, deadline=deadline)
                persisted = persist_call_snapshot(output, call_id, call, secrets, redact, case_id=call_case_ids.get(call_id))
                if persisted['created']:
                    event('call_snapshot_saved', call_id=call_id, case=call_case_ids.get(call_id),
                          call_ended=persisted['call_ended'], artifact=Path(persisted['path']).name)
                if call.get("endedAt"):
                    downloaded_calls[call_id] = call
                return call
            return downloaded_calls[call_id]
        patch_attribute(freya, "get_call", collected_call)
        assert freya.AGENT_LLM == {"provider": "experimental-5", "model": "experimental-5"}
        workflow_path = Path(args.workflow)
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(workflow_path.read_bytes()).hexdigest()
        manifest = json.loads(workflow_path.with_name("manifest.json").read_text(encoding="utf-8"))
        expected_hash = manifest["workflow_sha256"]
        if digest != expected_hash:
            raise SmokeError("Generated workflow no longer matches its build manifest")
        if args.phase in {"publish", "simulate"}:
            from builder.runtime_budget import validate_runtime_budget
            budgets = {}
            for node_name, node in workflow["nodes"].items():
                for action in node.get("pre_actions", []):
                    function = action.get("function", {})
                    if function.get("handler") == "__function__:code_exec":
                        budgets[node_name] = validate_runtime_budget(function["parameters"]["properties"]["code"]["default"])
            save(args.phase + "-runtime-budget.json", budgets)
        if args.phase == "simulate":
            from tools.practice_inputs import check_practice_manifest
            inputs = load_case_inputs(args.cases, args.case or (None if args.cases else "PUB-11"), root=ROOT)
            cases = inputs["cases"]
            check_practice_manifest(manifest, bank_case_ids(cases))
            if any(case.get("style") and case["style"] not in run_cases.STYLES for case in cases):
                raise SmokeError("A scenario contains an unknown kit speaking style")
        event("phase_start", phase=args.phase, workflow_sha256=digest, style=args.style)
        if args.phase == "publish":
            validation = freya.validate_workflow(args.slot, workflow)
            save("publish-validation.json", validation)
            if validation.get("valid") is not True:
                raise SmokeError("Freya did not explicitly validate this workflow")
            event("server_validation_passed")
            ids = freya.save_workflow(args.slot, workflow, str(ROOT / ".freya-agents.json"))
            save("created-ids.json", ids)
            event("workflow_saved", **ids)
            # The helper corrects the platform's default LLM to the required event
            # model if necessary and publishes. This is a kit-supported operation.
            version = freya.update_workflow(ids["workflow_id"], workflow)
            state_path = ROOT / ".freya-agents.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state[args.slot]["version"] = version
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            saved = freya.api("GET", "/api/v2/workflow/" + ids["workflow_id"], workspaceId=freya.workspace_id())["workflow"]
            definition = saved["workflow"]
            save_definition("saved-workflow.json", saved)
            differences = missing_values(workflow, definition)
            llm = definition.get("agent_config", {}).get("llmConfig", {})
            if any(llm.get(k) != v for k, v in freya.AGENT_LLM.items()):
                differences.append("agent_config.llmConfig: required event model not retained")
            versions = freya.api("GET", "/api/v2/versions/workflow/" + ids["workflow_id"], workspaceId=freya.workspace_id())["versions"]
            if not any(v["versionNumber"] == version and v.get("isPublished") for v in versions):
                differences.append("Requested version is not published")
            report = {**ids, "version": version, "workflow_sha256": digest, "differences": differences,
                      "published_definition_sha256": canonical_digest(definition),
                      "status": "readback_verified" if not differences else "readback_mismatch"}
            save("publication.json", report)
            if differences:
                event("readback_mismatch", differences=differences[:40])
                raise SmokeError("Freya changed supplied workflow fields; simulation has not been started")
            event("publication_verified", **report)
            return 0

        publication = json.loads((output / "publication.json").read_text(encoding="utf-8"))
        if publication.get("status") != "readback_verified" or publication["workflow_sha256"] != digest:
            raise SmokeError("Publish and verify this exact workflow before simulation")
        if args.phase == "export":
            if (output / "test-publication.json").exists():
                publication = json.loads((output / "test-publication.json").read_text(encoding="utf-8"))
            saved_path = output / ("tested-definition.json" if (output / "tested-definition.json").exists() else "saved-workflow.json")
            saved = json.loads(saved_path.read_text(encoding="utf-8"))["workflow"]
            if args.untested:
                # Final run on an unseen brief: no practice customer matches its
                # API, so export the verified publication and say it was untested.
                def check(exported):
                    return {**verify_published_export(publication, saved, exported), "self_test": "not_run"}
            else:
                result = json.loads((output / "result.json").read_text(encoding="utf-8"))
                review_path = output / "manual-review.json"
                review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else None
                from tools.speech_audit import audit_batch
                speech = audit_batch(workflow, output)
                save("export-speech-audit.json", speech)
                if speech["status"] != "passed":
                    raise SmokeError("Export blocked: spoken responses differ from prepared text or delivery evidence is missing")

                def check(exported):
                    return verify_export(publication, result, saved, exported, review)
            # Gate before contacting the platform, using the selected saved definition.
            check({"workflow_id": publication["workflow_id"], "agent_id": publication["agent_id"], "workflow": saved})
            versions = freya.api("GET", "/api/v2/versions/workflow/" + publication["workflow_id"], workspaceId=freya.workspace_id())["versions"]
            if (max(v["versionNumber"] for v in versions) != publication["version"] or
                    not any(v["versionNumber"] == publication["version"] and v.get("isPublished") for v in versions)):
                raise SmokeError("Workflow version changed after testing; export refused")
            with tempfile.TemporaryDirectory(prefix="freya-export-") as temp:
                path = Path(temp) / "export.json"
                freya.export_workflow(publication["workflow_id"], str(path))
                exported = json.loads(path.read_text(encoding="utf-8"))
                report = check(exported)
            save_definition("submission-export.json", exported)
            save("export-verification.json", report)
            event("export_verified", **report)
            return 0
        if (output / "simulation-started.json").exists():
            raise SmokeError("This directory already contains a simulation; collect it rather than starting another")
        if (output / "simulation-attempted.json").exists():
            raise SmokeError("A simulation creation was already attempted; resolve its outcome before starting another")
        if (output / "case-inputs.json").exists():
            previous = json.loads((output / "case-inputs.json").read_text(encoding="utf-8"))
            if previous != inputs:
                raise SmokeError("The selected evaluation inputs changed after the earlier preflight")
        else:
            save("case-inputs.json", inputs)
        inputs_digest = canonical_digest(inputs)
        # Recheck the actual publication immediately before testing. Keep an exact
        # definition independent of older diagnostic files that redacted schema keys.
        saved = freya.api("GET", "/api/v2/workflow/" + publication["workflow_id"], workspaceId=freya.workspace_id())["workflow"]
        definition = saved["workflow"]
        differences = missing_values(workflow, definition)
        llm = definition.get("agent_config", {}).get("llmConfig", {})
        if differences or any(llm.get(k) != v for k, v in freya.AGENT_LLM.items()):
            raise SmokeError("Published workflow no longer matches this generated candidate")
        versions = freya.api("GET", "/api/v2/versions/workflow/" + publication["workflow_id"], workspaceId=freya.workspace_id())["versions"]
        if (max(v["versionNumber"] for v in versions) != publication["version"] or
                not any(v["versionNumber"] == publication["version"] and v.get("isPublished") for v in versions)):
            raise SmokeError("Published version changed before testing")
        publication = {**publication, "published_definition_sha256": canonical_digest(definition)}
        if (output / "tested-definition.json").exists():
            prior = json.loads((output / "tested-definition.json").read_text(encoding="utf-8"))["workflow"]
            if canonical_digest(prior) != canonical_digest(definition):
                raise SmokeError("The selected definition changed since the earlier test preflight")
        else:
            save_definition("tested-definition.json", saved)
        if (output / "test-publication.json").exists():
            prior = json.loads((output / "test-publication.json").read_text(encoding="utf-8"))
            if prior != publication:
                raise SmokeError("Publication metadata changed since the earlier test preflight")
        else:
            save("test-publication.json", publication)
        event("test_preflight_verified", version=publication["version"],
              published_definition_sha256=publication["published_definition_sha256"])
        case_lookup = {case["id"]: case for case in cases}
        cache_path = ROOT / ".personas.json"
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        persona_ids = {}
        for case in cases:
            spec = run_cases.persona_for(case, {"identity": case["identity"]}, args.style)
            cache_key = persona_cache_key(case["id"], spec)
            if cache_key not in cache:
                made = freya.create_personas([spec])["personas"]
                cache[cache_key] = made[0]["id"]
                cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
            persona_ids[case["id"]] = cache[cache_key]
        case_label = ",".join(case["id"] for case in cases)
        original_http = run_cases.http
        ledger_cache = {}

        def bank_http(path, body=None):
            if body is None and path in ledger_cache:
                return ledger_cache[path]
            scenario = None
            if path == "/v1/practice/sessions":
                if not isinstance(body, dict) or set(body) != {"case"} or body["case"] not in case_lookup:
                    raise SmokeError("A session request does not identify a selected evaluation scenario")
                scenario = case_lookup[body["case"]]
                body = {"case": scenario["bank_case_id"]}
            result = (read_retry(lambda: original_http(path, body), event, deadline=deadline)
                      if body is None else original_http(path, body))
            if path == "/v1/practice/sessions":
                result = apply_session_overrides(scenario, result)
                variables = result.get("variables", {})
                if variables.get("customer_id"):
                    secrets.append(variables["customer_id"])
                else:
                    raise SmokeError("Practice bank did not return session variables")
            elif body is None and path.startswith("/v1/practice/ledger/"):
                ledger_cache[path] = result
            return result

        patch_attribute(run_cases, "http", bank_http)
        original_get_iteration = freya.get_iteration
        iteration_cache, iteration_evidence = {}, {}

        def stable_iteration(sim_id, it_id):
            key = (sim_id, it_id)
            if key not in iteration_cache:
                observed, evidence = observe_iteration(lambda: original_get_iteration(sim_id, it_id),
                    persona_ids.values(), save, event, deadline=deadline)
                iteration_cache[key], iteration_evidence[key] = observed, evidence
                reverse = {persona: case for case, persona in persona_ids.items()}
                candidates = {}
                for row in observed.get('runs', []):
                    if row.get('callId') and row.get('personaId') in reverse:
                        candidates.setdefault(row['callId'], []).append(reverse[row['personaId']])
                call_case_ids.update({call_id: matches[0] for call_id, matches in candidates.items() if len(matches) == 1})
            return iteration_cache[key]

        patch_attribute(freya, "get_iteration", stable_iteration)

        def bounded_simulation(agent_id, name, variables_by_persona, max_seconds=420, concurrency=1):
            # Exactly one creation attempt. No slow-run discard or automatic rerun.
            # Write BEFORE POST: a lost creation response must not enable a duplicate.
            save("simulation-attempted.json", {"agent_id": agent_id, "case": case_label,
                                               "style": args.style, "case_inputs_sha256": inputs_digest,
                                               "at": datetime.now(timezone.utc).isoformat()})
            parallel_calls = min(args.concurrency, len(variables_by_persona))
            created = freya.api("POST", "/api/v2/simulations", {
                "agentId": agent_id, "name": name, "mode": "chat", "selectedPersonaIds": list(variables_by_persona),
                "customerVariablesMap": variables_by_persona, "maxConcurrentCalls": parallel_calls, "runsPerPersona": 1,
                "maxCallDurationSeconds": max_seconds, "assistantLlmOverride": freya.AGENT_LLM,
                "routingLlmOverride": freya.AGENT_LLM, "extractionLlmOverride": freya.AGENT_LLM,
                "personaLlmOverride": freya.SIM_LLM, "analysisLlmOverride": freya.SIM_LLM})
            sim_id = (created.get("simulation") or created)["id"]
            save("simulation-started.json", {"simulation_id": sim_id, "agent_id": agent_id,
                                             "expected_version": publication["version"], "case": case_label,
                                             "cases": persona_ids, "concurrency": parallel_calls, "style": args.style,
                                             "bank_cases": {case["id"]: case["bank_case_id"] for case in cases},
                                             "case_inputs_sha256": inputs_digest})
            event("simulation_started", simulation_id=sim_id, case=case_label, budget_seconds=args.budget,
                  concurrency=parallel_calls, style=args.style, case_inputs_sha256=inputs_digest)
            last_status = None
            while time.monotonic() < deadline:
                if time.monotonic() + 10 >= deadline:
                    break
                time.sleep(10)
                result = freya.api("GET", "/api/v2/simulations/" + sim_id)
                result = result.get("simulation") or result
                if result.get("status") != last_status:
                    last_status = result.get("status")
                    event("simulation_status", simulation_id=sim_id, status=last_status)
                if last_status in {"completed", "failed", "paused"}:
                    save("simulation.json", result)
                    iterations = freya.api("GET", "/api/v2/simulations/" + sim_id + "/iterations")
                    iterations = iterations.get("iterations") or iterations.get("items") or iterations
                    return sim_id, iterations[0]["id"], last_status
            raise SmokeError("Simulation observation budget exceeded. The saved simulation ID must be collected; do not start another run")

        patch_attribute(freya, "run_simulation", bounded_simulation)
        console = sys.stdout
        class SafeConsole:
            def write(self, text):
                return console.write(redact(text, secrets))
            def flush(self):
                console.flush()
        with redirect_stdout(SafeConsole()):
            rows, sim_id, it_id, status, took, median = run_cases.run_once(publication["agent_id"], cases, persona_ids)
        iteration = freya.get_iteration(sim_id, it_id)
        save("iteration.json", iteration)
        observation = iteration_evidence[(sim_id, it_id)]
        judge = {r["personaId"]: r for r in iteration.get("runs", [])}
        calls = {}
        for row in rows:
            call = {}
            if row.get("call_id"):
                call = freya.get_call(row["call_id"])
                calls[row["call_id"]] = call
            # An absent call remains explicit missing evidence. Persist its empty
            # snapshot so the speech audit records a failure rather than crashing
            # before the rest of this finished batch can be reviewed.
            if row.get('call_id') and call.get('endedAt'):
                persist_call_snapshot(output, row['call_id'], call, secrets, redact, case_id=row['id'])
            else:
                save("call-" + row["id"] + ".json", call)
            ledger = bank_http("/v1/practice/ledger/" + row["customer_id"])
            save("ledger-" + row["id"] + ".json", ledger)
            if ledger.get("case") != case_lookup[row["id"]]["bank_case_id"]:
                raise SmokeError("Bank ledger belongs to a different case")
            row.update(scored_row(case_lookup[row["id"]], ledger,
                judge.get(persona_ids[row["id"]], {}), call, run_cases.score_ledger, observation))
            row["bank_case_id"] = case_lookup[row["id"]]["bank_case_id"]
        simulation = json.loads((output / "simulation.json").read_text(encoding="utf-8"))
        version_check = test_version_evidence(publication, iteration, calls, simulation)
        save("test-version-verification.json", version_check)
        report = {"status": status, "simulation_id": sim_id, "iteration_id": it_id,
                  "agent_id": publication["agent_id"], "expected_version": publication["version"],
                  "workflow_sha256": digest, "version_verified": version_check["verified"],
                  "style": args.style, "verdict_observation": observation, "case_inputs_sha256": inputs_digest,
                  "passed": sum(r["pass"] for r in rows), "total": len(rows), "seconds": round(took, 2),
                  "rows": rows, "median_slowest_turn_seconds": median}
        save("result.json", report)
        from tools.speech_audit import audit_batch
        speech = audit_batch(workflow, output)
        save("speech-audit.json", speech)
        event("prepared_speech_checked", status=speech["status"], findings=len(speech["findings"]))
        event("simulation_collected", status=status, passed=report["passed"], total=report["total"],
              seconds=report["seconds"], version_verified=version_check["verified"])
        return 0 if (report["passed"] == report["total"] and status == "completed"
                     and version_check["verified"] and speech["status"] == "passed") else 1
    except Exception as exc:
        event("error", phase=args.phase, error_type=type(exc).__name__, message=str(exc))
        return 2
    finally:
        urllib.request.urlopen = original_urlopen
        for module, name, existed, value in reversed(overrides):
            if existed:
                setattr(module, name, value)
            else:
                delattr(module, name)


if __name__ == "__main__":
    raise SystemExit(main())
