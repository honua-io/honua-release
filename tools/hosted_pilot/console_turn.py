"""Adopt original human input; never create a turn or derive campaign expectations."""
from __future__ import annotations
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_e2e_pack import canonical_digest, digest_bytes, load_json, select
from controller_archive import require
from controller_handoff import ROOT, git


def declaration(value):
    require(isinstance(value, dict) and set(value) == {
        'workspaceId', 'sessionCustody', 'pack', 'caseId', 'contextSha256', 'sourceSha256'} and
        isinstance(value['workspaceId'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value['workspaceId']) and
        all(isinstance(value[name], str) and re.fullmatch(r'sha256:[a-f0-9]{64}', value[name])
            for name in ('contextSha256', 'sourceSha256')),
        'Console adoption needs exact reviewed original workspace/context/source bindings')
    pack = value['pack']
    require(isinstance(pack, dict) and set(pack) == {'path', 'sha256'} and
        pack['path'] == 'certification/agent-e2e/pack.v2.json' and
        re.fullmatch(r'sha256:[a-f0-9]{64}', pack['sha256']), 'Console turn must retain the original frozen campaign pack')
    path = ROOT/pack['path']
    require(path.is_file() and not path.is_symlink() and digest_bytes(path.read_bytes()) == pack['sha256'],
        'Console turn frozen pack bytes differ')
    git('ls-files', '--error-unmatch', '--', pack['path'])
    case, scenario = select(load_json(path), value['caseId'])
    return scenario['prompts'][case['variant']]


def retained_session(durable, reference, endpoint, human):
    require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'}, 'original Console session custody reference required')
    custody = durable.state.get('scenarioOperations', {}).get(reference['key'])
    require(isinstance(custody, dict) and canonical_digest(custody) == reference['sha256'] and
        custody.get('schema') == 'honua.private-console-human-session/v1' and
        custody.get('producer') == 'private-console-human-capture' and
        custody.get('candidateKey') == durable.store.candidate and custody.get('qualification') is False,
        'Console adoption lacks original independently captured human session')
    from browser_session import operator_partition
    observed = custody['observed']
    profile = observed['consoleProfile']
    require(custody['configuration']['consoleProfile'] == profile and
        profile['serverUri'].rstrip('/') == endpoint and
        observed['nativeTarget']['principal'] == human and human.get('isHumanSession') is True and
        human.get('authenticationKind') == 'operator-bearer' and
        observed['session'] == {'authenticated': True, 'environmentId': profile['id'],
            'principalId': operator_partition(human.get('subjectId'), profile)},
        'Console subject/profile/partition/native human or endpoint differs')
    return custody


def observe_session(durable, reference, endpoint, human):
    from browser_session import STORAGE_ENV, binding, observe, storage
    from worker_mcp_configuration import verify_injected_secret
    custody = retained_session(durable, reference, endpoint, human)
    binding(custody['storageState'])
    verify_injected_secret(custody['storageState'], STORAGE_ENV, 'us-east-1')
    observed = observe(custody['configuration'], storage(os.environ.get(STORAGE_ENV)))
    require(all(observed[name] == custody['observed'][name]
        for name in ('session', 'nativeTarget', 'consoleProfile', 'deployment')),
        'Console human session or source-reviewed deployment changed since capture')
    return observed


def validate_turn(value, *, turn, workspace, turn_id, human):
    prompt = declaration(value)
    require(turn.get('turnId') == turn_id and turn.get('status') == 'accepted' and
        turn.get('originatingActorId') == human.get('actorId') and isinstance(human.get('actorId'), str) and
        human['actorId'] and human.get('isHumanSession') is True and
        workspace.get('workspaceId') == value['workspaceId'] and turn.get('context') == workspace and
        canonical_digest(workspace) == value['contextSha256'] and
        workspace.get('tenantId') == human.get('tenantId'),
        'Console turn is not the original human-authored exact captured context')
    sources = {name: turn.get(name) for name in ('draft', 'localProcessSnapshot', 'datasetImportContext', 'browserFileSelection')}
    require(canonical_digest(sources) == value['sourceSha256'], 'Console original selected source or draft differs')
    messages = turn.get('messages')
    require(isinstance(messages, list), 'Console turn original user message is missing')
    selected = [row for row in messages if isinstance(row, dict) and row.get('turnId') == turn_id]
    require(len(selected) == 1 and selected[0].get('id') == turn_id+'-user' and
        selected[0].get('role') == 'user' and selected[0].get('content') == prompt and
        selected[0].get('evidenceKind', 'conversation') == 'conversation',
        'Console original prompt differs from frozen task or this turn already has a response')
    return prompt
