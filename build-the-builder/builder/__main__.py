"""Offline checks and bounded, explicit model-backed local build commands."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

from . import __version__
from .contracts import Contracts
from .inputs import InvalidInput, read_documents, read_json
from .policy import Policy
from .codex_provider import CodexProvider, ProviderError
from .extract_run import run_extraction
from .compile_run import run_compilation, run_build


def write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Freya builder: source-backed policy extraction and guarded local workflow generation")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Read briefs/OpenAPI and emit an offline input bundle and unbound tool templates")
    prepare.add_argument("--brief", action="append", required=True, help="Repeat in intended source order; order alone does not resolve amendments")
    prepare.add_argument("--api", required=True)
    prepare.add_argument("--out", required=True, help="New output directory; existing directories are never overwritten")
    check = commands.add_parser("check-policy", help="Validate a source-backed offline policy fixture; this does not extract it from prose")
    check.add_argument("--brief", action="append", required=True)
    check.add_argument("--policy", required=True)
    extract = commands.add_parser("extract", help="Use existing Codex ChatGPT auth to extract source-backed policy; consumes model quota")
    extract.add_argument("--brief", action="append", required=True)
    extract.add_argument("--api", required=True)
    extract.add_argument("--out", required=True, help="New output directory")
    extract.add_argument("--model", required=True, help="Explicit builder model, separate from the Freya phone-agent model")
    extract.add_argument("--codex-exe", help="Native Codex executable; otherwise discovered on PATH")
    extract.add_argument("--codex-home", help="Existing Codex authentication directory; no keys are copied or printed")
    extract.add_argument("--timeout", type=int, default=300, help="Total extraction budget in seconds, 20–1800 (default: 300)")
    extract.add_argument("--max-repairs", type=int, choices=(0, 1), default=1, help="Structural repair calls within the same deadline (default: 1)")
    for name, help_text in (("compile", "Generate a local workflow from an existing extraction; consumes model quota"),
                            ("build", "Read briefs and build a local workflow unattended; consumes model quota")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--brief", action="append", required=True)
        command.add_argument("--api", required=True)
        command.add_argument("--out", required=True, help="New output directory")
        command.add_argument("--model", required=name == "build")
        command.add_argument("--codex-exe")
        command.add_argument("--codex-home")
        command.add_argument("--timeout", type=int, default=900 if name == "build" else 300,
                             help="Overall local run budget in seconds, at most 1800")
        if name == "compile":
            command.add_argument("--extraction", required=True, help="Prior extraction.json, validated against these original inputs")
            command.add_argument("--reuse-plan-from", help="Development only: prior generated directory with identical inputs; skips model calls")
    args = parser.parse_args(argv)
    started = time.monotonic()
    trace = None
    try:
        if args.command in {"compile", "build"}:
            cached = args.command == "compile" and args.reuse_plan_from
            provider = None if cached else CodexProvider(args.model, args.codex_exe, args.codex_home)
            if args.command == "compile":
                result = run_compilation(args.brief, args.api, args.extraction, args.out, provider, args.timeout,
                                         reuse_plan_from=args.reuse_plan_from)
            else:
                result = run_build(args.brief, args.api, args.out, provider, args.timeout)
            print(json.dumps({"status": result["status"], "output": args.out, "model_calls": result["model_calls"],
                              "published": False, "official_freya_tests": "not_run", "submission_export": False}))
            return 0
        if args.command == "extract":
            provider = CodexProvider(args.model, args.codex_exe, args.codex_home)
            result = run_extraction(args.brief, args.api, args.out, provider, args.timeout, args.max_repairs)
            print(json.dumps({"status": result["status"], "output": args.out, "model_calls": result["model_calls"],
                              "requirements": result["requirements"], "source_units": result["source_units"],
                              "semantic_correctness_verified": False}))
            return 0 if result["status"] == "extracted" else 3
        documents = read_documents(args.brief)
        if args.command == "check-policy":
            policy = Policy.load(read_json(args.policy), documents)
            print(json.dumps({"status": "valid_offline_policy", "scope": "numeric_and_action_rules_only",
                              "bands": len(policy.bands), "max_products": policy.max_products,
                              "evidence_verified": True, "semantic_extraction_verified": False}))
            return 0
        contracts = Contracts(read_json(args.api))
        output = Path(args.out)
        output.mkdir(parents=True, exist_ok=False)
        trace = (output / "trace.jsonl").open("x", encoding="utf-8", newline="\n")

        def event(kind, **fields):
            trace.write(json.dumps({"event": kind, "at": datetime.now(timezone.utc).isoformat(), **fields}) + "\n")
            trace.flush()

        # Trace records metadata only. Raw input text is intentionally isolated in the bundle.
        event("run_start", mode="offline_prepare", core_version=__version__)
        manifest = {"core_version": __version__, "status": "inputs_prepared",
                    "documents": [d.manifest() for d in documents],
                    "api": {"name": Path(args.api).name, "sha256": hashlib.sha256(Path(args.api).read_bytes()).hexdigest(),
                            "base_url": contracts.base_url, "tools": sorted(contracts.operations)},
                    "model_calls": 0, "network_calls": 0,
                    "policy_extraction": "not_run", "workflow_generation": "not_connected",
                    "official_freya_tests": "not_run", "tool_templates": "unbound_not_deployable"}
        write_json(output / "manifest.json", manifest)
        write_json(output / "input-bundle.json", {"documents": [{**d.manifest(), "text": d.text} for d in documents],
                                                  "tool_contracts": contracts.operations})
        write_json(output / "tool-templates.json", contracts.freya_tools())
        event("inputs_validated", documents=manifest["documents"], api=manifest["api"])
        event("run_end", status="inputs_prepared", seconds=round(time.monotonic() - started, 3), model_calls=0, network_calls=0)
        print(json.dumps({"status": "inputs_prepared", "output": str(output), "documents": len(documents),
                          "tools": len(contracts.operations), "model_calls": 0, "network_calls": 0}))
        return 0
    except (InvalidInput, ProviderError, OSError, ValueError, KeyError, TypeError) as exc:
        # Do not echo raw source content, credential-bearing values or exception payloads.
        if trace:
            trace.write(json.dumps({"event": "run_end", "status": "failed", "error_type": type(exc).__name__}) + "\n")
        message = str(exc) if isinstance(exc, (InvalidInput, ProviderError)) else "Could not process inputs or create a new output directory"
        print("Builder: " + message, file=sys.stderr)
        return 2
    finally:
        if trace:
            trace.close()


if __name__ == "__main__":
    raise SystemExit(main())
