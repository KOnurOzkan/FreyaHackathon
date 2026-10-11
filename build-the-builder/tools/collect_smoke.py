"""Collect an existing smoke simulation after a connection failure.

This command only reads remote state. It never starts or retries a simulation.
Recovered customer secrets are kept in memory and redacted from every artifact.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.live_smoke import SmokeError, load_credentials, redact, read_retry, observe_iteration, scored_row, bounded_urlopen
from tools.artifact_checks import test_version_evidence
from tools.call_download import get_call as download_call
from tools.scenario_inputs import recovery_case_inputs
from tools.call_artifacts import customer_secrets, persist_call_snapshot


def match_runs(started, runs, allow_unfinished=False):
    """Map cases to runs. A stopped (paused/failed) simulation may leave runs
    without a call; with allow_unfinished they are returned for recording."""
    expected = started.get('cases')
    if expected is None:
        if len(runs) != 1 or ',' in started['case']:
            raise SmokeError('Legacy recovery requires exactly one case')
        expected = {started['case']: runs[0].get('personaId')}
    if not expected or len(runs) != len(expected):
        raise SmokeError('The selected run set is incomplete')
    matched = []
    for case_id, persona_id in expected.items():
        candidates = [r for r in runs if r.get('personaId') == persona_id]
        if len(candidates) != 1 or (not candidates[0].get('callId') and not allow_unfinished):
            raise SmokeError('Missing or ambiguous persona/call mapping')
        matched.append((case_id, candidates[0]))
    call_ids = [r['callId'] for _, r in matched if r.get('callId')]
    if len(set(call_ids)) != len(call_ids):
        raise SmokeError('Duplicate call in recovered runs')
    return matched


def collect(source, env_path, output, *, snapshot_running=False, budget=600):
    if not 30 <= budget <= 1800:
        raise SmokeError('Collection budget must be between 30 and 1800 seconds')
    deadline = time.monotonic() + budget
    source, output = Path(source), Path(output)
    started = json.loads((source / 'simulation-started.json').read_text())
    publication = json.loads((source / 'test-publication.json').read_text())
    if started['agent_id'] != publication['agent_id'] or started['expected_version'] != publication['version']:
        raise SmokeError('Simulation metadata does not match the selected publication')
    inputs = recovery_case_inputs(source, started, root=ROOT)
    private = load_credentials(env_path)
    import freya
    from tools import score
    output.mkdir(parents=True, exist_ok=False)
    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = bounded_urlopen(original_urlopen, deadline)

    def save(name, value):
        (output / name).write_text(json.dumps(redact(value, private), indent=2, ensure_ascii=False), encoding='utf-8')

    def event(kind, **fields):
        entry = redact({'event': kind, 'at': datetime.now(timezone.utc).isoformat(), **fields}, private)
        with (output / 'trace.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry) + '\n')
        print(json.dumps(entry), flush=True)

    try:
        sim_id = started['simulation_id']
        read = lambda operation: read_retry(operation, event, deadline=deadline)
        simulation = read(lambda: freya.api('GET', '/api/v2/simulations/' + sim_id))
        simulation = simulation.get('simulation') or simulation
        if simulation.get('status') not in ('completed', 'failed', 'paused') and not snapshot_running:
            raise SmokeError('Simulation is still running; collect this same ID when it finishes')
        iterations = read(lambda: freya.api('GET', '/api/v2/simulations/' + sim_id + '/iterations'))
        iterations = iterations.get('iterations') or iterations.get('items') or iterations
        if not isinstance(iterations, list) or len(iterations) != 1:
            raise SmokeError('Expected exactly one iteration for this smoke run')
        it_id = iterations[0]['id']
        first = read(lambda: freya.get_iteration(sim_id, it_id))
        # Legacy one-case artifacts have no persona map; validate that mapping
        # before deriving the expected persona, rather than trusting list order.
        # A paused or failed simulation can stop calls mid-retry; those runs have
        # no call. Record them as platform failures instead of losing the batch.
        stopped = simulation.get('status') in ('paused', 'failed') or snapshot_running
        initial_match = match_runs(started, first.get('runs', []), allow_unfinished=stopped)
        pending, latest_full = [first], [first]
        # Verdict stability is judged over runs that have a call; an unfinished
        # run's verdict can never settle. Matching still uses the full run set.
        finished_personas = {run['personaId'] for _, run in initial_match if run.get('callId')}

        def observed():
            raw = pending.pop() if pending else read(lambda: freya.get_iteration(sim_id, it_id))
            latest_full[0] = raw
            return {**raw, 'runs': [r for r in raw.get('runs', []) if r.get('personaId') in finished_personas]}
        _, observation = observe_iteration(observed, sorted(finished_personas), save, event, deadline=deadline)
        iteration = latest_full[0]
        matched = match_runs(started, iteration.get('runs', []), allow_unfinished=stopped)
        calls, customer_ids, never_started = {}, {}, set()
        unfinished = {case_id for case_id, run in matched if not run.get('callId')}
        for case_id in sorted(unfinished):
            event('call_unfinished', case=case_id)
        for case_id, run in matched:
            if case_id in unfinished:
                continue
            call = read(lambda: download_call(freya, run['callId'], budget=max(1, deadline-time.monotonic())))
            ids = customer_secrets(call)
            persisted = persist_call_snapshot(output, run['callId'], call, private, redact, case_id=case_id)
            if persisted['created']:
                event('call_snapshot_saved', call_id=run['callId'], case=case_id,
                      call_ended=persisted['call_ended'], artifact=Path(persisted['path']).name)
            if not ids and not any(m.get('role') == 'assistant' for m in call.get('messages') or []):
                # The platform never started this conversation (seen during the
                # 11 October outages), so no customer ID exists to read a ledger.
                # Record it as a platform failure and keep collecting the batch.
                never_started.add(case_id)
                calls[run['callId']] = call
                event('call_never_started', call_id=run['callId'], case=case_id)
                continue
            if len(ids) != 1:
                raise SmokeError('Could not recover one unambiguous customer ID from the full call')
            customer_ids[case_id] = next(iter(ids))
            calls[run['callId']] = call
        save('simulation.json', simulation)
        save('iteration.json', iteration)
        save('test-publication.json', publication)
        save('case-inputs.json', inputs)
        available = {c['id']: c for c in inputs['cases']}
        rows = []
        for case_id, run in matched:
            if case_id in unfinished:
                case = available[case_id]
                rows.append({'id': case_id, 'level': case.get('level'), 'family': case.get('family'), 'pass': False,
                             'failed': ['platform: the call did not finish before the simulation stopped'],
                             'ledger_pass': None, 'official_outcome': run.get('outcome'),
                             'official_reason': run.get('reason'), 'call_ended': False, 'call_id': None,
                             'evaluation_issues': [], 'official_observed_outcomes': [], 'tools': [],
                             'bank_case_id': case['bank_case_id'], 'never_started': True})
                continue
            if calls[run['callId']].get('endedAt'):
                persist_call_snapshot(output, run['callId'], calls[run['callId']], private, redact, case_id=case_id)
            else:
                save('call-' + case_id + '.json', calls[run['callId']])
            if case_id in never_started:
                case = available[case_id]
                rows.append({'id': case_id, 'level': case.get('level'), 'family': case.get('family'), 'pass': False,
                             'failed': ['platform: the call never started (no agent turn); ledger unavailable'],
                             'ledger_pass': None, 'official_outcome': run.get('outcome'),
                             'official_reason': run.get('reason'), 'call_ended': bool(calls[run['callId']].get('endedAt')),
                             'call_id': run.get('callId'), 'evaluation_issues': [], 'official_observed_outcomes': [],
                             'tools': [], 'bank_case_id': case['bank_case_id'], 'never_started': True})
                continue
            ledger = read(lambda: score.http('/v1/practice/ledger/' + customer_ids[case_id]))
            save('ledger-' + case_id + '.json', ledger)
            if ledger.get('case') != available[case_id]['bank_case_id']:
                raise SmokeError('Recovered bank ledger belongs to a different case')
            row = scored_row(available[case_id], ledger, run, calls[run['callId']], score.score_ledger, observation)
            row['bank_case_id'] = available[case_id]['bank_case_id']
            rows.append(row)
        finished_runs = [run for _, run in matched if run.get('callId')]
        evidence = test_version_evidence(publication, {**iteration, 'runs': finished_runs,
                                                       'runsTotal': len(finished_runs)}, calls, simulation)
        save('test-version-verification.json', evidence)
        report = {'status': simulation['status'], 'simulation_id': sim_id, 'iteration_id': it_id,
                  'agent_id': publication['agent_id'], 'expected_version': publication['version'],
                  'workflow_sha256': publication['workflow_sha256'], 'version_verified': evidence['verified'],
                  'style': started.get('style'), 'verdict_observation': observation,
                  'case_inputs_sha256': started.get('case_inputs_sha256'),
                  'passed': sum(r['pass'] for r in rows), 'total': len(rows), 'rows': rows, 'recovered_from': str(source)}
        report.update(snapshot_running=snapshot_running, all_calls_ended=all(r['call_ended'] for r in rows))
        save('result.json', report)
        definition_path = source / 'tested-definition.json'
        if not definition_path.exists():
            definition_path = source / 'saved-workflow.json'
        if definition_path.exists():
            from tools.speech_audit import audit_batch
            definition = json.loads(definition_path.read_text(encoding='utf-8'))['workflow']
            speech = audit_batch(definition, output)
        else:
            speech = {'status': 'failed', 'findings': [{'reason': 'Saved tested definition is missing; prepared speech cannot be verified'}]}
        save('speech-audit.json', speech)
        report['speech_verified'] = speech['status'] == 'passed'
        save('result.json', report)
        event('prepared_speech_checked', status=speech['status'], findings=len(speech['findings']))
        event('simulation_collected', status=report['status'], passed=report['passed'], total=len(rows),
              version_verified=evidence['verified'], failures={r['id']: r['failed'] for r in rows if r['failed']})
        return report
    except Exception as exc:
        event('collection_error', error_type=type(exc).__name__, message=str(exc))
        raise
    finally:
        urllib.request.urlopen = original_urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--env', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--snapshot-running', action='store_true', help='Read existing calls while aggregate status is running; never marks the simulation completed')
    parser.add_argument('--budget', type=int, default=600, help='Total read-only collection budget in seconds')
    args = parser.parse_args()
    try:
        report = collect(args.source, args.env, args.out, snapshot_running=args.snapshot_running, budget=args.budget)
    except Exception:
        return 2
    return 0 if (report['status'] == 'completed' and report['passed'] == report['total']
                 and report['version_verified'] and report['speech_verified']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
