"""Development evaluation loop: strict gates, fresh live trials, failure evidence.

This never trains model weights, edits a candidate, or sends test expectations to
the builder. Change generic builder code after reviewing feedback, regenerate,
then evaluate a new immutable candidate. A competition run still starts from
briefs with complete_run.py; this development command is not a submission run.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from builder.codex_provider import child_environment
from builder.inputs import InvalidInput, read_json
from tools.complete_run import Deadline, RunExpired, remote_recovery_state
from tools.speech_audit import audit_batch
from tools.practice_inputs import check_practice_manifest
from tools.scenario_inputs import load_case_inputs, bank_case_ids


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def case_ids(text):
    items = text.split(",")
    if not all(items) or len(set(items)) != len(items):
        raise InvalidInput("Select distinct comma-separated case IDs")
    return items


def feedback(folders, strict=()):
    """Keep evidence outside provider inputs; do not convert case answers to rules."""
    findings = []
    for folder in folders:
        folder = Path(folder)
        result = read_json(folder / "result.json")
        common = {"source": str(folder.resolve()), "workflow_sha256": result["workflow_sha256"],
                  "version": result.get("expected_version"), "simulation_id": result.get("simulation_id")}
        for row in result.get("rows", []):
            if row.get("pass") is not True:
                kind = ("ledger_failure" if row.get("ledger_pass") is False else
                        "official_inconclusive" if row.get("official_outcome") == "inconclusive" else
                        "evaluation_incomplete" if row.get("call_ended") is False or
                            ("official_outcome" in row and row.get("official_outcome") is None) else
                        "official_failure" if row.get("official_outcome") in {"fail", "error"} else
                        "automated_failure")
                findings.append({**common, "case": row.get("id"), "call_id": row.get("call_id"),
                    "priority": "P0" if row.get("id") in strict else "P1", "kind": kind,
                    "official_outcome": row.get("official_outcome"), "ledger_pass": row.get("ledger_pass"),
                    "evidence": row.get("failed", []), "blocks_promotion": True})
            for issue in row.get("evaluation_issues", []):
                findings.append({**common, "case": row.get("id"), "priority": "P0",
                    "kind": "evaluation_issue", "evidence": [issue], "blocks_promotion": True})
        review_path = folder / "manual-review.json"
        audit_path = folder / "speech-audit.json"
        if audit_path.exists():
            audit = read_json(audit_path)
            for finding in audit.get("findings", []):
                findings.append({**common, "case": finding.get("case"), "priority": "P0",
                    "kind": "prepared_speech_violation", "evidence": [finding], "blocks_promotion": True})
            if audit.get("status") != "passed" and not audit.get("findings"):
                findings.append({**common, "priority": "P0", "kind": "prepared_speech_violation",
                    "evidence": ["Prepared speech delivery could not be verified"], "blocks_promotion": True})
        if review_path.exists():
            review = read_json(review_path)
            for finding in review.get("findings", []):
                robustness = finding.get('kind') == 'robustness' and finding.get('policy_violation') is False
                priority = finding.get('priority') if robustness and finding.get('priority') in {'P1', 'P2'} else 'P0'
                findings.append({**common, "case": finding.get("case"), "priority": priority,
                    "kind": "robustness_review" if robustness else "transcript_review",
                    "evidence": [finding.get("observed", "Review finding")],
                    "blocks_promotion": True})
            for issue in review.get("evaluation_issues", []):
                findings.append({**common, "case": issue.get("case") if isinstance(issue, dict) else None,
                    "priority": "P0", "kind": "manual_evaluation_issue", "evidence": [issue],
                    "blocks_promotion": True})
            if review.get("submission_ready") is False and not review.get("findings") and not review.get("evaluation_issues"):
                findings.append({**common, "priority": "P0", "kind": "transcript_review",
                    "evidence": ["Review explicitly blocks this candidate"], "blocks_promotion": True})
    findings.sort(key=lambda item: item["priority"])
    return {"status": "feedback_ready", "findings": findings,
        "repair_order": ["P0: strict policy, privacy, identity limits and honest bank-result handling",
                         "P1: remaining policy and conversation failures", "Quality only after all policy gates pass"],
        "repair_protocol": ["Identify the generic failure using the source brief, call and bank evidence",
                            "Add a regression with different dates, values or policy parameters",
                            "Fix the builder, regenerate without editing workflow JSON, run this loop again"],
        "provider_input": False, "automatic_code_repair": False}


def check_result(result, expected, digest, seen_calls, seen_simulations):
    """Missing, reused or wrong-version evidence never counts as a repeat pass."""
    rows = result.get("rows", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return False, "Malformed test rows"
    if (result.get("status") != "completed" or result.get("version_verified") is not True
            or result.get("workflow_sha256") != digest or result.get("total") != len(expected)
            or len(rows) != len(expected) or {r.get("id") for r in rows} != set(expected)):
        return False, "Incomplete, mismatched or unverified test evidence"
    sim = result.get("simulation_id")
    calls = [row.get("call_id") for row in rows]
    if (not sim or sim in seen_simulations or not all(calls) or len(set(calls)) != len(calls)
            or seen_calls.intersection(calls)):
        return False, "A repeated simulation/call is not a fresh trial"
    seen_simulations.add(sim)
    seen_calls.update(calls)
    passed = result.get("passed") == len(expected) and all(
        r.get("pass") is True and r.get("ledger_pass") is True and r.get("official_outcome") == "pass"
        and r.get("call_ended") is True and not r.get("evaluation_issues") for r in rows)
    return passed, "All selected cases passed" if passed else "At least one case failed"


def execute(args, seconds, log):
    # Stream progress without copying potentially sensitive raw stderr into reports.
    with Path(log).open("w", encoding="utf-8") as stream:
        process = subprocess.run([sys.executable, "-B", *map(str, args)], cwd=ROOT,
            env=child_environment(), stdout=stream, stderr=subprocess.STDOUT,
            timeout=seconds, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return process.returncode


def evaluate(compiled, output, *, brief, strict, cases, history=(), live=False,
             env=None, slot=None, reps=3, seconds=1800, concurrency=5, style=None, case_inputs=None,
             run=execute, clock=time.monotonic):
    if reps not in (1, 2, 3) or concurrency not in range(1, 6):
        raise InvalidInput("Use one to three fresh repetitions and one to five concurrent calls")
    if not strict or not set(strict).issubset(cases) or len(set(cases)) != len(cases):
        raise InvalidInput("The nonempty strict gate must be a subset of the selected regression cases")
    if live and (not env or not slot):
        raise InvalidInput("Live mode needs the credential-file path and a reusable workflow slot")
    compiled, output = Path(compiled).resolve(), Path(output).resolve()
    workflow = compiled / "workflow.json"
    digest = hashlib.sha256(workflow.read_bytes()).hexdigest()
    candidate_manifest = read_json(compiled / "manifest.json")
    if candidate_manifest.get("workflow_sha256") != digest:
        raise InvalidInput("Candidate differs from its generation manifest")
    input_snapshot = load_case_inputs(case_inputs, cases) if case_inputs else None
    if live:
        practice = check_practice_manifest(candidate_manifest,
            bank_case_ids(input_snapshot['cases']) if input_snapshot else cases)
        if practice['brief'] != brief:
            raise InvalidInput("Selected practice cases do not match the requested brief")
    budget = Deadline(seconds, clock)
    output.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "mode": "development_evaluation", "workflow_sha256": digest,
        "compiled": str(compiled), "strict_cases": strict, "regression_cases": cases, "required_repetitions": 3,
        "requested_repetitions": reps, "stages": [], "submission_ready": False,
        "automatic_code_repair": False, "test_oracles_sent_to_builder": False,
        "concurrency": concurrency, "style": style, "deadline_seconds": seconds,
        "evaluation_sources": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "tools").glob("*.py"))}, "case_trials": {case: 0 for case in cases}}
    completed = []
    active = None
    seen_calls, seen_simulations = set(), set()

    def event(name, **fields):
        with (output / "trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": name, "at": datetime.now(timezone.utc).isoformat(), **fields}) + "\n")
        write(output / "manifest.json", report)
        print(json.dumps({"event": name, **fields}), flush=True)

    def stage(name, args, minimum=1):
        if hashlib.sha256(workflow.read_bytes()).hexdigest() != digest:
            raise InvalidInput("Candidate changed during evaluation")
        left = budget.remaining(20)
        if left < minimum:
            raise RunExpired("Insufficient budget to start " + name)
        event("stage_start", stage=name, remaining_seconds=left)
        code = run(args, left, output / (name + ".log"))
        budget.remaining()
        report["stages"].append({"name": name, "exit_code": code})
        event("stage_end", stage=name, exit_code=code)
        return code

    try:
        prior = feedback(history, strict)
        write(output / "prior-feedback.json", prior)
        if input_snapshot:
            write(output / 'evaluation-case-inputs.json', input_snapshot)
            # Child stages consume this immutable local list rather than rereading
            # the caller's scenario file if it changes during a long evaluation.
            write(output / 'evaluation-cases.json', input_snapshot['cases'])
        for name, args in (("strict_local", ["-m", "unittest", "tests.test_strict_regressions", "-q"]),
                           ("regression_local", ["-m", "unittest", "discover", "-s", "tests", "-q"]),
                           ("artifact_local", ["-m", "tests.audit_generated_workflow", "--compiled", compiled,
                              "--brief", brief, "--out", output / "artifact-audit.json"])):
            if stage(name, args):
                report["status"] = "blocked_local"
                return report
        if not live:
            report["status"] = "local_gates_passed"
            return report
        active = output / "publication"
        common = ["tools/live_smoke.py", "--workflow", workflow, "--env", Path(env).resolve(),
                  "--slot", slot, "--concurrency", str(concurrency)]
        if style:
            common.extend(["--style", style])
        if input_snapshot:
            common.extend(['--cases', output / 'evaluation-cases.json'])
        if stage("publish", [*common, "publish", "--out", active,
                 "--budget", str(min(1800, budget.remaining(20)))], minimum=60):
            report["status"] = "blocked_publication"
            return report
        # The strict batch is part of repetition one, not an extra repetition.
        # Finish its remaining cases, then run fresh complete repetitions.
        rest = [case for case in cases if case not in strict]
        stages = [("strict_live", strict)] + ([("regression_1", rest)] if rest else [])
        stages += [("regression_" + str(i+1), cases) for i in range(1, reps)]
        for name, selected in stages:
            active = output / name
            active.mkdir()
            # Only publication evidence is reused. Each simulate invocation creates
            # fresh practice sessions and one new simulation, never another run of
            # an old customer ledger. The helper independently rechecks publication.
            shutil.copyfile(output / "publication/publication.json", active / "publication.json")
            available = min(1800, budget.remaining(30))
            code = stage(name, [*common, "simulate", "--out", active, "--case", ",".join(selected),
                               "--budget", str(available)], minimum=480)
            if not (active / "result.json").exists():
                report["status"] = "incomplete_live"
                return report
            completed.append(active)
            result = read_json(active / "result.json")
            ok, reason = check_result(result, selected, digest, seen_calls, seen_simulations)
            for row in result.get("rows", []):
                if row.get("id") in report["case_trials"]:
                    report["case_trials"][row["id"]] += 1
            report["stages"][-1].update(passed=result.get("passed"), total=result.get("total"),
                                        simulation_id=result.get("simulation_id"), evidence=reason)
            speech = audit_batch(read_json(workflow), active)
            write(active / "speech-audit.json", speech)
            event("prepared_speech_checked", stage=name, status=speech["status"], findings=len(speech["findings"]))
            if not ok or code:
                report["status"] = "blocked_" + name
                return report
            if speech["status"] != "passed":
                report["status"] = "blocked_speech_" + name
                return report
        same_candidate_findings = [f for f in feedback([*history, *completed], strict)["findings"]
                                   if f["workflow_sha256"] == digest]
        report["status"] = ("blocked_prior_evidence" if same_candidate_findings else
                            "automated_gates_passed" if reps == 3 else "partial_repetition_pass")
        return report
    except (InvalidInput, RunExpired, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        report.update(status="incomplete", error_type=type(exc).__name__)
        if isinstance(exc, (InvalidInput, RunExpired)):
            report["error"] = str(exc)
        return report
    finally:
        if active:
            recovery = remote_recovery_state(active)
            if recovery:
                report.update(recovery, recovery_directory=str(active))
        try:
            write(output / "feedback.json", feedback([*history, *completed], strict))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report.update(status="incomplete_feedback", feedback_error_type=type(exc).__name__)
        report["seconds"] = budget.elapsed()
        report["limits"] = "Automated gates are evidence, not proof of transcript compliance or unseen-brief generalization. No export is made."
        event("evaluation_end", status=report["status"], seconds=report["seconds"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiled", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--brief", required=True, choices=("A", "B"))
    parser.add_argument("--strict", required=True, help="Comma-separated practice cases for the first live gate")
    parser.add_argument("--cases", required=True, help="All selected regression cases")
    parser.add_argument("--history", action="append", default=[], help="Prior live-result folder; evaluator only")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env")
    parser.add_argument("--slot")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--style", help="Optional customer speaking style from the kit's STYLES")
    parser.add_argument("--case-inputs", help="Evaluation-only scenario JSON; --cases still selects comma-separated IDs")
    args = parser.parse_args()
    result = evaluate(args.compiled, args.out, brief=args.brief, strict=case_ids(args.strict),
        cases=case_ids(args.cases), history=args.history, live=args.live, env=args.env, slot=args.slot,
        reps=args.reps, seconds=args.seconds, concurrency=args.concurrency, style=args.style, case_inputs=args.case_inputs)
    print(json.dumps(result))
    return 0 if result["status"] in {"automated_gates_passed", "local_gates_passed", "partial_repetition_pass"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
