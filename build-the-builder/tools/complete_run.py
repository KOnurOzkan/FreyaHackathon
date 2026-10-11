"""One bounded build -> publish -> smoke test -> verified export invocation.

Without --live, only the local builder runs. --live explicitly enables workspace
writes and one simulation using the supplied .env. No provider API-key fallback.
Platform stages reuse live_smoke.py, with its immutable artifacts and duplicate
creation guard. Unknown or over-budget remote runs are recorded for collection;
they never become a successful submission. This integration is locally tested
with process doubles and still requires an actual Freya end-to-end trial.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from builder.codex_provider import CodexProvider, ProviderError, child_environment
from builder.compile_run import run_build
from builder.inputs import InvalidInput, read_json
from tools.practice_inputs import check_practice_inputs, digest


class RunExpired(RuntimeError):
    pass


class Deadline:
    def __init__(self, seconds, clock=time.monotonic):
        if type(seconds) is not int or not 60 <= seconds <= 1800:
            raise InvalidInput("Overall deadline must be 60 to 1800 seconds")
        self.clock, self.start, self.seconds = clock, clock(), seconds

    def remaining(self, reserve=0):
        left = int(self.seconds - (self.clock() - self.start) - reserve)
        if left <= 0:
            raise RunExpired("The overall run deadline was reached")
        return left

    def elapsed(self):
        return round(self.clock() - self.start, 3)


def run_phase(phase, workflow, env, output, slot, case, seconds, codex_home=None):
    args = [sys.executable, "-B", str(ROOT / "tools/live_smoke.py"), phase,
            "--workflow", str(workflow), "--env", str(env), "--out", str(output),
            "--slot", slot, "--budget", str(min(1800, seconds))]
    if case:
        args += ["--case", case]
    elif phase == "export":
        # A final run has no practice case that matches its unseen brief.
        args.append("--untested")
    # Keep provider credentials out of live helper processes too. The helper loads
    # only the two explicitly authorized Freya roles from the supplied file.
    process = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=seconds, cwd=ROOT, env=child_environment(codex_home), shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # Raw stderr is intentionally not copied into a shared trace.
    return process.returncode


def remote_recovery_state(folder):
    folder = Path(folder)
    if (folder / "simulation-started.json").exists():
        try:
            started = read_json(folder / "simulation-started.json")
            if not isinstance(started, dict) or not isinstance(started.get("simulation_id"), str) or not started["simulation_id"]:
                raise InvalidInput("Missing simulation ID")
        except (OSError, ValueError):
            return {"status": "simulation_creation_uncertain"}
        result_path = folder / "result.json"
        try:
            result = read_json(result_path) if result_path.exists() else {}
        except (OSError, ValueError):
            result = {}
        # A saved running snapshot is useful evidence, not a completed collection.
        if not isinstance(result, dict) or result.get("status") not in {"completed", "failed", "paused"}:
            return {"status": "needs_collection", "simulation_id": started["simulation_id"]}
    if (folder / "simulation-attempted.json").exists() and not (folder / "simulation-started.json").exists():
        return {"status": "simulation_creation_uncertain"}
    return None


def complete_run(briefs, api, output, provider, *, seconds=1800, live=False, env=None,
                 slot="builder-practice-b", case="PUB-11", codex_home=None,
                 build=run_build, phase=run_phase, clock=time.monotonic, final=False, sleep=time.sleep):
    """final=True is the submission run on an unseen brief: build, publish, export.

    The organizer's practice customers only fit the practice briefs and API, so
    a final run records that no self-test ran instead of refusing to export.
    A final run also waits out platform outages: publish and export record no
    customer action, so they are retried while the run's own budget allows.
    """
    budget = Deadline(seconds, clock)
    if final and not live:
        raise InvalidInput("A final run publishes and exports; enable live mode")
    if live and (not env or not slot):
        raise InvalidInput("Live mode requires an explicit credential file and reusable slot")
    if live and seconds < 900:
        raise InvalidInput("Live mode needs at least 900 seconds for build, test and export reserve")
    briefs = tuple(briefs)
    compatibility = check_practice_inputs(briefs, api, case) if live and not final else None
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    platform = output / "platform"
    manifest = {"status": "running", "mode": ("complete_final_run" if final else "complete_live_run") if live else "local_build_only",
                "deadline_seconds": seconds, "model_calls": 0, "phases_completed": [],
                "official_freya_tests": "not_run", "submission_export": False,
                "runtime_model": "experimental-5", "automatic_workflow_repairs": 0,
                "orchestration_sources": {name: hashlib.sha256((ROOT / "tools" / name).read_bytes()).hexdigest()
                    for name in ("complete_run.py", "live_smoke.py", "artifact_checks.py", "practice_inputs.py")}}
    if compatibility:
        manifest["practice_compatibility"] = compatibility
    if final:
        manifest["self_test"] = "not_run: no practice customer matches an unseen brief and its API"

    def check_immutable_workflow(workflow):
        if digest(workflow) != manifest["workflow_sha256"]:
            raise InvalidInput("Workflow changed during the complete run; start a new run")
        if any(digest(ROOT / "tools" / name) != expected
               for name, expected in manifest["orchestration_sources"].items()):
            raise InvalidInput("Platform helper source changed during the complete run; start a new run")

    def save_manifest():
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    save_manifest()
    with (output / "trace.jsonl").open("x", encoding="utf-8") as trace:
        def event(kind, **fields):
            item = {"event": kind, "at": datetime.now(timezone.utc).isoformat(), **fields}
            trace.write(json.dumps(item) + "\n")
            trace.flush()
        event("run_start", mode=manifest["mode"], deadline_seconds=seconds)
        try:
            # Without a simulation, a final run can give the model-backed build
            # almost everything except publish, export and trace reserves.
            build_seconds = (min(1500, seconds - 300) if final else min(800, int(seconds * 0.45))) if live else budget.remaining()
            result = build(briefs, api, output / "build", provider, build_seconds)
            if result.get("status") != "compiled_locally":
                raise InvalidInput("Local build did not produce an accepted workflow")
            manifest["phases_completed"].append("build")
            manifest["workflow_sha256"] = result["workflow_sha256"]
            manifest["model_calls"] = result["model_calls"]
            budget.remaining()
            if not live:
                manifest["status"] = "compiled_locally"
                return manifest
            workflow = output / "build/compiled/workflow.json"
            for name in ("publish", "export") if final else ("publish", "simulate", "export"):
                attempt = 1
                while True:
                    check_immutable_workflow(workflow)
                    # Reserve an entire minute for export, checks and final trace writing.
                    reserve = 60 if name != "export" else 5
                    available = budget.remaining(reserve)
                    minimum = 480 if name == "simulate" else 60
                    if available < minimum:
                        raise RunExpired("Insufficient remaining budget for " + name)
                    manifest["phase"] = name
                    save_manifest()
                    event("phase_start", phase=name, budget_seconds=available, attempt=attempt)
                    exit_code = phase(name, workflow, env, platform, slot, None if final else case, available, codex_home)
                    budget.remaining(5)
                    check_immutable_workflow(workflow)
                    if exit_code == 0:
                        break
                    if not final:
                        raise InvalidInput("Platform " + name + " did not pass; inspect its saved trace")
                    # Final run: wait out an outage. A publish retry uses a fresh
                    # folder so no earlier attempt's records are overwritten.
                    event("phase_failed_retrying", phase=name, attempt=attempt, wait_seconds=45)
                    sleep(45)
                    attempt += 1
                    if name == "publish":
                        platform = output / ("platform-" + str(attempt))
                manifest["phases_completed"].append(name)
                event("phase_end", phase=name, status="passed")
                if name == "simulate":
                    manifest["official_freya_tests"] = "completed"
            export = read_json(platform / "export-verification.json")
            if export.get("status") != "verified_export" or export.get("workflow_sha256") != manifest["workflow_sha256"]:
                raise InvalidInput("Verified export does not match this complete build")
            manifest.update(status="verified_export", submission_export=True,
                            export=platform.name + "/submission-export.json", workflow_id=export["workflow_id"],
                            agent_id=export["agent_id"], version=export["version"])
            return manifest
        except (InvalidInput, ProviderError, RunExpired, OSError, ValueError, KeyError, TypeError,
                subprocess.TimeoutExpired) as exc:
            manifest.update(status="failed", error_type=type(exc).__name__)
            if isinstance(exc, (InvalidInput, ProviderError, RunExpired)):
                manifest["error"] = str(exc)
            recovery = remote_recovery_state(platform)
            if recovery:
                manifest.update(recovery)
                manifest["official_freya_tests"] = "incomplete"
            event("run_failed", status=manifest["status"], error_type=type(exc).__name__)
            return manifest
        finally:
            # Include original child events unchanged, with stage labels. This
            # records model calls and the actual workflow/simulation IDs, not keys.
            evidence_errors = []
            # Every platform attempt (platform, platform-2, ...) stays in the trace.
            attempts = sorted((p for p in output.glob("platform*") if p.is_dir()),
                              key=lambda p: int(p.name.partition("-")[2] or 1))
            for name, path in [("build", output / "build/trace.jsonl")] + [(p.name, p / "trace.jsonl") for p in attempts]:
                if path.exists():
                    model_calls = 0
                    try:
                        for line in path.read_text(encoding="utf-8").splitlines():
                            entry = json.loads(line)
                            if not isinstance(entry, dict) or not isinstance(entry.get("event"), str):
                                raise InvalidInput("Invalid child event")
                            model_calls += entry["event"] == "model_call_start"
                            trace.write(json.dumps({**entry, "stage": name}) + "\n")
                    except (OSError, ValueError) as exc:
                        # A forcibly ended child may leave a partial last line.
                        # Preserve the original and still close the parent trace.
                        evidence_errors.append({"artifact": name + "/trace.jsonl", "error_type": type(exc).__name__})
                    if name == "build":
                        manifest["model_calls"] = model_calls
            manifest["seconds"] = budget.elapsed()
            if (platform / "result.json").exists():
                try:
                    result = read_json(platform / "result.json")
                    if not isinstance(result, dict):
                        raise InvalidInput("Invalid test result")
                    manifest["official_freya_tests"] = result.get("status", "incomplete")
                    manifest["selected_tests"] = {k: result[k] for k in ("passed", "total", "version_verified") if k in result}
                except (OSError, ValueError) as exc:
                    evidence_errors.append({"artifact": "platform/result.json", "error_type": type(exc).__name__})
                    manifest["official_freya_tests"] = "incomplete"
            if evidence_errors:
                manifest.update(submission_export=False, evidence_errors=evidence_errors)
                if manifest["status"] not in {"needs_collection", "simulation_creation_uncertain"}:
                    manifest["status"] = "failed"
            if manifest["seconds"] >= seconds:
                manifest["deadline_exceeded"] = True
                manifest["submission_export"] = False
                if manifest["status"] in {"verified_export", "compiled_locally"}:
                    manifest["status"] = "failed"
            save_manifest()
            event("run_end", status=manifest["status"], seconds=manifest["seconds"], model_calls=manifest["model_calls"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", action="append", required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--codex-exe")
    parser.add_argument("--codex-home")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--live", action="store_true", help="Explicitly publish, start one simulation and export")
    parser.add_argument("--env")
    parser.add_argument("--slot", help="Reusable workflow slot; required with --final (default builder-practice-b)")
    parser.add_argument("--case", default="PUB-11")
    parser.add_argument("--final", action="store_true",
                        help="Submission run on an unseen brief: build, publish and export without a practice self-test")
    args = parser.parse_args()
    if args.final and not (args.live and args.slot):
        parser.error("--final needs --live and an explicit --slot for this brief")
    try:
        provider = CodexProvider(args.model, args.codex_exe, args.codex_home)
        result = complete_run(args.brief, args.api, args.out, provider, seconds=args.timeout, live=args.live,
                              env=Path(args.env).resolve() if args.env else None,
                              slot=args.slot or "builder-practice-b", case=args.case,
                              codex_home=args.codex_home, final=args.final)
    except (InvalidInput, ProviderError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, (InvalidInput, ProviderError)) else "Check paths and use a new output directory"}))
        return 2
    print(json.dumps(result))
    return 0 if result["status"] in {"compiled_locally", "verified_export"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
