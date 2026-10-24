#!/usr/bin/env python3
"""Expand the controller-owned pilot profile without changing the frozen RC pack.

Pilot coverage gaps stay blocked. Existing authenticated campaign evidence supports
mapped assertions only; it cannot prove new Console/Claude Code interactions.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import re

from agent_e2e_pack import canonical_digest, load_json, inventory as rc_inventory
import coverage_audit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / 'certification/agent-e2e/foss4g-pilot.v1.json'
ENTRYPOINTS = ('console', 'claude-code')


def validate(profile, *, root=ROOT):
    if profile.get('schemaVersion') != 'honua.foss4g-ai-pilot/v1':
        raise ValueError('unknown pilot profile')
    policy = profile['policy']
    if (policy['totalBudgetUsd'], policy['cleanupReserveUsd'], policy['maxConcurrentPaidAttempts']) != (2000, 200, 4):
        raise ValueError('pilot budget requires separate owner decision')
    if policy['developmentVariants'] != ['A', 'B'] or policy['heldOutVariants'] != ['C'] or policy['rcGateChanged'] is not False:
        raise ValueError('development/heldout or RC boundary changed')
    pack_path = root / profile['sourcePack']
    import hashlib
    if hashlib.sha256(pack_path.read_bytes()).hexdigest() != profile['sourcePackDigest']:
        raise ValueError('source pack bytes changed')
    pack = load_json(pack_path)
    source_rows = rc_inventory(pack)
    source_ids = {r['scenarioId'] for r in source_rows}
    audit=load_json(root / profile['coverageAudit'])
    coverage_audit.validate_findings(audit)
    audit_ids={finding['id'] for finding in audit['findings']}
    if profile['profiles'] != {'local': {'target': 'local-docker', 'variants': ['A']},
                               'ecs': {'target': 'aws-ecs-redis-on', 'variants': ['A', 'B', 'C']}}:
        raise ValueError('pilot target/variant matrix changed')
    ids, subcases = set(), set()
    for scenario in profile['scenarios']:
        sid = scenario['id']
        if sid in ids: raise ValueError('duplicate scenario')
        ids.add(sid)
        if scenario['entrypoints'] != list(ENTRYPOINTS) or scenario['targets'] != ['local-docker', 'aws-ecs-redis-on']:
            raise ValueError('both pilot entrypaths and targets required')
        if scenario['runnerStatus'] != 'blocked' or not scenario['blockers']:
            raise ValueError('no qualified pilot runner exists; source declarations cannot establish readiness')
        for field in ('assertions', 'forbidden', 'prerequisites', 'subcases'):
            if not scenario[field]: raise ValueError(f'{sid}: missing {field}')
        for mapping in scenario['sourceMappings']:
            if mapping['pack'] != profile['sourcePack'] or mapping['scenarioId'] not in source_ids or not mapping['scope']:
                raise ValueError('invalid existing source mapping')
        if not set(scenario['coverageAuditFindingIds']) <= audit_ids: raise ValueError('unknown coverage audit finding')
        for case in scenario['subcases']:
            if not case['entrypoints'] or not set(case['entrypoints']) <= set(ENTRYPOINTS) or len(set(case['entrypoints']))!=len(case['entrypoints']): raise ValueError('invalid subcase entrypaths')
            expected_variants={'local':['A'],'ecs':['A','B','C'] if scenario['kind']=='journey' else ['A']}
            if case['variantsByProfile'] != expected_variants: raise ValueError('unexpected subcase variant expansion')
            key = sid + ':' + case['id']
            if key in subcases or not re.fullmatch(r'[a-z][a-z0-9-]+', case['id']): raise ValueError('duplicate/invalid subcase')
            subcases.add(key)
            if set(case['prompts']) != set('ABC') or len(set(case['prompts'].values())) != 3:
                raise ValueError('three distinct controller prompts required')
            for prompt in case['prompts'].values():
                if not isinstance(prompt, str) or not prompt.strip() or re.search(r'\b(?:AI|IX)-\d\d\b|honua_\w+|\b(?:GET|POST|PUT) /', prompt):
                    raise ValueError('prompt contains scenario ID or tool recipe')
    if ids != {f'AI-{n:02}' for n in range(1, 6)} | {f'IX-{n:02}' for n in range(1, 17)}:
        raise ValueError('five journeys and sixteen probes required')
    return profile


def inventory(profile, *, root=ROOT):
    validate(profile, root=root)
    rows=[]
    for scenario in profile['scenarios']:
        for subcase in scenario['subcases']:
            for profile_id, target in profile['profiles'].items():
                for variant in subcase['variantsByProfile'][profile_id]:
                    for entry in subcase['entrypoints']:
                        rows.append({'caseId': ':'.join([scenario['id'], subcase['id'], variant, target['target'], entry]),
                                     'scenarioId': scenario['id'], 'subcaseId': subcase['id'], 'kind': scenario['kind'],
                                     'profile': profile_id, 'variant': variant, 'target': target['target'], 'entrypoint': entry,
                                     'promptDigest': canonical_digest(subcase['prompts'][variant]),
                                     'runnerStatus': scenario['runnerStatus'], 'blockers': scenario['blockers']})
    return rows


def projection(profile, entrypoint, *, root=ROOT):
    validate(profile, root=root)
    if entrypoint not in ENTRYPOINTS: raise ValueError('unknown entrypoint')
    scenarios=[]
    for scenario in profile['scenarios']:
        row={key: scenario[key] for key in ('id','title','prerequisites','assertions','forbidden','sourceMappings','runnerStatus','runnerMappings','coverageAuditFindingIds')}
        row['subcases']=[{'id': sub['id'], 'entrypoints':sub['entrypoints'],'variantsByProfile':sub['variantsByProfile'],'setup':sub['setup'], 'prompts': {v: sub['prompts'][v] for v in ('A','B')},
                          **({'developmentProbe': sub['developmentProbe']} if 'developmentProbe' in sub else {})} for sub in scenario['subcases'] if entrypoint in sub['entrypoints']]
        row['blockedAssertions']=scenario.get('blockedAssertions',[])
        scenarios.append(row)
    return {'schemaVersion':'honua.foss4g-pilot-projection/v1','profileDigest':canonical_digest(profile),
            'entrypoint':entrypoint,'qualification':'inventory-only','heldOutPrompts':'controller-only; never exported',
            'scenarios':scenarios}


def report(profile, *, root=ROOT, verified_source=None):
    rows=inventory(profile, root=root)
    # Only the existing authenticated verifier may supply supporting evidence.
    # This command has no input flag accepting a self-attested passing result.
    supported=[]
    if verified_source:
        sources={s['id']:s for s in load_json(root/profile['sourcePack'])['scenarios']}
        for scenario in profile['scenarios']:
            supported.extend({'scenarioId':scenario['id'],'sourceScenarioId':m['scenarioId'],
                              'scope':m['scope'],'campaignDigest':verified_source['campaignDigest'],
                              'sourceAssertions':sources[m['scenarioId']]['assertions'],
                              'sourceTargets':sources[m['scenarioId']]['targets']}
                             for m in scenario['sourceMappings'])
    return {'schemaVersion':'honua.foss4g-pilot-report/v1','profileDigest':canonical_digest(profile),
            'status':'blocked','pilotQualified':False,'rcGateChanged':False,
            'reason':'Pilot-specific independent live oracles and authenticated entrypath receipts are not yet qualified.',
            'counts':dict(Counter(row['profile'] for row in rows)),
            'journeyCounts':dict(Counter(row['profile'] for row in rows if row['kind']=='journey')),
            'probeCounts':dict(Counter(row['profile'] for row in rows if row['kind']=='interaction-probe')),
            'byEntrypoint':dict(Counter(row['entrypoint'] for row in rows)),
            'existingSourceEvidence':supported,'cases':rows}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('validate','inventory','export','report'))
    parser.add_argument('--profile',type=Path,default=DEFAULT_PROFILE)
    parser.add_argument('--entrypoint',choices=ENTRYPOINTS,default='console')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--source-bundle',type=Path)
    parser.add_argument('--current-lock',type=Path)
    parser.add_argument('--campaign-digest')
    parser.add_argument('--source-sha')
    parser.add_argument('--run-id',type=int)
    parser.add_argument('--run-attempt',type=int)
    args=parser.parse_args(argv)
    try:
        profile=load_json(args.profile); validate(profile)
        verified=None
        authentication=(args.source_bundle,args.current_lock,args.campaign_digest,args.source_sha,args.run_id,args.run_attempt)
        if any(value is not None for value in authentication):
            if args.command!='report' or not all(value is not None for value in authentication): raise ValueError('all source authentication inputs required for report')
            import verify_agent_e2e_campaign
            verified=verify_agent_e2e_campaign.verify(args.source_bundle,ROOT/profile['sourcePack'],args.current_lock,
                args.campaign_digest,args.source_sha,args.run_id,args.run_attempt)
        result=({'status':'valid','profileDigest':canonical_digest(profile)} if args.command=='validate' else
                inventory(profile) if args.command=='inventory' else projection(profile,args.entrypoint) if args.command=='export' else
                report(profile,verified_source=verified))
        text=json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False)+'\n'
        if args.output:
            if args.output.resolve() in {args.profile.resolve(),(ROOT/profile['sourcePack']).resolve()}: raise ValueError('output cannot overwrite canonical profile/pack')
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(text)
        else: print(text,end='')
        return 1 if args.command=='report' else 0
    except (ValueError,KeyError,TypeError,OSError) as exc:
        parser.exit(2,f'invalid pilot profile/evidence: {exc}\n')

if __name__=='__main__': raise SystemExit(main())
