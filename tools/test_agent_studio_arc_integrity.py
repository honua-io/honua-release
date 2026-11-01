import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_e2e_studio_arc import score_goal, ui_provenance


def record(body, time):
    raw = json.dumps(body).encode()
    return SimpleNamespace(principal='controller', status=200, raw=raw, json=body,
                           sha256='sha256:' + hashlib.sha256(raw).hexdigest(), recorded_at=time)


def evidence(tmp_path):
    now = datetime.now(timezone.utc)
    at = lambda n: (now + timedelta(seconds=n)).isoformat()
    customer = 'subject:issuer:customer@tenant:public'
    version = {'itemId': 'item', 'versionId': 'version', 'sourceDraftId': 'draft', 'createdBy': customer, 'contentHash': 'content-hash'}
    events = [{'auditId': n, 'action': 'operation.completed', 'resourceType': 'operation_instance',
               'actor': customer, 'outcome': 'Success', 'timestamp': at(n), 'resourceId': 'handle' + str(n),
               'details': 'operationId=studio.draft.' + operation + ';status=Completed'}
              for n, operation in [(1, 'create'), (2, 'save-version')]]
    records = {'p08-version': record({'data': version}, at(3)),
               'studio-audit-before-0': record({'items': []}, at(0)),
               'studio-audit-after-0': record({'items': events}, at(3))}
    rows = []
    for n, (kind, path, data) in enumerate([
        ('studio_ui_create', '/api/studio/v1/studio/package-drafts', {'draftId': 'draft'}),
        ('studio_ui_save', '/api/studio/v1/studio/package-drafts/draft/content-versions', version)], 1):
        action = {'tool': kind, 'arguments': {'path': path, 'startedAt': at(n)}}
        raw = json.dumps(action).encode()
        response = json.dumps({'data': data}).encode()
        action_path, response_path = tmp_path / str(n), tmp_path / (str(n) + '.response')
        action_path.write_bytes(raw)
        response_path.write_bytes(response)
        interaction = json.dumps({'tool': 'studio_click', 'arguments': {'selector': 'button'}}).encode()
        interaction_path = tmp_path / (str(n) + '.click')
        interaction_path.write_bytes(interaction)
        rows.append({'status': 'pass', 'logPath': str(interaction_path),
                     'digest': 'sha256:' + hashlib.sha256(interaction).hexdigest()})
        rows.append({'status': 'pass', 'logPath': str(action_path), 'resultRawPath': str(response_path),
                     'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                     'resultRawSha256': 'sha256:' + hashlib.sha256(response).hexdigest(), 'resultHttpStatus': 201})
    handles = {
        'handle1': {'operationInstanceId': 'handle1', 'operationId': 'studio.draft.create', 'status': 'Completed',
                    'resourceIds': {'draftId': 'draft', 'itemId': 'item'}},
        'handle2': {'operationInstanceId': 'handle2', 'operationId': 'studio.draft.save-version', 'status': 'Completed',
                    'resourceIds': {'itemId': 'item', 'versionId': 'version', 'contentHash': 'content-hash'}},
    }
    return records, rows, customer, handles.get


def test_ui_evidence_rejects_mcp_only_wrong_actor_and_wrong_map(tmp_path):
    records, rows, customer, handle_lookup = evidence(tmp_path)
    assert ui_provenance(records, rows, customer, handle_lookup)
    with pytest.raises(ValueError, match='uniquely bind'):
        ui_provenance(records, rows, customer, lambda _id: None)
    with pytest.raises(ValueError, match='browser create and save'):
        ui_provenance(records, [], customer, handle_lookup)
    with pytest.raises(ValueError, match='UI interactions'):
        ui_provenance(records, [rows[1], rows[3]], customer, handle_lookup)
    records['studio-audit-after-0'].json['items'][1]['actor'] = 'api-key:other'
    # Re-seal the changed controller observation to isolate the principal check.
    records['studio-audit-after-0'] = record(records['studio-audit-after-0'].json,
                                              records['studio-audit-after-0'].recorded_at)
    with pytest.raises(ValueError, match='audit events'):
        ui_provenance(records, rows, customer, handle_lookup)
    records, rows, customer, handle_lookup = evidence(tmp_path)
    records['p08-version'] = record({'data': {'itemId': 'different', 'versionId': 'version',
        'sourceDraftId': 'draft', 'createdBy': customer, 'contentHash': 'content-hash'}}, records['p08-version'].recorded_at)
    with pytest.raises(ValueError, match='browser create and save'):
        ui_provenance(records, rows, customer, handle_lookup)


def test_style_label_and_unsupported_goal_negatives():
    from agent_browser_studio import map_spec, label_ids
    body = {'layers': [{'id': n, 'visible': True} for n in ('parcels', 'flood_zones', 'shelters')],
            'mapSpec': map_spec(True)}
    records = {'p08-version': record({'data': {'envelope': {'body': body}}}, '2026-11-01T00:00:00Z'),
               'p08-render': record({'visibleLabelSourceIds': label_ids('P09', 'A')}, '2026-11-01T00:00:00Z')}
    def scores(goal):
        return {r['assertion']: r['status'] for r in score_goal(records, goal, 'A')}
    assert set(scores('style and labels').values()) == {'pass'}
    body['mapSpec']['layers'][1]['paint']['fill-color'] = '#ff0000'
    records['p08-version'] = record({'data': {'envelope': {'body': body}}}, '2026-11-01T00:00:00Z')
    assert scores('style and labels')['goal.authored-style'] == 'fail'
    assert scores('share with operators')['goal.sharing'] == 'fail'
    assert scores('filter only parcels')['goal.filters'] == 'fail'
    assert scores('make it red')['goal.unsupported-colour'] == 'fail'
    records['p08-render'] = record({'visibleLabelSourceIds': []}, '2026-11-01T00:00:00Z')
    assert scores('labels')['goal.labels'] == 'fail'


def test_browser_gate_records_native_create_and_save(tmp_path):
    from agent_browser_studio import OriginLock, PlaywrightPage
    from agent_e2e_local_runner import ActionLog
    page = PlaywrightPage.__new__(PlaywrightPage)
    page.origin = OriginLock('http://localhost:1234')
    page._stage_gate = page._identity = page._assistant_log = None
    page._refusal_log = ActionLog(tmp_path, 1, set(), 10)
    page.aborted = []
    class Response:
        status = 201
        headers = {'content-type': 'application/json'}
        def body(self): return b'{"data":{"draftId":"draft"}}'
    class Route:
        def __init__(self, path):
            self.request = SimpleNamespace(url='http://localhost:1234' + path, method='POST', resource_type='fetch')
        def fetch(self, **kwargs): return Response()
        def fulfill(self, **kwargs): pass
        def abort(self, reason): raise AssertionError(reason)
    page._gate(Route('/api/studio/v1/studio/package-drafts'))
    page._gate(Route('/api/studio/v1/studio/package-drafts/draft/content-versions'))
    assert [row['tool'] for row in page._refusal_log.rows] == ['studio_ui_create', 'studio_ui_save']
    assert all(row['status'] == 'pass' and row['resultHttpStatus'] == 201 for row in page._refusal_log.rows)
    assert json.loads((tmp_path / 'action-0001.json').read_text())['arguments']['startedAt']


def test_supported_filter_requires_both_saved_rules_and_rendered_labels():
    from agent_browser_studio import map_spec, label_ids
    body = {'layers': [{'id': n, 'visible': True} for n in ('parcels', 'flood_zones', 'shelters')],
            'mapSpec': map_spec(True, filtered=True)}
    records = {'p08-version': record({'data': {'envelope': {'body': body}}}, '2026-11-01T00:00:00Z'),
               'p08-render': record({'visibleLabelSourceIds': label_ids('P24', 'A')}, '2026-11-01T00:00:00Z')}
    goal = 'Show only open shelters with capacity over 50'
    def checks(): return {r['assertion']: r['status'] for r in score_goal(records, goal, 'A')}
    assert checks()['goal.filters'] == 'pass'
    assert checks()['goal.labels'] == 'pass'
    body['mapSpec']['layers'][-1].pop('filter')
    records['p08-version'] = record({'data': {'envelope': {'body': body}}}, '2026-11-01T00:00:00Z')
    assert checks()['goal.filters'] == 'fail'
