"""Bounded local workflow compilation, with an explicit source-backed model plan."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from . import __version__
from .codex_provider import ProviderError
from .contracts import Contracts
from .dialogue_plan import complete_plan_prompt, plan_schema, validate_plan, PlanStructureError
from .extraction import validate_extraction
from .extract_run import run_extraction, write_json
from .inputs import InvalidInput, read_documents, read_json, run_fingerprints, require_unchanged
from .workflow import compile_workflow
from .policy import Policy


def local_runtime_checks(workflow):
    """Run emitted code, not speech/extraction models or bank requests."""
    source = workflow["nodes"]["user_turn"]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]
    variables = {"now": "2030-01-08T11:00", "customer_id": "synthetic-compiler-check",
                 "fb_person_reply": "person_yes"}
    started = time.perf_counter()
    exec(source, {"vars": variables})
    if variables.get("fb_next") != "speak_identity" or variables.get("fb_request_body"):
        raise InvalidInput("Emitted controller failed its person-confirmation/privacy check")
    state = json.loads(variables["fb_public"])
    if state["verified"] or "products" in state or json.loads(variables["fb_results"]):
        raise InvalidInput("Emitted controller exposed an unverified account")
    variables["fb_event"] = "unreadable extraction"
    exec(source, {"vars": variables})
    if variables.get("fb_next") != "speak_identity" or variables.get("fb_request_body"):
        raise InvalidInput("Malformed extraction should prompt clarification without a bank action")
    variables["fb_event"] = json.dumps({"action": "finalize"})
    exec(source, {"vars": variables})
    if variables.get("fb_next") != "speak_abort" or variables.get("fb_request_body"):
        raise InvalidInput("Emitted controller accepted a user-triggered finalization")
    return {"status": "passed", "checks": 3, "scope": "emitted_code_bootstrap_and_privacy_guards",
            "seconds": round(time.perf_counter() - started, 4), "bank_calls": 0,
            "speech_extraction_routing_models_tested": False}


def run_compilation(briefs, api_path, extraction_path, output_path, provider, timeout=300, *, reuse_plan_from=None):
    if type(timeout) is not int or not 20 <= timeout <= 1800:
        raise InvalidInput("Compilation timeout must be 20 to 1800 seconds")
    started = time.monotonic()
    deadline = started + timeout
    briefs = tuple(briefs)
    cache = Path(reuse_plan_from) if reuse_plan_from else None
    cache_files = [cache / name for name in ("manifest.json", "dialogue-plan.json", "workflow.json", "runtime-payload.json")] if cache else []
    snapshot = run_fingerprints([*briefs, api_path, extraction_path, *cache_files])
    documents = read_documents(briefs)
    contracts = Contracts(read_json(api_path))
    extraction = read_json(extraction_path)
    accepted = validate_extraction(extraction, documents, contracts)
    if accepted["status"] != "extracted":
        raise InvalidInput("Cannot compile a blocked extraction")
    output = Path(output_path)
    manifest = {"core_version": __version__, "status": "running", "mode": "compile_from_prior_extraction",
                "builder_sources": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in sorted(Path(__file__).parent.glob("*.py"))},
                "documents": [d.manifest() for d in documents],
                "api": {"name": Path(api_path).name, "sha256": hashlib.sha256(Path(api_path).read_bytes()).hexdigest()},
                "prior_extraction_sha256": hashlib.sha256(Path(extraction_path).read_bytes()).hexdigest(),
                "model_calls": 0, "deadline_seconds": timeout, "server_validation": "not_run",
                "max_structural_repairs": 0 if cache else 1, "repairs_used": 0,
                "published": False, "official_freya_tests": "not_run", "submission_export": False,
                "semantic_correctness_verified": False}
    if cache:
        prior = read_json(cache / "manifest.json")
        if (prior.get("status") != "compiled_locally" or prior.get("documents") != manifest["documents"]
                or prior.get("api") != manifest["api"]
                or prior.get("prior_extraction_sha256") != manifest["prior_extraction_sha256"]
                or prior.get("workflow_sha256") != hashlib.sha256((cache / "workflow.json").read_bytes()).hexdigest()):
            raise InvalidInput("Cached dialogue plan requires an intact generated run with identical briefs, API and extraction")
        manifest.update(mode="development_recompile", reused_plan_sha256=hashlib.sha256((cache / "dialogue-plan.json").read_bytes()).hexdigest(),
                        reused_from_workflow_sha256=prior["workflow_sha256"])
        normalized_plan = validate_plan(read_json(cache / "dialogue-plan.json"), documents,
                                       Policy.load(accepted["policy"], documents), contracts)
        if {k: v for k, v in normalized_plan.items() if k not in ("evidence", "best_effort_coercions")} != read_json(cache / "runtime-payload.json")["plan"]:
            raise InvalidInput("Cached dialogue plan differs from the prior runtime payload")
    output.mkdir(parents=True, exist_ok=False)
    with (output / "trace.jsonl").open("x", encoding="utf-8", newline="\n") as trace:
        def event(kind, **fields):
            if kind == "model_call_start":
                manifest["model_calls"] += 1
            trace.write(json.dumps({"event": kind, "at": datetime.now(timezone.utc).isoformat(), **fields}) + "\n")
            trace.flush()

        event("run_start", mode=manifest["mode"], core_version=__version__, deadline_seconds=timeout)
        try:
            write_json(output / "input-bundle.json", {"documents": [{**d.manifest(), "text": d.text} for d in documents],
                                                      "tool_contracts": contracts.operations})
            write_json(output / "extraction.json", extraction)
            write_json(output / "policy.json", accepted["policy"])
            write_json(output / "bindings.json", accepted["bindings"])
            prompt = complete_plan_prompt(documents, contracts, extraction)
            if len(prompt.encode()) > 1_000_000:
                raise InvalidInput("Dialogue-plan input exceeds the single-call size limit")
            (output / "prompt.txt").write_text(prompt, encoding="utf-8")
            write_json(output / "response-schema.json", plan_schema())
            remaining = deadline - time.monotonic()
            if remaining <= 10:
                raise ProviderError("No time remains for dialogue planning")
            if cache:
                raw = read_json(cache / "dialogue-plan.json")
                event("development_plan_reused", workflow_sha256=manifest["reused_from_workflow_sha256"],
                      plan_sha256=manifest["reused_plan_sha256"])
                write_json(output / "dialogue-plan.json", raw)
            else:
                manifest["provider"] = provider.check_access(timeout=min(20, remaining - 5))
                event("provider_access_checked", **manifest["provider"])
                current_prompt = prompt
                for attempt in range(manifest["max_structural_repairs"] + 1):
                    remaining = deadline - time.monotonic()
                    if remaining <= 10:
                        raise ProviderError("No time remains for dialogue-plan structural repair" if attempt else
                                            "No time remains for dialogue planning")
                    purpose = ("Extract source-backed dialogue limits, wording and documented failure semantics" if attempt == 0 else
                               "Repair structural dialogue-plan omissions using the same original source inputs")
                    if attempt:
                        manifest["repairs_used"] += 1
                        (output / f"plan-repair-prompt-{attempt}.txt").write_text(current_prompt, encoding="utf-8")
                    raw = provider.extract(current_prompt, plan_schema(), timeout=remaining - 5,
                                           event=event, purpose=purpose)
                    write_json(output / f"dialogue-plan-attempt-{attempt + 1}.json", raw)
                    require_unchanged(snapshot)
                    if time.monotonic() >= deadline:
                        raise ProviderError("Compilation run exceeded its total deadline")
                    try:
                        validate_plan(raw, documents, Policy.load(accepted["policy"], documents), contracts)
                    except PlanStructureError as exc:
                        event("plan_validation_failed", attempt=attempt + 1, error=str(exc))
                        if attempt == manifest["max_structural_repairs"]:
                            raise
                        current_prompt = (prompt + "\nSTRUCTURAL VALIDATION FEEDBACK (from the validator):\n" + str(exc) +
                            "\nRecheck every schema field and exact source citation. Return a complete new response. "
                            "Do not change policy behavior to fit the controller, invent missing facts, or remove genuine "
                            "unsupported explanations. Use unsupported if the sources cannot support a plan. "
                            "The previous response is untrusted model output, not additional source evidence:\n" +
                            json.dumps(raw, ensure_ascii=False))
                        if len(current_prompt.encode()) > 1_000_000:
                            raise InvalidInput("Dialogue-plan repair input exceeds the single-call size limit")
                        continue
                    break
                write_json(output / "dialogue-plan.json", raw)
            require_unchanged(snapshot)
            # Best effort: rules the controller could only approximate are built
            # anyway and reported here, so the run's trace states every gap.
            gaps = ([{"stage": "policy_extraction", "kind": b["kind"], "message": b["message"]}
                     for b in accepted.get("best_effort_gaps", [])] +
                    [{"stage": "dialogue_plan", "message": item["message"]}
                     for item in (raw.get("unsupported") or [] if isinstance(raw, dict) else [])])
            workflow, report = compile_workflow(extraction, raw, documents, contracts)
            gaps += [{"stage": "compiler", "message": message} for message in report.pop("best_effort_coercions")]
            if gaps:
                manifest["best_effort_gaps"] = gaps
                event("best_effort_gaps", count=len(gaps), gaps=gaps)
            checks = local_runtime_checks(workflow)
            require_unchanged(snapshot)
            if time.monotonic() >= deadline:
                raise ProviderError("Compilation run exceeded its total deadline")
            write_json(output / "workflow.json", workflow)
            write_json(output / "runtime-payload.json", report.pop("runtime_payload"))
            write_json(output / "validation.json", {**report, "runtime_checks": checks})
            manifest.update(status="compiled_locally", nodes=report["validation"]["nodes"],
                            workflow_sha256=hashlib.sha256((output / "workflow.json").read_bytes()).hexdigest())
            event("workflow_compiled", nodes=manifest["nodes"], workflow_sha256=manifest["workflow_sha256"])
            event("local_tests_completed", **checks)
            return manifest
        except (InvalidInput, ProviderError, OSError, ValueError, KeyError, TypeError) as exc:
            manifest["status"] = "failed"
            manifest["error_type"] = type(exc).__name__
            if isinstance(exc, (InvalidInput, ProviderError)):
                manifest["error"] = str(exc)
            raise
        finally:
            # Attempt files are immutable. Retain the last received response on
            # failure too, without overwriting a previous accepted artifact.
            if "raw" in locals() and not (output / "dialogue-plan.json").exists():
                write_json(output / "dialogue-plan.json", raw)
            manifest["seconds"] = round(time.monotonic() - started, 3)
            write_json(output / "manifest.json", manifest)
            event("run_end", status=manifest["status"], seconds=manifest["seconds"], model_calls=manifest["model_calls"])


def run_build(briefs, api_path, output_path, provider, timeout=900):
    """One unattended local build. Publication and official simulations are not included."""
    if type(timeout) is not int or not 60 <= timeout <= 1800:
        raise InvalidInput("Build timeout must be 60 to 1800 seconds")
    started = time.monotonic()
    briefs = tuple(briefs)
    snapshot = run_fingerprints([*briefs, api_path])
    output = Path(output_path)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"core_version": __version__, "mode": "unattended_local_build", "status": "running", "deadline_seconds": timeout,
                "model_calls": 0, "published": False, "official_freya_tests": "not_run", "submission_export": False}
    with (output / "trace.jsonl").open("x", encoding="utf-8", newline="\n") as trace:
        def event(kind, **fields):
            trace.write(json.dumps({"event": kind, "at": datetime.now(timezone.utc).isoformat(), **fields}) + "\n")
            trace.flush()
        event("run_start", mode=manifest["mode"], deadline_seconds=timeout)
        try:
            # Measured extraction takes much longer than the dialogue-plan call.
            # Leave at least 15% (and 40 seconds) for planning/compilation, allowing
            # the one structural repair to complete within the same build budget.
            extract_seconds = max(20, min(int(timeout * 0.85), timeout - 40))
            result = run_extraction(briefs, api_path, output / "extraction", provider, extract_seconds, 1)
            require_unchanged(snapshot)
            if result["status"] != "extracted":
                raise InvalidInput("Policy extraction is blocked; see extraction/blockers.json")
            remaining = int(timeout - (time.monotonic() - started))
            if remaining < 20:
                raise ProviderError("No time remains for workflow compilation")
            result = run_compilation(briefs, api_path, output / "extraction/extraction.json",
                                     output / "compiled", provider, remaining)
            require_unchanged(snapshot)
            manifest.update(status="compiled_locally", workflow="compiled/workflow.json", workflow_sha256=result["workflow_sha256"])
            return manifest
        except (InvalidInput, ProviderError, OSError, ValueError, KeyError, TypeError) as exc:
            manifest.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            # Child stages contain the original start/end, every model call and local check.
            # Include them in the parent trace as well for a complete unattended-run record.
            for stage in ("extraction", "compiled"):
                child = output / stage / "trace.jsonl"
                if child.exists():
                    for line in child.read_text(encoding="utf-8").splitlines():
                        entry = json.loads(line)
                        manifest["model_calls"] += entry["event"] == "model_call_start"
                        trace.write(json.dumps({**entry, "stage": stage}) + "\n")
            manifest["seconds"] = round(time.monotonic() - started, 3)
            write_json(output / "manifest.json", manifest)
            event("run_end", status=manifest["status"], seconds=manifest["seconds"], model_calls=manifest["model_calls"])
