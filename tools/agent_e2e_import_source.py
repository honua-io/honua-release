"""Supply only an original selected browser attachment to a native import task.

The agent must first create the task through its actual worker MCP action. This
adapter corroborates that action and the independently retained task GET, then
finds the exact source-frozen bytes. The private contributor owns human-session
custody and resumable input transfer; this module never creates or submits jobs.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from agent_e2e_budget import validate as validate_budget
from agent_e2e_gate import safe_file
from agent_e2e_import_observer import CREATE
from agent_e2e_mcp import validate_agent_context
from agent_e2e_pack import PackError, canonical_digest, digest_bytes, select, selected_files
from agent_e2e_semantics import validate_response


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def task_original(intent, response, *, observer, candidate, path):
    require(set(observer) == {'endpoint', 'credentialKind', 'attemptId', 'agentContext'} and
            validate_agent_context(observer['agentContext']) is not None,
            'source task observer requires its actual native workspace and turn')
    require(set(intent) == {'schemaVersion', 'candidateKey', 'attemptId', 'step', 'endpoint', 'method', 'path',
            'credentialKind', 'contentType', 'requestBodyDigest', 'agentContext'} and
            set(response) == {'schemaVersion', 'candidateKey', 'attemptId', 'step', 'intentDigest', 'intentReference', 'response'} and
            type(intent['schemaVersion']) is int and type(response['schemaVersion']) is int and
            intent['schemaVersion'] == response['schemaVersion'] == 1 and
            intent['candidateKey'] == response['candidateKey'] == candidate and
            intent['attemptId'] == response['attemptId'] == observer['attemptId'] and
            all(intent[field] == observer[field] for field in ('endpoint', 'credentialKind', 'agentContext')) and
            intent['credentialKind'] in ('api-key', 'bearer') and intent['step'] == response['step'] and
            intent['method'] == 'GET' and intent['path'] == path and intent['contentType'] == 'application/json' and
            intent['requestBodyDigest'] == digest_bytes(b'') and response['intentDigest'] == canonical_digest(intent),
            'source task original changed native context, observer or GET provenance')
    validate_response(response['response'])
    observed = response['response']
    require(observed['httpStatus'] == 200 and isinstance(observed['body'], dict) and
            observed['body'].get('success', True) is not False and 'error' not in observed['body'],
            'source task original is not a successful authenticated native read')
    return observed['body'].get('data', observed['body'])


def original_source(*, pack, case_id, corpus_root, manifest, task, materialized_root=None):
    """Resolve committed or pinned materialized bytes without changing the input."""
    source = task.get('source', {})
    require(source.get('kind') == 'browser-file', 'human contribution requires the original browser-file attachment')
    name, expected, size = source.get('fileName'), source.get('expectedSha256'), source.get('totalBytes')
    _, scenario = select(pack, case_id)
    if scenario['id'] == 'B03':
        large = [row for row in manifest['fixtures'] if row['id'] == 'large-import-s10' and row['id'] in scenario['fixtureIds']]
        require(len(large) == 1 and (name, expected, size) == (large[0]['name'], large[0]['sha256'], large[0]['bytes']),
                'B03 must retain its original full-size browser attachment')
    candidates = [(Path(corpus_root), row['path']) for row in selected_files(pack, case_id, manifest)
        if Path(row['path']).name == name and row['sha256'] == expected and row['bytes'] == size]
    for fixture in manifest['fixtures']:
        if fixture['id'] in scenario['fixtureIds'] and fixture.get('committed') is False and \
                (fixture.get('name'), fixture.get('sha256'), fixture.get('bytes')) == (name, expected, size):
            require(materialized_root is not None, 'the original large attachment has not been materialized')
            candidates.append((Path(materialized_root), fixture['name']))
    require(len(candidates) == 1, 'task source is not one exact selected original case attachment')
    root, relative = candidates[0]
    try:
        path = safe_file(root, relative)
    except ValueError as error:
        raise PackError(str(error)) from None
    digest, count = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            count += len(block)
    require(count == size and digest.hexdigest() == expected, 'original attachment bytes changed before contribution')
    return {'path': str(path.resolve(strict=True)), 'sha256': expected, 'bytes': count}


def contribute_selected_source(*, selected, dispatch, durable, context, observer, configuration,
                               pack, corpus_root, manifest, materialized_root=None, contributor=None):
    """Run the private input-only contributor between CREATE and the next action.

    Callers omit this hook when no reviewed human contribution is configured.
    An enabled hook requires the real private implementation and its custody;
    absence is an explicit failure, never a successful synthetic receipt.
    """
    durable._require_live_lease()
    validate_budget(durable.state['ledger'])
    require(context['componentBindingDigest'] == durable.store.candidate == durable.state['ledger']['candidateKey'],
            'source contribution belongs to a different installed candidate')
    reservations = [row for row in durable.state['ledger']['entries'] if row['state'] == 'reserved' and
        all(row.get(key) == context[key] for key in ('campaignId', 'caseId', 'attempt'))]
    require({'infrastructure', 'cleanup'} <= {row['kind'] for row in reservations},
            'source contribution requires prepaid attempt infrastructure and cleanup')
    identity = {key: context[key] for key in ('campaignId', 'caseId', 'attempt')}
    attempt = canonical_digest(identity)
    records = durable.state.get('scenarioOperations', {})

    def phase(name):
        key = canonical_digest({'producer': 'dataset-import-native', 'attempt': attempt, 'phase': name})
        row = records.get(key)
        require(isinstance(row, dict) and row.get('producer') == 'dataset-import-native' and row.get('identity') == identity and
                row.get('candidateKey') == durable.store.candidate and row.get('phase') == name,
                'source contribution lacks its original retained model action/result')
        return row['value']

    ordinal = dispatch.get('ordinal')
    require(type(ordinal) is int and ordinal > 0 and dispatch.get('actionId') == canonical_digest({
        **identity, 'componentBindingDigest': durable.store.candidate, 'ordinal': ordinal})[7:],
        'source contribution cannot use another producer action identity')
    action_record = phase('model-action-'+str(ordinal))
    action, worker = action_record['action'], phase('model-result-'+str(ordinal))
    public = worker.get('agentResult', {})
    require(action_record['dispatch'] == dispatch and selected.get('tool') == CREATE and
            action.get('name') == 'mcp_call_tool' and action.get('input', {}).get('name') == CREATE and
            dispatch.get('requestDigest') == selected.get('requestDigest') == canonical_digest({key: action[key] for key in ('name', 'input')}) and
            phase('import-selected-'+dispatch['actionId']) == selected and
            selected.get('workerResultDigest') == canonical_digest(public) and public.get('isError', False) is False and
            public.get('content', {}).get('isError', False) is False and
            public.get('content', {}).get('structuredContent') == selected.get('task'),
            'only the same original model-created native task can receive source input')
    task = selected['task']
    require(task.get('workspaceId') == action['input']['arguments'].get('workspaceId'),
            'source contribution changed the original model workspace')
    reference = selected['nativeReferences']['task']
    require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'}, 'source task lacks original authenticated GET custody')
    response = records.get(reference['key'])
    require(isinstance(response, dict) and canonical_digest(response) == reference['sha256'], 'original source task GET is absent or changed')
    intent_reference = response.get('intentReference', {})
    intent = records.get(intent_reference.get('key'))
    require(isinstance(intent, dict) and canonical_digest(intent) == intent_reference.get('sha256'),
            'original source task GET intent is absent or changed')
    path = '/api/v1/agent/workspaces/'+task['workspaceId']+'/dataset-import-tasks/'+task['taskId']+'/versions/'+str(task['revision'])
    require(task_original(intent, response, observer=observer, candidate=durable.store.candidate, path=path) == task and
            observer['agentContext']['workspaceId'] == task['workspaceId'],
            'source task differs from its original authenticated native read')
    source = original_source(pack=pack, case_id=context['caseId'], corpus_root=corpus_root, manifest=manifest,
                             task=task, materialized_root=materialized_root)
    require(isinstance(configuration, dict) and configuration.get('schemaVersion') == 1,
            'human source contribution requires reviewed private custody configuration')
    if contributor is None:
        try:
            from hosted_pilot.source_contribution import contribute_original_source as contributor
        except ImportError:
            raise PackError('private original-human source contributor is not installed') from None
    durable._require_live_lease()
    return contributor(task=copy.deepcopy(task), task_reference=copy.deepcopy(reference), durable=durable,
        context=copy.deepcopy(context), configuration=copy.deepcopy(configuration), source=source)
