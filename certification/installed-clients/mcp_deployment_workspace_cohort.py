"""Complete original two-client deployment/workspace proof; no weak fallback."""
from __future__ import annotations
import argparse
import copy
from pathlib import Path
import sys
import json
sys.path.insert(0,str(Path(__file__).resolve().parent))
import mcp_map_package_validation as wire
import mcp_deployment_workspace_validation as semantic
import mcp_deployment_workspace_authority as authority
from mcp_deployment_identity_validation import verified_cohort,verify_full_identity
from mcp_managed_identity_fixture import verify_receipt
from mcp_ops_events_data_read import verify as verify_data_read
from mcp_deployment_workspace_store_oracle import instant

SCHEMA='honua.mcp-deployment-workspace-client-observations/v2'
CANONICAL={'sdk':'Official MCP TypeScript SDK','inspector':'MCP Inspector'}
FIELDS={'schema','nonce','canonicalClient','clientVersion','targetId','identityPreparation','authority','semantic','calls'}


def original_preparation(terminal):
    """Reconstruct the complete exact public pre-journey state from originals."""
    original=copy.deepcopy(terminal);original.pop('cleanupStartedAt');original['cleanupComplete']=False
    role=original['roleReceipt'];role.pop('cleanupStartedAt');role['cleanupComplete']=False
    # Normal create never declares cleanup observations. Preserve absent keys;
    # adding nulls would reject the actual original provisioning receipt.
    role.pop('beforeCleanupStore');role.pop('afterCleanupStore');role['events']=role['events'][:5];role['cohort']['journeys']=[]
    for pair in original['keys'].values():
        for receipt in pair.values():
            for key in ('revocation','revokedKeyIds','revokedEffectivePermissions'):receipt.pop(key)
            receipt['cleanupComplete']=False;receipt['events']=receipt['events'][:2]
    return original


def checked_observation(value):
    if (not isinstance(value,dict) or set(value)!=FIELDS or value['schema']!=SCHEMA
            or value['canonicalClient'] not in CANONICAL.values() or not isinstance(value['semantic'],dict)
            or any(value['semantic'].get(key)!=value[key] for key in ('nonce','canonicalClient','clientVersion','targetId'))):
        raise ValueError('complete original client observations missing')
    return value


def authority_progress(fixture,bootstrap,value,stage):
    checked_observation(value)
    return authority.advance(fixture,bootstrap,value['authority'],nonce=value['nonce'],canonical=value['canonicalClient'],
        version=value['clientVersion'],target_id=value['targetId'],preparation=value['identityPreparation'],stage=stage,
        semantic_observations=value['semantic'] if stage=='after' else None)


