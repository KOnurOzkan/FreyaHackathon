"""Development audit of an unedited practice artifact; never used by the builder.

Runs semantic events through the artifact's own code/hooks with an in-memory bank.
No speech model or official Freya simulation is involved.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import unittest

from builder.inputs import read_json
from builder.extract_run import write_json
from tests.test_workflow import WorkflowTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiled", required=True, help="Generated directory containing workflow.json and runtime-payload.json")
    parser.add_argument("--out", required=True, help="New audit JSON file outside the generated run")
    parser.add_argument("--brief", choices=("A", "B"), default="B")
    args = parser.parse_args()
    folder = Path(args.compiled)
    workflow = read_json(folder / "workflow.json")
    payload = read_json(folder / "runtime-payload.json")
    digest = hashlib.sha256((folder / "workflow.json").read_bytes()).hexdigest()
    if read_json(folder / "manifest.json")["workflow_sha256"] != digest:
        raise ValueError("Generated workflow changed after compilation")
    if Path(args.out).exists():
        raise FileExistsError("Use a new audit output file")

    class GeneratedAudit(WorkflowTests):
        case_numbers = tuple(range(1, 11)) if args.brief == "A" else (11, 12, 13)

        @classmethod
        def setUpClass(cls):
            cls.compiled = {args.brief: (workflow, {"runtime_payload": payload})}

    names = ["test_generated_code_runs_all_public_ledger_scenarios",
             "test_privacy_fragments_corrections_and_swapped_date_hint",
             "test_unclear_readback_and_correction_never_record_old_terms",
             "test_transfer_after_one_write_preserves_it_and_never_adds_notes",
             "test_two_documented_failures_keep_successful_other_product",
             "test_human_help_offer_pauses_recording_and_acceptance_resumes_it"]
    if args.brief == "A":
        names = ["test_generated_code_runs_all_public_ledger_scenarios",
                 "test_unknown_transport_result_stops_without_retry_or_claimed_success",
                 "test_model_cannot_finalize_and_missing_extraction_does_not_repeat_answer",
                 "test_callback_one_correction_and_privacy_refusal_close_cleanly",
                 "test_amount_is_retained_across_date_clarification_and_date_only_correction",
                 "test_below_floor_amount_without_date_gets_floor_offer"]
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(GeneratedAudit(name) for name in names))
    report = {"status": "passed" if result.wasSuccessful() else "failed", "scope": "generated_" + args.brief + "_workflow_local_semantic_events",
              "workflow_sha256": digest,
              "tests": result.testsRun, "public_ledger_scenarios": [f"PUB-{n:02d}" for n in GeneratedAudit.case_numbers],
              "additional_guard_tests": 5, "seconds": round(time.perf_counter() - started, 3),
              "official_freya_tests": "not_run", "speech_extraction_routing_models_tested": False,
              "failed_tests": [str(test) for test, _ in result.failures + result.errors]}
    write_json(Path(args.out), report)
    print(json.dumps(report))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
