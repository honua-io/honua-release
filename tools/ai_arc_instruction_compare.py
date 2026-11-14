"""Compare two evidence-verified development runs with different instruction content."""
import argparse
import json
from pathlib import Path

import ai_arc_runner as R

PAIR_FIELDS = ('target', 'arm', 'modelId', 'variant', 'candidate', 'arcSha256',
               'harnessDigests', 'preregDigest', 'exclusions', 'opsScope',
               'cliVersion', 'family', 'temperature', 'temperaturePolicy')


def compare(receipts, verify):
    if len(receipts) != 2:
        raise ValueError('one guide-only and one guide-plus-skills run are required')
    baseline, skills = receipts
    if [r.get('instructionVariant', 'guide-only') for r in receipts] != ['guide-only', 'guide-plus-skills']:
        raise ValueError('instruction variants must be guide-only then guide-plus-skills')
    if baseline.get('runId') == skills.get('runId') or any(baseline.get(k) != skills.get(k) for k in PAIR_FIELDS):
        raise ValueError('runs do not share one candidate, goal, model, harness and target')
    if baseline.get('target') != 'local-docker' or baseline.get('variant') not in ('A', 'B'):
        raise ValueError('comparison requires local-docker development runs')
    for receipt in receipts:
        stages = receipt.get('stages') or []
        if len(stages) != len(R.STAGES) or {s.get('stage') for s in stages} != set(R.STAGES) or any(
                s.get('outcome') not in ('pass', 'fail') for s in stages):
            raise ValueError('all four stages must have observed pass/fail outcomes')
    results = [verify(receipt) for receipt in receipts]
    return {'schema': 'honua.ai-arc.instruction-comparison.v1', 'certifying': False,
            'nativeSkillLoadingProven': False, 'instructionLoading': 'project-instructions',
            'runIds': [r['runId'] for r in receipts],
            'binding': {key: baseline.get(key) for key in PAIR_FIELDS},
            'stages': [{'stage': stage, 'guideOnly': results[0]['stagePass'][stage],
                        'guidePlusSkills': results[1]['stagePass'][stage],
                        'delta': int(results[1]['stagePass'][stage]) - int(results[0]['stagePass'][stage])}
                       for stage in R.STAGES],
            'verification': results}


def verifier(context, evidence):
    arc, candidate, bound, package = context
    def verify(receipt):
        row = {'runId': receipt.get('runId'), 'variant': receipt.get('variant'), 'caseId': None, 'set': None}
        result = R.verify_run(arc, receipt, target='local-docker', arm_id=receipt.get('arm'),
                            candidate=bound, digests=R.harness_digests(), row=row, goal_sets={},
                            evidence=evidence[receipt['runId']], package=package)
        if not R.on_shipped_package(receipt, package):
            raise ValueError('both runs must use the pinned quickstart package and ops composition')
        return result
    return verify


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--guide-only', type=Path, required=True)
    parser.add_argument('--guide-plus-skills', type=Path, required=True)
    parser.add_argument('--guide-only-evidence', type=Path, required=True)
    parser.add_argument('--guide-plus-skills-evidence', type=Path, required=True)
    parser.add_argument('--quickstart-package-source', type=Path, required=True)
    parser.add_argument('--lock-path', required=True)
    parser.add_argument('--lock-digest', required=True)
    args = parser.parse_args(argv)
    arc = R.load_arc()
    candidate = R.candidate_from_lock(args.lock_path, args.lock_digest)
    bound = {'imageDigest': candidate['imageDigest'], 'sourceSha': candidate['sourceRevision'],
             'platformLockDigest': candidate['platformLockDigest'],
             **({'development': True} if candidate.get('development') else {})}
    package = R.expected_package(arc, candidate, args.quickstart_package_source)
    raws = [p.read_bytes() for p in (args.guide_only, args.guide_plus_skills)]
    receipts = [json.loads(raw) for raw in raws]
    evidence = dict(zip([r['runId'] for r in receipts],
                        (args.guide_only_evidence, args.guide_plus_skills_evidence)))
    report = compare(receipts, verifier((arc, candidate, bound, package), evidence))
    report['receiptSha256'] = [R.sha256(raw) for raw in raws]
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
