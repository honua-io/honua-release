#!/usr/bin/env python3
"""Source-bound private prefreeze setup; never creates campaign expectations.

Observed IDs/hashes stay in durable candidate state and private native custody.
Review their exact references into source before the ordinary campaign freeze.
This entry cannot initialize accounting, mint API keys, approve, or run a model.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import canonical_digest, load_json
from agent_e2e_state import DurableExecution, open_store
from agent_e2e_studio_backend import BootstrapHttp, prepare_initial_fixture
from agent_e2e_scenario_operations import body
from controller_archive import canonical, digest, portable, require
from controller_handoff import ROOT, PREPARE_WORKFLOW, PREPARATIONS, bundle_identity, git


def source_configuration(reference):
    require(set(reference) == {'path', 'sha256'}, 'private preparation source reference missing')
    portable(reference['path'])
    require(reference['path'].startswith('certification/agent-e2e/') and reference['path'].endswith('.json'), 'preparation configuration escaped reviewed namespace')
    path = ROOT/reference['path']
    require(path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == reference['sha256'], 'preparation source configuration bytes differ')
    git('ls-files', '--error-unmatch', '--', reference['path'])
    config = load_json(path)
    require(set(config) == {'schemaVersion', 'action', 'candidateKey', 'state', 'bootstrapArtifactDigest', 'configuration'} and
            config['schemaVersion'] == 1 and config['action'] in PREPARATIONS and
            re.fullmatch(r'sha256:[a-f0-9]{64}', config['candidateKey']) and
            re.fullmatch(r'sha256:[a-f0-9]{64}', config['bootstrapArtifactDigest']), 'invalid reviewed private preparation configuration')
    return config


def native_context(durable, config):
    require(set(config) == {'candidateKey', 'operation', 'nativeBootstrapOperationId', 'endpoint', 'requesterCredentialEnv', 'viewId', 'turnId'} and
            config['candidateKey'] == durable.store.candidate and config['requesterCredentialEnv'] == 'HONUA_AGENT_REQUESTER_KEY' and
            config['viewId'] == 'studio-native' and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', config['turnId']), 'native context setup must use its actual issued agent key and reviewed turn')
    native_key = canonical_digest({'kind': 'native-bootstrap', 'candidateKey': config['candidateKey'], 'operationId': config['nativeBootstrapOperationId']})
    issued = durable.state.get('scenarioOperations', {}).get(native_key+'/key-agent', {})
    require(issued.get('producer') == 'private-native-bootstrap' and issued.get('candidateKey') == config['candidateKey'], 'native context lacks actual retained agent key issuance')
    key_record = issued['value']
    require('agent:workspace' in key_record['permissions'] and isinstance(key_record['apiKeyId'], str), 'native context key lacks observed workspace grant')
    phase = canonical_digest({'kind': 'native-agent-context', 'candidateKey': config['candidateKey'], 'operation': config['operation'], 'keyId': key_record['apiKeyId']})
    stages = durable.state.setdefault('studioBootstrapOperations', {})
    require(phase+'/start' not in stages, 'native context setup already started; reconcile original observations without replay')
    client = BootstrapHttp(operation=config['operation'], durable=durable, context={'componentBindingDigest': config['candidateKey']},
        endpoint=config['endpoint'], credential_env=config['requesterCredentialEnv'], credential_kind='api-key',
        attempt_id='native-context-'+phase[7:31], timeout=300)
    def retain(stage, value):
        client.authorize()
        require(phase+'/'+stage not in stages, 'native context stage already retained')
        stages[phase+'/'+stage] = value
        durable._persist()
    retain('start', {'configurationDigest': canonical_digest(config), 'issuedKey': key_record,
        'producerTaskArn': os.environ['PILOT_CONTROLLER_TASK_ARN']})
    target_answer = client.json('native-target', 'GET', '/api/v1/agent/target')
    target = body(target_answer)
    require(isinstance(target.get('targetId'), str) and target['targetId'], 'native target did not report actual deployment identity')
    workspace_answer = client.json('native-workspace-create', 'POST', '/api/v1/agent/workspaces',
        {'viewId': config['viewId'], 'targetId': target['targetId']})
    workspace = body(workspace_answer, (200, 201))
    require(isinstance(workspace.get('workspaceId'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', workspace['workspaceId']) and
            workspace.get('viewId') == config['viewId'] and workspace.get('targetId') == target['targetId'] and
            type(workspace.get('revision')) is int and workspace['revision'] == 0 and
            workspace.get('selectedResources') == [] and workspace.get('task') is None,
            'native initial context is not its exact actual empty workspace')
    turn_answer = client.json('native-turn-capture', 'POST', '/api/v1/agent/workspaces/'+workspace['workspaceId']+'/turns',
        {'turnId': config['turnId'], 'viewId': config['viewId'], 'contextRevision': workspace['revision'],
         'prompt': 'Prepare this isolated fixture context; do not execute a campaign task.'})
    turn = body(turn_answer)
    require(turn.get('turnId') == config['turnId'] and turn.get('context') == workspace, 'native server did not capture the requested actual context/turn')
    workspace_read_answer = client.json('native-workspace-readback', 'GET',
        '/api/v1/agent/workspaces/'+workspace['workspaceId']+'?viewId='+config['viewId'])
    turn_read_answer = client.json('native-turn-readback', 'GET',
        '/api/v1/agent/workspaces/'+workspace['workspaceId']+'/turns/'+turn['turnId'])
    workspace_read, turn_read = body(workspace_read_answer), body(turn_read_answer)
    require(workspace_read == workspace and turn_read.get('turnId') == turn['turnId'] and
            turn_read.get('context') == workspace and turn_read.get('status') == turn.get('status'),
            'actual issued agent cannot read back its installed workspace membership and captured turn')
    value = {'schema': 'honua.native-agent-context/v1', 'candidateKey': config['candidateKey'],
        'issuedAgentKey': key_record, 'ownerPrincipalId': 'apikey:api-key:'+key_record['apiKeyId'],
        'agentContext': {'workspaceId': workspace['workspaceId'], 'turnId': turn['turnId']},
        'target': target, 'workspace': workspace, 'turn': turn,
        'nativeOriginals': {'target': target_answer, 'workspace': workspace_answer, 'turn': turn_answer,
            'workspaceReadback': workspace_read_answer, 'turnReadback': turn_read_answer},
        'producerTaskArn': os.environ['PILOT_CONTROLLER_TASK_ARN'], 'qualification': False}
    retain('completed', value)
    return {'key': phase+'/completed', 'sha256': canonical_digest(value), 'value': value, 'qualification': False}


def execute(bundle, output):
    document = load_json(Path(bundle)/'preparation.json')
    manifest, identity, state = bundle_identity(bundle, operation=document['operation'], case_id=None, target=None, shard=0, shards=6)
    config = source_configuration(manifest['configuration'])
    require(config['action'] == identity['operation'] and config['candidateKey'] == identity['candidateKey'] and state == config['state'], 'private preparation manifest differs from source/accounting')
    require(datetime.fromisoformat(manifest['expiresAt'].replace('Z', '+00:00')) > datetime.now(timezone.utc), 'private preparation manifest expired')
    durable = DurableExecution(open_store(state, config['candidateKey']), Path(bundle)/'budget-ledger.json')
    try:
        require(durable.state.get('bootstrapImport', {}).get('artifactDigest') == config['bootstrapArtifactDigest'], 'private preparation cannot substitute authenticated history')
        if config['action'] == 'prepare-native-context':
            result = native_context(durable, config['configuration'])
        else:
            result = prepare_initial_fixture(durable=durable, configuration=config['configuration'])
            require(result.get('qualification') is False, 'fixture preparation cannot qualify a campaign')
        result = {'schema': 'honua.private-controller-preparation-result/v1', 'operation': config['action'],
            'candidateKey': config['candidateKey'], 'sourceConfiguration': manifest['configuration'],
            'producerTaskArn': os.environ['PILOT_CONTROLLER_TASK_ARN'], 'result': result,
            'requiresSourceReviewBeforeFreeze': True, 'qualification': False}
        Path(output).write_bytes(canonical(result)+b'\n')
    finally:
        durable.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['bundle', 'execute'])
    parser.add_argument('--configuration')
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.action == 'execute':
        require(args.output is not None and args.configuration is None, 'private preparation execution needs only fixed bundle/output')
        execute(args.bundle, args.output)
        return
    require(os.environ.get('GITHUB_REF') == 'refs/heads/trunk' and os.environ.get('GITHUB_REPOSITORY') == 'honua-io/honua-release' and
            os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and git('rev-parse', 'HEAD').decode().strip() == os.environ['GITHUB_SHA'], 'preparation requires the original checked trunk workflow')
    require(args.configuration is not None, 'reviewed preparation configuration required')
    path = ROOT/args.configuration
    reference = {'path': args.configuration, 'sha256': digest(path.read_bytes())}
    config = source_configuration(reference)
    require(path.read_bytes() == git('show', os.environ['GITHUB_SHA']+':'+args.configuration), 'preparation source differs from original workflow commit')
    manifest = {'schema': 'honua.private-controller-preparation/v1', 'componentBindingDigest': config['candidateKey'],
        'controller': {'repository': 'honua-io/honua-release', 'workflow': PREPARE_WORKFLOW, 'sourceSha': os.environ['GITHUB_SHA'],
            'runId': int(os.environ['GITHUB_RUN_ID']), 'runAttempt': int(os.environ['GITHUB_RUN_ATTEMPT'])},
        'expiresAt': (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), 'operation': config['action'],
        'state': config['state'], 'configuration': reference}
    args.bundle.mkdir(mode=0o700, parents=True, exist_ok=False)
    (args.bundle/'preparation.json').write_bytes(canonical(manifest)+b'\n')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('private preparation refused; retain original state/effects and reconcile without replay', file=sys.stderr)
        sys.exit(1)
