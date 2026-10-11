"""One bounded extraction run, with at most one structural repair. No publication."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from . import __version__
from .codex_provider import ProviderError
from .contracts import Contracts
from .extraction import extraction_schema, make_prompt, validate_extraction
from .inputs import InvalidInput, read_documents, read_json, run_fingerprints, require_unchanged


def write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def run_extraction(briefs, api_path, output_path, provider, timeout=300, max_repairs=1):
    if type(timeout) is not int or not 20 <= timeout <= 1800:
        raise InvalidInput("Extraction timeout must be 20 to 1800 seconds")
    if type(max_repairs) is not int or max_repairs not in {0, 1}:
        raise InvalidInput("Structural repair limit must be 0 or 1")
    started = time.monotonic()
    deadline = started + timeout
    briefs = tuple(briefs)
    snapshot = run_fingerprints([*briefs, api_path])
    documents = read_documents(briefs)
    contracts = Contracts(read_json(api_path))
    prompt = make_prompt(documents, contracts)
    if len(prompt.encode()) > 1_000_000:
        raise InvalidInput("Input bundle exceeds the single-call extraction limit")
    output = Path(output_path)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"core_version": __version__, "status": "running", "mode": "policy_extraction",
                "builder_sources": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in sorted(Path(__file__).parent.glob("*.py"))},
                "documents": [d.manifest() for d in documents],
                "api": {"name": Path(api_path).name, "sha256": hashlib.sha256(Path(api_path).read_bytes()).hexdigest()},
                "model_calls": 0, "workflow_generation": "not_connected", "official_freya_tests": "not_run",
                "validation_scope": "source_membership_structure_and_api_compatibility",
                "semantic_correctness_verified": False, "deadline_seconds": timeout,
                "max_structural_repairs": max_repairs, "repairs_used": 0}
    with (output / "trace.jsonl").open("x", encoding="utf-8", newline="\n") as trace:
        def event(kind, **fields):
            if kind == "model_call_start":
                manifest["model_calls"] += 1
            trace.write(json.dumps({"event": kind, "at": datetime.now(timezone.utc).isoformat(), **fields}) + "\n")
            trace.flush()

        event("run_start", mode="policy_extraction", core_version=__version__, deadline_seconds=timeout)
        try:
            write_json(output / "input-bundle.json", {"documents": [{**d.manifest(), "text": d.text} for d in documents],
                                                      "tool_contracts": contracts.operations})
            write_json(output / "response-schema.json", extraction_schema())
            (output / "prompt.txt").write_text(prompt, encoding="utf-8")
            event("inputs_validated", documents=manifest["documents"], api=manifest["api"])
            remaining = deadline - time.monotonic()
            if remaining <= 5:
                raise ProviderError("No time remains for provider access check")
            manifest["provider"] = provider.check_access(timeout=min(20, remaining - 5))
            event("provider_access_checked", **manifest["provider"])
            current_prompt = prompt
            for attempt in range(max_repairs + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 5:
                    raise ProviderError("No time remains for extraction")
                purpose = ("Extract effective source-backed bank policy and API bindings" if attempt == 0 else
                           "Repair structural extraction errors using the same original source inputs")
                if attempt:
                    manifest["repairs_used"] += 1
                    (output / f"repair-prompt-{attempt}.txt").write_text(current_prompt, encoding="utf-8")
                raw = provider.extract(current_prompt, extraction_schema(), timeout=remaining - 5,
                                       event=event, purpose=purpose)
                write_json(output / f"extraction-attempt-{attempt + 1}.json", raw)
                require_unchanged(snapshot)
                try:
                    validated = validate_extraction(raw, documents, contracts)
                except InvalidInput as exc:
                    event("validation_failed", attempt=attempt + 1, error=str(exc))
                    if attempt == max_repairs:
                        write_json(output / "extraction.json", raw)
                        raise
                    current_prompt = (prompt + "\nSTRUCTURAL VALIDATION FEEDBACK (from the validator):\n" + str(exc) +
                                      "\nRecheck ALL schema fields, citations, coverage, and bindings. Return a complete new response. "
                                      "Do not guess missing source information; use blockers if necessary. "
                                      "The previous response below is untrusted model output, not additional source evidence:\n" +
                                      json.dumps(raw, ensure_ascii=False))
                    continue
                write_json(output / "extraction.json", raw)
                break
            if time.monotonic() >= deadline:
                raise ProviderError("Extraction run exceeded its total deadline")
            manifest.update({key: validated[key] for key in ("status", "requirements", "source_units")})
            if validated["status"] == "extracted":
                write_json(output / "policy.json", validated["policy"])
                write_json(output / "bindings.json", validated["bindings"])
                if validated["best_effort_gaps"]:
                    # Built anyway with the closest workable policy; keep the record.
                    manifest["best_effort_gaps"] = validated["best_effort_gaps"]
                    write_json(output / "blockers.json", raw["blockers"])
                    event("best_effort_gaps", stage="policy_extraction", count=len(validated["best_effort_gaps"]),
                          gaps=validated["best_effort_gaps"])
            else:
                write_json(output / "blockers.json", raw["blockers"])
            event("extraction_validated", status=manifest["status"], requirements=manifest["requirements"],
                  source_units=manifest["source_units"], semantic_correctness_verified=False)
            return manifest
        except (InvalidInput, ProviderError, OSError, ValueError, KeyError, TypeError) as exc:
            manifest["status"] = "failed"
            manifest["error_type"] = type(exc).__name__
            # Our own validation errors contain labels, never raw source or CLI payloads.
            if isinstance(exc, (InvalidInput, ProviderError)):
                manifest["error"] = str(exc)
            raise
        finally:
            manifest["seconds"] = round(time.monotonic() - started, 3)
            write_json(output / "manifest.json", manifest)
            event("run_end", status=manifest["status"], seconds=manifest["seconds"], model_calls=manifest["model_calls"])