def judge(fixture,bootstrap,selected,terminal):
    checked_observation(selected)
    service=bootstrap['ownedArea']['serviceId'];nonce=selected['nonce']
    members=verified_cohort(terminal,runtime=selected['semantic']['runtimeAfter'],nonce=nonce,service_id=service)
    sdk=members[CANONICAL['sdk']]['original']
    inspector=members[CANONICAL['inspector']]['original']
    if instant(sdk['completedAt'])>instant(inspector['startedAt']):
        raise ValueError('original SDK and Inspector lifetimes overlap')
    if not wire.same_json(inspector['observations']['semantic']['runtimeBefore'],sdk['observations']['semantic']['runtimeAfter']):
        raise ValueError('original clients lost the same-container restart lineage')
    expected_preparation=original_preparation(terminal)
    sessions=set();proofs={};selected_found=False
    for canonical,binding in members.items():
        envelope=binding['original'];value=checked_observation(envelope['observations'])
        if value['canonicalClient']!=canonical or value['nonce']!=nonce or not wire.same_json(value['identityPreparation'],expected_preparation):
            raise ValueError('original raw journey lost its exact managed provisioning state')
        if canonical==selected['canonicalClient']:
            if not wire.same_json(value,selected):raise ValueError('selected proof is not its original shared-cohort journey')
            selected_found=True
        primary=value['semantic'];tenant='mcp-deployment-'+nonce
        semantic_proof=semantic.judge(fixture,bootstrap,primary,tenant_id=tenant)
        if semantic_proof.get('semanticOnly') is not True:
            raise ValueError('semantic component cannot substitute for the complete proof')
        checked=authority_progress(fixture,bootstrap,value,'after')
        if checked.get('action')!='complete':raise ValueError('complete original own-positive/authority/readback journey missing')
        facts=checked['facts'];auth=value['authority']
        joined=[*auth['callsBefore'],*primary['calls'],*auth['callsAfter']]
        if not wire.same_json(value['calls'],joined) or not joined:
            raise ValueError('original shared journey lost actual call ordering or omitted calls')
        previous=instant(envelope['startedAt'])
        for call in joined:
            started,completed=instant(call['startedAt']),instant(call['completedAt'])
            if not previous<=started<=completed<=instant(envelope['completedAt']):
                raise ValueError('original shared journey call lifetime/order differs')
            previous=completed
            session=call['wire']['initialize']['response']['sessionSha256']
            if session in sessions:raise ValueError('actual transport session reused across original callers/clients')
            sessions.add(session)
        # Decode only after both independent semantic components have rejudged the
        # original published response and all canonical DB/resource relationships.
        publication=[call for call in primary['calls'] if call['id']=='publish-studio-version']
        if len(publication)!=1:raise ValueError('one real canonical primary publication required')
        primary_id=wire.json_tool_output(publication[0]['result'])['deploymentId']
        identity=binding['identity']
        verify_full_identity(identity,runtime=primary['runtimeAfter'],nonce=nonce,service_id=service,calls=joined,
            primary_stores=primary['storeBefore']['stores'],foreign_stores=facts['foreignStores'],
            personal_stores=facts['personalStores'],primary_prerequisite=primary['workspacePrerequisite'],
            foreign_prerequisite=auth['foreignWorkspacePrerequisite'],personal_prerequisite=auth['personalWorkspacePrerequisite'],
            primary_deployment_id=primary_id,foreign_deployment_id=facts['foreignDeploymentId'])
        owner=verify_receipt(identity['primaryReceipt'],primary['runtimeAfter'],'admin')['canonicalActorId']
        for slot in ('proposalBefore','proposalAfter'):
            proposal=primary[slot];record,audit=proposal['record'],proposal['audit']
            if (record.get('requestedBy')!=owner or audit.get('requestedBy')!=owner
                    or record.get('requestedByAgent')!='agent:'+owner
                    or record.get('tenantId')!=tenant or audit.get('tenantId')!=tenant):
                raise ValueError('actual rollback proposal lost its normal managed caller/tenant identity')
        foreign=verify_receipt(identity['foreignReceipt'],primary['runtimeAfter'],'workspace-reader',service_id=service)
        verify_data_read(auth['limitedRead'],fixture,bootstrap,foreign,auth['runtimeBefore'])
        data=auth['limitedRead'];denials=[call for call in auth['callsBefore'] if call['id'] in ('foreign-deploy-role-denial','foreign-rollback-role-denial')]
        if (len(denials)!=2 or not instant(foreign['observedAt'])<=instant(data['startedAt'])<=instant(data['completedAt'])
                <=min(instant(call['startedAt']) for call in denials)
                or instant(data['completedAt'])>instant(terminal['cleanupStartedAt'])):
            raise ValueError('actual read:* positive must precede role denials within the original key lifecycle')
        # Every independent GP setup is inside this original envelope, not a
        # projection borrowed from another key or a previous candidate attempt.
        for gp in (primary['workspacePrerequisite'],auth['foreignWorkspacePrerequisite'],auth['personalWorkspacePrerequisite']):
            records=[gp['submitted'],*gp['polls'],gp['output']]
            if any(not instant(envelope['startedAt'])<=instant(item['startedAt'])<=instant(item['completedAt'])<=instant(envelope['completedAt']) for item in records):
                raise ValueError('actual owned GP prerequisite escaped its original journey lifetime')
        proofs[canonical]={**semantic_proof,'semanticOnly':False,'normalManagedIdentityJoined':True,
            'originalTwoClientCohortJoined':True,'ownResourceEligibility':True,'tenantOwnerIsolation':True,
            'sameTenantPersonalOwnerRefusal':True,'scopedPlatformRoleRefusal':True,'limitedDataReadPositive':True,
            'configurationSha256':binding['original']['configurationSha256']}
    if not selected_found or set(proofs)!=set(CANONICAL.values()):raise ValueError('both original canonical clients are mandatory')
    return proofs[selected['canonicalClient']]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',type=Path,required=True);parser.add_argument('--bootstrap',type=Path,required=True)
    parser.add_argument('--observations',type=Path,required=True);parser.add_argument('--cohort',type=Path)
    parser.add_argument('--phase',choices=('before','after','judge'),default='judge');args=parser.parse_args()
    try:
        fixture=args.fixture.read_bytes();bootstrap=wire.strict_json(args.bootstrap.read_bytes());value=wire.strict_json(args.observations.read_bytes())
        if args.phase=='judge':
            if args.cohort is None:raise ValueError('terminal original two-client cohort required')
            result=judge(fixture,bootstrap,value,wire.strict_json(args.cohort.read_bytes()))
        else:result=authority_progress(fixture,bootstrap,value,args.phase)
        print(json.dumps(result,allow_nan=False));return 0
    except (ValueError,TypeError,KeyError,AttributeError,OSError):
        print('complete original two-client deployment/workspace oracle refused evidence',file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
