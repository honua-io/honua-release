"""Inventory/source-mapping tests; synthetic cases never qualify pilot evidence."""
import copy
from pathlib import Path
import pytest
import agent_e2e_pilot as pilot
from agent_e2e_pack import load_json, inventory as rc_inventory

@pytest.fixture
def profile(): return load_json(pilot.DEFAULT_PROFILE)

def test_five_journeys_expand_exact_entrypath_and_variant_matrix(profile):
    rows=pilot.inventory(profile)
    journeys=[row for row in rows if row['kind']=='journey']
    assert sum(r['profile']=='local' for r in journeys)==10
    assert sum(r['profile']=='ecs' for r in journeys)==30
    assert len({r['caseId'] for r in rows})==len(rows)
    assert {r['entrypoint'] for r in rows}=={'console','claude-code'}
    assert not any(r['variant']!='A' and r['profile']=='local' for r in rows)

def test_every_probe_has_explicit_both_target_entrypath_subcases(profile):
    rows=pilot.inventory(profile)
    for scenario in profile['scenarios']:
        if scenario['kind']!='interaction-probe': continue
        for sub in scenario['subcases']:
            cases=[r for r in rows if r['scenarioId']==scenario['id'] and r['subcaseId']==sub['id']]
            assert len(cases)==2*len(sub['entrypoints'])
            assert {r['variant'] for r in cases}=={'A'}
            assert {r['target'] for r in cases}=={'local-docker','aws-ecs-redis-on'}

def test_legacy_pack_stays_36_scenarios_108_prompts_252_cases(profile):
    pack=load_json(pilot.ROOT/profile['sourcePack'])
    assert len(pack['scenarios'])==36
    assert sum(len(s['prompts']) for s in pack['scenarios'])==108
    assert len(rc_inventory(pack))==252

def test_projection_never_exports_heldout_prompts(profile):
    result=pilot.projection(profile,'console')
    assert result['qualification']=='inventory-only'
    for scenario in result['scenarios']:
        for sub in scenario['subcases']: assert set(sub['prompts'])=={'A','B'}
    assert 'Cedar' not in str(result)

def test_missing_probe_duplicate_subcase_and_tool_recipe_refused(profile):
    for mutate in (lambda p:p['scenarios'].pop(),
                   lambda p:p['scenarios'][0]['subcases'].append(copy.deepcopy(p['scenarios'][0]['subcases'][0])),
                   lambda p:p['scenarios'][0]['subcases'][0]['prompts'].update(A='Call honua_publish_service')):
        bad=copy.deepcopy(profile);mutate(bad)
        with pytest.raises(ValueError):pilot.validate(bad)

def test_candidate_source_pack_and_budget_cannot_silently_change(profile):
    for mutate in (lambda p:p.update(sourcePackDigest='0'*64),
                   lambda p:p['policy'].update(totalBudgetUsd=2500),
                   lambda p:p['policy'].update(maxConcurrentPaidAttempts=5),
                   lambda p:p['profiles']['local'].update(variants=['A','B'])):
        bad=copy.deepcopy(profile);mutate(bad)
        with pytest.raises(ValueError):pilot.validate(bad)

def test_declaring_runner_ready_is_not_qualification(profile):
    profile['scenarios'][0]['runnerStatus']='qualified'
    with pytest.raises(ValueError):pilot.inventory(profile)

def test_report_retains_blocked_cases_without_fabricating_receipts(profile):
    report=pilot.report(profile)
    assert report['status']=='blocked' and report['pilotQualified'] is False
    assert report['existingSourceEvidence']==[]
    assert all(r['runnerStatus']=='blocked' for r in report['cases'])
    assert report['journeyCounts']=={'local':10,'ecs':30}
    assert report['probeCounts']=={'local':47,'ecs':47}
    assert len(report['cases'])==134

def test_existing_campaign_mapping_cannot_qualify_new_entrypaths(profile):
    result=pilot.report(profile,verified_source={'campaignDigest':'sha256:'+'a'*64})
    assert result['existingSourceEvidence']
    assert result['pilotQualified'] is False and result['status']=='blocked'
    assert all('does not prove' in r['scope'] for r in result['existingSourceEvidence'])

def test_cli_full_report_exits_nonzero_and_preserves_profile(profile,tmp_path):
    output=tmp_path/'report.json'
    assert pilot.main(['report','--output',str(output)])==1
    assert load_json(output)['pilotQualified'] is False
    with pytest.raises(SystemExit):pilot.main(['export','--output',str(pilot.DEFAULT_PROFILE)])

def test_unknown_coverage_finding_and_source_mapping_refused(profile):
    for mutate in (lambda p:p['scenarios'][0].update(coverageAuditFindingIds=['invented']),
                   lambda p:p['scenarios'][0]['sourceMappings'][0].update(scenarioId='invented')):
        bad=copy.deepcopy(profile);mutate(bad)
        with pytest.raises(ValueError):pilot.validate(bad)

def test_partial_source_authentication_cannot_promote_report(tmp_path):
    with pytest.raises(SystemExit):
        pilot.main(['report','--source-bundle',str(tmp_path)])
