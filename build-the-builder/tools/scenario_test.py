"""Test a saved phone agent against scenarios the builder wrote from a case's documents.

    python tools/scenario_test.py <agent_id> <scenarios.json> [--max-seconds 420]

scenarios.json is a list of objects:
  {"name": "...", "prompt": "how the simulated customer behaves", "success": ["criterion", ...],
   "variables": {"<call variable>": "<value>", ...}}
Prints one PASS/FAIL line per scenario with the judge's reason and a short transcript
(tool calls included), so the builder can fix the workflow and test again. Generic:
nothing here knows any case content.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import freya  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("agent_id")
    parser.add_argument("scenarios")
    parser.add_argument("--max-seconds", type=int, default=420)
    args = parser.parse_args()
    scenarios = json.loads(Path(args.scenarios).read_text(encoding="utf-8"))
    specs = [{"name": "builder-test " + s["name"], "prompt": s["prompt"],
              "successCriteria": "All of these are true:\n- " + "\n- ".join(s["success"]),
              "tags": ["builder-test"]} for s in scenarios]
    personas = freya.create_personas(specs)["personas"]
    variables = {p["id"]: {k: str(v) for k, v in s.get("variables", {}).items()} for p, s in zip(personas, scenarios)}
    names = {p["id"]: s["name"] for p, s in zip(personas, scenarios)}
    sim_id, iteration_id, status = freya.run_simulation(args.agent_id, "builder test", variables, max_seconds=args.max_seconds)
    runs = freya.get_iteration(sim_id, iteration_id)["runs"]
    passed = 0
    for run in runs:
        name = names.get(run.get("personaId"), run.get("personaName", "?"))
        ok = run.get("outcome") == "pass"
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'} {name}: {str(run.get('reason') or '')[:700]}")
        if run.get("callId"):
            try:
                messages = freya.get_call(run["callId"]).get("messages") or []
            except Exception as exc:  # a missing transcript must not stop the report
                print(f"  (transcript unavailable: {type(exc).__name__})")
                continue
            for m in messages:
                text = m.get("message") if isinstance(m.get("message"), str) else json.dumps(m.get("message"), ensure_ascii=False)
                if m.get("role") in ("assistant", "user"):
                    print(f"  {m['role'][:4].upper()}: {text[:220]}")
                elif '"function_call_start"' in text and ("api_call" in text or "end_call" in text):
                    print(f"  TOOL: {text[:220]}")
    print(f"\n{passed}/{len(runs)} scenarios passed (simulation {sim_id}, status {status})")


if __name__ == "__main__":
    main()
