"""Read-only cross-run evidence matrix; never runs tests or declares readiness.

Each workflow SHA and frozen case fingerprint is evaluated separately. Three
passes means at least three independent trials and a conjunction of ALL observed
trial gates, not selection of three favorable verdicts around a failure.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.artifact_checks import canonical_digest, test_version_evidence, version_number
from tools.score import score_ledger


def read(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def matching_version(value, expected):
    try:
        return version_number(value) == version_number(expected)
    except (ValueError, TypeError):
        return False


def scoped(items, case):
    return [item for item in items if not isinstance(item, dict) or item.get('case') in (None, case)]


def manual_status(review, result, case, folder):
    if review is None:
        return 'missing'
    if (review.get('workflow_sha256') != result.get('workflow_sha256') or
            not matching_version(review.get('workflow_version'), result.get('expected_version')) or
            review.get('simulation_id', result.get('simulation_id')) != result.get('simulation_id')):
        return 'mismatched'
    # Reviewers may freeze source digests. Check relevant artifacts without
    # copying their contents (especially transcript strings) into the matrix.
    digests = review.get('sources_sha256', {})
    if not isinstance(digests, dict):
        return 'malformed'
    for name in ('result.json', 'speech-audit.json', 'tested-definition.json',
                 'case-inputs.json', 'call-' + case + '.json', 'ledger-' + case + '.json'):
        if name in digests:
            try:
                if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digests[name]:
                    return 'stale_sources'
            except OSError:
                return 'stale_sources'
    findings, issues = review.get('findings', []), review.get('evaluation_issues', [])
    if not isinstance(findings, list) or not isinstance(issues, list):
        return 'malformed'
    if scoped(findings, case) or scoped(issues, case):
        return 'findings'
    cases = review.get('cases')
    if isinstance(cases, dict) and isinstance(cases.get(case), dict):
        item = cases[case]
        if item.get('findings') or item.get('status') not in {'pass', 'passed', 'pass_no_observed_violation'}:
            return 'findings'
        return 'passed'
    evidence = review.get('case_evidence', [])
    matches = [item for item in evidence if isinstance(item, dict) and item.get('case') == case] if isinstance(evidence, list) else []
    if len(matches) != 1:
        return 'incomplete'
    if review.get('review_passed') is False or (review.get('submission_ready') is False and not findings and not issues):
        return 'blocked'
    return 'passed'


def collect_folder(folder, public):
    folder = Path(folder).resolve()
    result = read(folder / 'result.json')
    if result is None or not re.fullmatch(r'[0-9a-f]{64}', str(result.get('workflow_sha256', ''))):
        return [], {'source': str(folder), 'reason': 'missing_or_invalid_result_fingerprint'}
    rows = result.get('rows')
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        return [], {'source': str(folder), 'reason': 'missing_or_malformed_rows'}
    publication = read(folder / 'test-publication.json') or {}
    iteration = read(folder / 'iteration.json') or {}
    simulation = read(folder / 'simulation.json') or {}
    inputs = read(folder / 'case-inputs.json') or {}
    audit = read(folder / 'speech-audit.json') or {}
    review = read(folder / 'manual-review.json')
    common = []
    if result.get('status') != 'completed' or result.get('total') != len(rows):
        common.append('incomplete_batch')
    if len({row.get('id') for row in rows}) != len(rows):
        common.append('duplicate_case_in_batch')
    if any(not isinstance(row.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]+', row['id']) for row in rows):
        common.append('invalid_case_identifier')
    if not result.get('simulation_id') or not result.get('iteration_id'):
        common.append('missing_simulation_or_iteration')
    if (publication.get('status') != 'readback_verified' or publication.get('differences') or
            publication.get('workflow_sha256') != result['workflow_sha256'] or
            publication.get('agent_id') != result.get('agent_id') or
            not matching_version(publication.get('version'), result.get('expected_version'))):
        common.append('publication_mismatch')
    case_list = inputs.get('cases', [])
    case_lookup = {c['id']: c for c in case_list if isinstance(c, dict) and isinstance(c.get('id'), str)} if isinstance(case_list, list) else {}
    if not case_lookup or len(case_lookup) != len(case_list):
        common.append('missing_or_ambiguous_frozen_inputs')
        # Label legacy public artifacts usefully, while keeping missing frozen
        # inputs blocking. This fallback is not historical input verification.
        case_lookup = {row['id']: {**public[row['id']], 'bank_case_id': row['id']}
                       for row in rows if row.get('id') in public}
    expected_inputs = result.get('case_inputs_sha256')
    if expected_inputs and canonical_digest(inputs) != expected_inputs:
        common.append('frozen_inputs_changed')
    observation = result.get('verdict_observation') or {}
    if observation.get('stable') is not True or type(observation.get('snapshots')) is not int or observation['snapshots'] < 2:
        common.append('unstable_or_unobserved_verdict')
    calls = {}
    for row in rows:
        case = row.get('id')
        if isinstance(case, str) and re.fullmatch(r'[A-Za-z0-9_-]+', case):
            call = read(folder / ('call-' + case + '.json'))
            if call and row.get('call_id'):
                calls[row['call_id']] = call
    try:
        versions = test_version_evidence(publication, iteration, calls, simulation)
    except (KeyError, ValueError, TypeError, AttributeError):
        versions = {'verified': False}
    if result.get('version_verified') is not True or versions.get('verified') is not True:
        common.append('actual_version_unverified_or_mismatched')
    trials = []
    for row in rows:
        case, call_id = row.get('id'), row.get('call_id')
        if not isinstance(case, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', case):
            continue
        blockers = list(common)
        frozen = case_lookup.get(case)
        category, bank_case = 'unknown', row.get('bank_case_id')
        case_digest = None
        if frozen:
            bank_case = frozen.get('bank_case_id', case)
            case_digest = canonical_digest(frozen)
            if case in public:
                normalized = {k: v for k, v in frozen.items() if k != 'bank_case_id'}
                category = 'public' if bank_case == case and normalized == public[case] else 'unknown'
            elif bank_case in public:
                category = 'targeted'
        if category == 'unknown':
            blockers.append('case_fixture_unidentified')
        if row.get('bank_case_id', bank_case) != bank_case:
            blockers.append('bank_case_mapping_mismatch')
        ledger = read(folder / ('ledger-' + case + '.json'))
        ledger_verified = False
        try:
            ledger_verified = bool(frozen and ledger and ledger.get('case') == bank_case and
                                   score_ledger(frozen, ledger)['pass'])
        except (KeyError, ValueError, TypeError):
            pass
        if row.get('ledger_pass') is not True or not ledger_verified:
            blockers.append('ledger_failed_or_unverified')
        call = calls.get(call_id, {})
        if (not call_id or call.get('id') != call_id or not call.get('endedAt') or
                row.get('call_ended') is not True):
            blockers.append('missing_mismatched_or_unfinished_call')
        official = row.get('official_outcome')
        matching_runs = [item for item in iteration.get('runs', []) if isinstance(item, dict) and item.get('callId') == call_id]
        if official != 'pass' or len(matching_runs) != 1 or matching_runs[0].get('outcome') != official:
            blockers.append('official_nonpass_or_mismatched')
        persona = matching_runs[0].get('personaId') if len(matching_runs) == 1 else None
        observed = observation.get('personas', {}).get(persona, {})
        if (observed.get('outcomes') != ['pass'] or observed.get('call_ids') != [call_id] or
                observed.get('conflict') is not False):
            blockers.append('conflicting_or_missing_persona_observation')
        if row.get('official_observed_outcomes') != ['pass']:
            blockers.append('missing_or_conflicting_observed_outcome')
        if row.get('evaluation_issues') or row.get('pass') is not True:
            blockers.append('row_blocked')
        case_audit = audit.get('cases', {}).get(case, {})
        speech = ('passed' if audit.get('workflow_sha256') == result['workflow_sha256'] and
                  matching_version(audit.get('workflow_version'), result.get('expected_version')) and
                  case_audit.get('status') == 'passed' and not case_audit.get('findings') and
                  type(case_audit.get('utterances_checked')) is int and case_audit['utterances_checked'] > 0 else 'missing_failed_or_mismatched')
        if speech != 'passed':
            blockers.append('speech_unverified')
        manual = manual_status(review, result, case, folder)
        if manual != 'passed':
            blockers.append('manual_' + manual)
        style = result.get('style') or (frozen or {}).get('style') or 'default'
        if not isinstance(style, str):
            style = 'invalid'
            blockers.append('invalid_speaking_style')
        trials.append({'source': str(folder), 'workflow_sha256': result['workflow_sha256'],
            'workflow_version': result.get('expected_version'), 'agent_id': result.get('agent_id'),
            'simulation_id': result.get('simulation_id'), 'iteration_id': result.get('iteration_id'),
            'case': case, 'case_sha256': case_digest, 'category': category, 'bank_case_id': bank_case,
            'brief': frozen.get('brief') if frozen else None,
            'style': style,
            'call_id': call_id, 'call_ended': bool(call.get('endedAt')), 'official_outcome': official,
            'ledger_reported_pass': row.get('ledger_pass'), 'ledger_recomputed_pass': ledger_verified,
            'ledger_status': 'passed' if ledger_verified and row.get('ledger_pass') is True else 'failed_or_unverified',
            'speech_status': speech, 'manual_status': manual, 'version_verified': versions.get('verified') is True,
            'blockers': sorted(set(blockers))})
    return trials, None


def conjunction(trials):
    passed = sum(trial['qualifying_pass'] for trial in trials)
    complete = len(trials) >= 3 and passed == len(trials)
    return {'trials': len(trials), 'qualifying_passes': passed,
            'unique_calls': len({trial['call_id'] for trial in trials if trial['call_id']}),
            'unique_simulations': len({trial['simulation_id'] for trial in trials if trial['simulation_id']}),
            'three_pass_conjunction': complete,
            'status': 'three_independent_passes' if complete else 'blocked' if passed < len(trials) else 'insufficient_repetitions',
            'blocker_counts': dict(sorted(Counter(reason for trial in trials for reason in trial['blockers']).items()))}


def build_matrix(folders, *, public_path=ROOT / 'public-test-set.json'):
    public = {case['id']: case for case in json.loads(Path(public_path).read_text(encoding='utf-8'))}
    trials, rejected = [], []
    for folder in sorted({Path(folder).resolve() for folder in folders}):
        try:
            rows, error = collect_folder(folder, public)
            trials.extend(rows)
            if error:
                rejected.append(error)
        except (ValueError, TypeError, KeyError, AttributeError):
            rejected.append({'source': str(folder), 'reason': 'malformed_evidence'})
    calls = Counter(trial['call_id'] for trial in trials if trial['call_id'])
    simulations = Counter((trial['workflow_sha256'], trial['case'], trial['case_sha256'], trial['simulation_id']) for trial in trials)
    for trial in trials:
        trial['fresh_call'] = bool(trial['call_id'] and calls[trial['call_id']] == 1)
        if not trial['fresh_call']:
            trial['blockers'].append('reused_or_missing_call_id')
        if simulations[(trial['workflow_sha256'], trial['case'], trial['case_sha256'], trial['simulation_id'])] > 1:
            trial['blockers'].append('reused_simulation_for_case')
        trial['blockers'] = sorted(set(trial['blockers']))
        trial['qualifying_pass'] = not trial['blockers']
    groups = defaultdict(list)
    for trial in trials:
        groups[trial['workflow_sha256']].append(trial)
    workflows = []
    for digest, selected in sorted(groups.items()):
        cases = defaultdict(list)
        for trial in selected:
            cases[(trial['category'], trial['case'], trial['case_sha256'])].append(trial)
        rows = []
        for (category, case, case_digest), case_trials in sorted(cases.items(), key=lambda item: str(item[0])):
            styles = {style: conjunction([trial for trial in case_trials if trial['style'] == style])
                      for style in sorted({trial['style'] for trial in case_trials})}
            rows.append({'category': category, 'case': case, 'case_sha256': case_digest,
                         'bank_case_id': case_trials[0]['bank_case_id'], 'brief': case_trials[0]['brief'],
                         **conjunction(case_trials), 'styles': styles, 'evidence': case_trials})
        briefs = {trial['brief'] for trial in selected if trial['brief']}
        expected_public = sorted(case for case, value in public.items() if value['brief'] in briefs)
        seen_public = {trial['case'] for trial in selected if trial['category'] == 'public'}
        workflows.append({'workflow_sha256': digest,
            'versions': sorted({trial['workflow_version'] for trial in selected}, key=str),
            'public_cases_observed': len(seen_public), 'public_cases_missing': sorted(set(expected_public) - seen_public),
            'targeted_variants_observed': sum(row['category'] == 'targeted' for row in rows),
            'cases_with_three_pass_conjunction': sum(row['three_pass_conjunction'] for row in rows),
            'cases': rows})
    return {'format': 'freya-evaluation-matrix-v1', 'generated_at': datetime.now(timezone.utc).isoformat(),
        'evaluation_only': True, 'provider_input': False, 'required_repetitions': 3,
        'conjunction_rule': 'At least three distinct calls/simulations; every observed trial in the same workflow SHA and frozen case must pass all evidence gates. No failed trial is discarded.',
        'scope': 'Collected local evidence only. Public and targeted cases, changed fixture fingerprints and speaking styles stay visible. This report does not authorize candidate readiness or submission.',
        'rejected_sources': rejected, 'workflows': workflows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folders', nargs='*', type=Path)
    parser.add_argument('--root', type=Path, help='Discover result.json files recursively beneath this output directory')
    parser.add_argument('--out', required=True, type=Path, help='New JSON report; existing files are never overwritten')
    args = parser.parse_args()
    folders = args.folders + ([path.parent for path in args.root.rglob('result.json')] if args.root else [])
    if not folders:
        parser.error('Provide collected folders or --root')
    report = build_matrix(folders)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
    print(json.dumps({'report': str(args.out), 'workflow_groups': len(report['workflows']),
                      'rejected_sources': len(report['rejected_sources'])}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
