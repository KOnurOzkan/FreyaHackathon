"""Run evaluator-only adversarial semantic replays on frozen generated artifacts.

Usage: python -B -m tests.audit_adversarial_workflows --a out/workflow-a-18
       --b out/workflow-b-18 --out out/adversarial-a18-b18.json
No model calls, credentials, publication, or network requests.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import unittest

from tests.test_live_adversarial_scenarios import AdversarialScenarioTests, CASES, ORACLES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--a', type=Path, required=True)
    parser.add_argument('--b', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError('Use a new audit report; do not overwrite prior evidence')
    compiled, artifacts = {}, {}
    for brief, folder in [('A',args.a),('B',args.b)]:
        source = (folder/'workflow.json').read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        if manifest['workflow_sha256'] != digest:
            raise ValueError('Generated workflow differs from its build manifest')
        compiled[brief] = (json.loads(source), {'runtime_payload':json.loads((folder/'runtime-payload.json').read_text(encoding='utf-8'))})
        artifacts[brief] = {'folder':str(folder),'workflow_sha256':digest,'core_version':manifest['core_version']}

    class GeneratedAdversarialAudit(AdversarialScenarioTests):
        pass
    GeneratedAdversarialAudit.compiled = compiled
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GeneratedAdversarialAudit))
    report = {'status':'passed' if result.wasSuccessful() else 'failed',
        'scope':'offline_emitted_code_with_expected_semantic_events',
        'live_language_understanding_tested':False,'official_freya_tests_run':False,
        'cases':len(json.loads(CASES.read_text(encoding='utf-8'))),'tests':result.testsRun,
        'seconds':round(time.monotonic()-started,3),'artifacts':artifacts,
        'sources_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (CASES,ORACLES)},
        'failed_tests':[{'test':str(test),'traceback':trace} for test,trace in result.failures+result.errors]}
    with args.out.open('x',encoding='utf-8') as f:
        json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k not in ('failed_tests','sources_sha256','artifacts')}))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
