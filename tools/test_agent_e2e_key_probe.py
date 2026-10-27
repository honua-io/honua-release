"""OGC item probe loopback fixtures, without model or approval claims."""
import copy
import json

import pytest

from agent_e2e_key_probe import KeyPermissionProbe
from agent_e2e_pack import PackError
from agent_e2e_scenario_backend import ScenarioHttp
from test_agent_e2e_setup_http import server
from test_agent_e2e_key_secret import consumer, SECRET


@pytest.fixture
def probe(consumer, server, monkeypatch):
    from types import SimpleNamespace
    issued = consumer.instance.consume('issuance', 'reference')
    monkeypatch.setenv('P16_INDEPENDENT_TOKEN', 'separate-independent-token')
    observer = ScenarioHttp(durable=consumer.durable, context=consumer.http.context, endpoint=server.url,
                            credential_env='P16_INDEPENDENT_TOKEN', attempt_id='independent-observer')
    observer.authorize = lambda: None
    observer.client.authorize = observer.authorize
    feature = {'type': 'Feature', 'id': '42', 'geometry': {'type': 'Point', 'coordinates': [1, 2]},
               'properties': {'name': 'original'}}
    state = SimpleNamespace(revoked=False, change=None, stored=copy.deepcopy(feature))
    replacement = {**feature, 'properties': {'name': 'changed'}}
    def respond(method, path, raw):
        assert path == '/ogc/features/collections/owned/items/42'
        token = server.seen[-1][2].get('X-Api-Key')
        assert token in (SECRET, 'separate-independent-token')
        if method == 'PUT':
            assert token == SECRET and json.loads(raw) == replacement
            if state.change == 'changed-data': state.stored['properties']['name'] = 'unexpected'
            status = 200 if state.change == 'allowed-write' else 403
            return status, {}, b'{"title":"Forbidden","status":403}'
        if token == SECRET and state.revoked:
            status = 403 if state.change == 'scope-only' else 401
            return status, {}, b'{"title":"Unauthorized"}'
        body = copy.deepcopy(state.stored)
        if state.change == 'protocol-error': body = {'error': {'code': 403}}
        return 200, {}, json.dumps(body).encode()
    server.respond = respond
    state.instance = KeyPermissionProbe(issued=issued, independent=observer, key_http=consumer.http,
        resource={'collectionId': 'owned', 'featureId': '42', 'replacement': replacement})
    return state


def test_same_issued_key_read_write_denial_and_later_401(probe, consumer, server):
    before = probe.instance.start()
    assert before['read']['observation']['httpStatus'] == 200
    assert before['writeDenial']['observation']['httpStatus'] == 403
    assert before['qualification'] is False
    probe.revoked = True  # Fixture state transition, never a scripted product revoke.
    after = probe.instance.after_revocation()
    assert after['authenticationDenial']['observation']['httpStatus'] == 401
    assert not any('/revoke' in row[1] for row in server.seen)
    assert SECRET not in json.dumps(consumer.durable.state)


@pytest.mark.parametrize('change', ['changed-data', 'allowed-write', 'protocol-error'])
def test_invalid_permission_or_poststate_cannot_pass(probe, change):
    probe.change = change
    with pytest.raises(PackError): probe.instance.start()


@pytest.mark.parametrize('change', [None, 'scope-only'])
def test_authentication_must_stop_not_merely_lose_scope(probe, change):
    probe.instance.start()
    probe.change = change
    probe.revoked = change == 'scope-only'
    with pytest.raises(PackError, match='still authenticates'): probe.instance.after_revocation()


def test_write_probe_is_not_repeated_after_unknown_or_failed_outcome(probe, server):
    probe.change = 'allowed-write'
    with pytest.raises(PackError): probe.instance.start()
    before = len(server.seen)
    with pytest.raises(PackError, match='already started'): probe.instance.start()
    assert len(server.seen) == before


def test_issued_key_cannot_be_its_own_independent_observer(probe, server):
    original = probe.instance
    original.independent.client.token = SECRET
    before = len(server.seen)
    with pytest.raises(PackError, match='cannot use the issued credential'):
        KeyPermissionProbe(issued=original.issued, independent=original.independent,
                           key_http=original.key_http, resource=original.resource)
    assert len(server.seen) == before
