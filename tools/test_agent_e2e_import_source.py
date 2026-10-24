"""Original MCP task plus authenticated HTTP custody bounds source contribution."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from agent_e2e_import_backend import Session, identity
from agent_e2e_import_observer import CREATE
from agent_e2e_import_source import contribute_selected_source, original_source
from agent_e2e_pack import PackError, canonical_digest, load_json
from test_agent_e2e_import_observer import imported
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server

ROOT = Path(__file__).resolve().parents[1]
PACK = load_json(ROOT/'certification/agent-e2e/pack.v2.json')
CORPUS = ROOT/'certification/agent-e2e/corpus'
MANIFEST = load_json(CORPUS/'manifest.v1.json')


def exact_source(imported):
    source_path = 'town-A/parcels.4326.geojson'
    row = next(row for row in MANIFEST['files'] if row['path'] == source_path)
    source = imported.create['request']['source']
    source.update(fileName=Path(source_path).name, expectedSha256=row['sha256'], totalBytes=row['bytes'])
    imported.observer.binding['destinations'][0].update(fileName=source['fileName'], sourceSha256=row['sha256'], totalBytes=row['bytes'])
    return row


@pytest.fixture
def contribution(imported, adapter):
    row = exact_source(imported)
    context = {**adapter.http.context, 'caseId': 'P04:A:aws-ecs-redis-on', 'campaignMode': 'development'}
    imported.observer.http.context = copy.deepcopy(context)
    for reserved in adapter.durable.state['ledger']['entries']:
        reserved['caseId'] = context['caseId']
    action, _, worker = imported.execute(CREATE, imported.create, observe=False)
    dispatch = {'ordinal': 1, 'actionId': canonical_digest({**identity(context),
        'componentBindingDigest': context['componentBindingDigest'], 'ordinal': 1})[7:], 'requestDigest': canonical_digest(action)}
    selected = imported.observer.selected(action, dispatch, worker)
    action.update(type='tool_use', id='actual-provider-call-id')
    session = Session.__new__(Session)
    session.durable, session.context = adapter.durable, context
    session.key, session.campaign = canonical_digest(identity(context)), context
    session.retain('model-action-1', {'action': action, 'dispatch': dispatch})
    session.retain('model-result-1', worker)
    session.retain('import-selected-'+dispatch['actionId'], selected)
    calls = []
    def private(**kwargs):
        calls.append(kwargs)
        return {'inputPreparationOnly': True}
    kwargs = dict(selected=selected, dispatch=dispatch, durable=adapter.durable, context=context,
        observer={'endpoint': adapter.http.client.endpoint, 'credentialKind': 'api-key', 'attemptId': 'import-observer',
                  'agentContext': copy.deepcopy(imported.observer.http.client.agent_context)},
        configuration={'schemaVersion': 1}, pack=PACK, corpus_root=CORPUS, manifest=MANIFEST, contributor=private)
    return SimpleNamespace(kwargs=kwargs, calls=calls, session=session, source=row)


def test_only_original_model_created_task_and_exact_selected_bytes_reach_private_contributor(contribution, server):
    assert contribute_selected_source(**contribution.kwargs) == {'inputPreparationOnly': True}
    call = contribution.calls[0]
    assert call['source'] == {'path': str(CORPUS/contribution.source['path']),
        'sha256': contribution.source['sha256'], 'bytes': contribution.source['bytes']}
    assert call['task_reference'] == contribution.kwargs['selected']['nativeReferences']['task']
    assert all(method == 'GET' for method, path, *_ in server.seen if path != '/mcp')
    assert len([row for row in server.seen if row[1] == '/mcp' and CREATE.encode() in row[3]]) == 1


@pytest.mark.parametrize('change', ['other-producer', 'different-action', 'claimed-result', 'missing-task-get',
    'changed-task-get', 'missing-intent', 'another-observer', 'lease-lost', 'unpaid', 'wrong-candidate'])
def test_contribution_refuses_foreign_or_unresolved_model_and_original_custody(contribution, change):
    kwargs, session = contribution.kwargs, contribution.session
    records = kwargs['durable'].state['scenarioOperations']
    selected, dispatch = kwargs['selected'], kwargs['dispatch']
    if change == 'other-producer': records[session.phase_key('model-action-1')]['producer'] = 'setup'
    elif change == 'different-action': dispatch['actionId'] = 'e'*64
    elif change == 'claimed-result': records[session.phase_key('model-result-1')]['value']['agentResult']['content']['structuredContent'] = {'status': 'complete'}
    elif change == 'missing-task-get': del records[selected['nativeReferences']['task']['key']]
    elif change == 'changed-task-get': records[selected['nativeReferences']['task']['key']]['response']['httpStatus'] = 403
    elif change == 'missing-intent':
        response = records[selected['nativeReferences']['task']['key']]
        del records[response['intentReference']['key']]
    elif change == 'another-observer': kwargs['observer']['attemptId'] = 'different-requester'
    elif change == 'lease-lost': kwargs['durable'].alive = False
    elif change == 'unpaid':
        for row in kwargs['durable'].state['ledger']['entries']:
            row.update(state='settled', actualUsd='0')
    else: kwargs['context']['componentBindingDigest'] = 'sha256:'+'c'*64
    with pytest.raises(PackError): contribute_selected_source(**kwargs)
    assert not contribution.calls


@pytest.mark.parametrize('change', ['other-variant', 'other-scenario', 'local-artifact', 'wrong-bytes', 'wrong-name'])
def test_source_cannot_replace_authored_attachment(contribution, change):
    task = copy.deepcopy(contribution.kwargs['selected']['task'])
    case = contribution.kwargs['context']['caseId']
    if change == 'other-variant': case = 'P04:B:aws-ecs-redis-on'
    elif change == 'other-scenario': case = 'B03:A:aws-batch'
    elif change == 'local-artifact': task['source']['kind'] = 'local-process-artifact'
    elif change == 'wrong-bytes': task['source']['totalBytes'] -= 1
    else: task['source']['fileName'] = 'flood_zones.3857.geojson'
    with pytest.raises(PackError): original_source(pack=PACK, case_id=case, corpus_root=CORPUS, manifest=MANIFEST, task=task)


def test_large_attachment_keeps_actual_original_pin_and_requires_materialization():
    pin = next(row for row in MANIFEST['fixtures'] if row['id'] == 'large-import-s10')
    task = {'source': {'kind': 'browser-file', 'fileName': pin['name'], 'expectedSha256': pin['sha256'], 'totalBytes': pin['bytes']}}
    assert pin['bytes'] == 85688964 and pin['featureCount'] == 330000
    with pytest.raises(PackError, match='has not been materialized'):
        original_source(pack=PACK, case_id='B03:A:aws-batch', corpus_root=CORPUS, manifest=MANIFEST, task=task)


def test_changed_or_symlinked_corpus_never_reaches_private_transfer(contribution, tmp_path):
    kwargs = contribution.kwargs
    kwargs['corpus_root'] = tmp_path
    source = tmp_path/contribution.source['path']
    source.parent.mkdir()
    source.write_bytes(b'changed')
    with pytest.raises(PackError, match='bytes changed'): contribute_selected_source(**kwargs)
    source.unlink()
    source.symlink_to(CORPUS/contribution.source['path'])
    with pytest.raises(PackError): contribute_selected_source(**kwargs)
    assert not contribution.calls


def test_session_contributes_after_actual_worker_create_before_next_model_action(imported, adapter, monkeypatch):
    row = exact_source(imported)
    context = {**adapter.http.context, 'caseId': 'P04:A:aws-ecs-redis-on', 'campaignMode': 'development'}
    imported.observer.http.context = copy.deepcopy(context)
    for reserved in adapter.durable.state['ledger']['entries']:
        reserved['caseId'] = context['caseId']
    session = Session.__new__(Session)
    session.durable, session.context = adapter.durable, context
    session.key, session.campaign = canonical_digest(identity(context)), context
    session.pack, session.corpus_path = PACK, CORPUS/'manifest.v1.json'
    session.config = {'sourceContribution': {'schemaVersion': 1}}
    session.open_worker = lambda: None
    session.observer_for_recovery = lambda: imported.observer
    imported.observer.retain = session.retain
    session.execute_action = lambda action, dispatch, timeout: imported.execute(CREATE, action['input']['arguments'], observe=False)[2]
    session.retain('public-inputs', {'files': [{'path': row['path'], 'sha256': 'sha256:'+row['sha256']}]})
    calls = []
    def private(**kwargs):
        assert session.value('model-result-1') is not None
        assert session.value('import-ordinal-1')['task'] == kwargs['task']
        assert session.value('source-contribution-1') is None
        calls.append(kwargs)
        return {'inputPreparationOnly': True}
    monkeypatch.setitem(sys.modules, 'hosted_pilot.source_contribution', SimpleNamespace(contribute_original_source=private))
    action = {'name': 'mcp_call_tool', 'input': {'name': CREATE, 'arguments': imported.create}}
    dispatch = {'ordinal': 1, 'actionId': canonical_digest({**identity(context),
        'componentBindingDigest': context['componentBindingDigest'], 'ordinal': 1})[7:], 'requestDigest': canonical_digest(action)}
    reply = session.action({'action': action, 'dispatch': dispatch}, 30)
    assert calls[0]['source']['sha256'] == row['sha256']
    assert reply['agentResult']['content']['structuredContent']['taskId'] == calls[0]['task']['taskId']
    assert session.value('source-contribution-1') == {'inputPreparationOnly': True}
