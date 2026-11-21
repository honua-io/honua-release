import json

import pytest

import agent_safety_seed as seed


class Fake:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def call(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body, headers))
        status, document = self.responses.pop(0)
        return status, json.dumps(document).encode() if document is not None else b'', {'ETag': '"e1"'}


def test_data_unwraps_the_envelope_and_splits_the_route():
    client = Fake((201, {'success': True, 'data': {'id': 'f1'}}))
    assert seed.data(client, 'POST /api/v1/admin/folders', {'name': 'x'}) == ({'id': 'f1'}, {'ETag': '"e1"'})
    assert client.calls[0][:2] == ('POST', '/api/v1/admin/folders')


def test_a_refused_seed_call_stops_the_walk():
    with pytest.raises(seed.SeedFailed, match='HTTP 409'):
        seed.data(Fake((409, {'detail': 'reserved'})), 'POST /api/v1/admin/folders')


def test_the_folder_is_created_against_the_current_metadata_etag():
    admin = Fake((200, {'success': True, 'data': []}), (201, {'success': True, 'data': {'metadata': {'id': 'f1'}}}))
    assert seed.seed_folder(admin) == 'f1'
    assert admin.calls[1][3] == {'If-Match': '"e1"'}


def test_a_candidate_without_the_imported_data_connection_cannot_be_seeded():
    with pytest.raises(seed.SeedFailed, match='Imported data'):
        seed.seed_layer(Fake((200, {'success': True, 'data': [{'name': 'other', 'connectionId': 'c'}]})))


def test_the_seeded_layer_names_its_storage_layer_id(monkeypatch):
    monkeypatch.setattr(seed, 'sql', lambda statement: 'svc-publish-agentsafetyseed')
    admin = Fake((200, {'success': True, 'data': [{'name': 'Imported data', 'connectionId': 'c1'}]}),
                 (201, {'success': True, 'data': {'serviceName': 'agentsafetyseed', 'layerId': 1, 'storageLayerId': 7}}))
    world = seed.seed_layer(admin)
    assert world['storageLayerId'] == '7' and world['serviceId'] == 'svc-publish-agentsafetyseed'
