import json
import subprocess

import pytest

from live_observer import observe_metadata_security


def test_security_observation_fails_closed_on_missing_graph_and_broken_links(tmp_path, monkeypatch):
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}')
    document = {'graphCount': 1, 'services': [{'id': 's', 'name': 'parcels', 'policyPresent': True, 'policy': None}],
                'resources': [{'id': 'r', 'policyPresent': True, 'policy': None, 'policyIds': []}], 'policies': [],
                'publications': [{'id': 'p', 'serviceId': 's', 'resourceId': 'r'}]}
    def run(argv, **kwargs):
        output = 'a' * 64 if argv[1] == 'compose' else json.dumps(document)
        return subprocess.CompletedProcess(argv, 0, output, '')
    monkeypatch.setattr(subprocess, 'run', run)
    project = 'ops-' + 'a' * 32
    assert observe_metadata_security(project, str(compose), ['parcels'])['security'] == document
    document['publications'][0]['resourceId'] = 'missing'
    with pytest.raises(ValueError, match='links incomplete'):
        observe_metadata_security(project, str(compose), ['parcels'])
    document['graphCount'] = 0
    with pytest.raises(ValueError, match='inventory incomplete'):
        observe_metadata_security(project, str(compose), ['parcels'])
    document['graphCount'] = 1
    document['publications'][0]['resourceId'] = 'r'
    document['resources'][0]['policyIds'] = ['missing-policy']
    with pytest.raises(ValueError, match='links incomplete'):
        observe_metadata_security(project, str(compose), ['parcels'])
    document['resources'][0]['policyIds'] = []
    document['services'].append({'id': 'other', 'name': 'parcels', 'policyPresent': True, 'policy': None})
    with pytest.raises(ValueError, match='inventory incomplete'):
        observe_metadata_security(project, str(compose), ['parcels'])
