"""Observer boundary tests; no fixture response is live semantic evidence."""
import io
import json
import base64
import copy

import pytest

from agent_e2e_pack import PackError
from agent_e2e_semantic_runner import HttpObserver, NoRedirect, pointer, run
from agent_e2e_semantics import CASES, SPATIAL_CASES, response_evidence, validate_response, validate_report, project_observation
from agent_e2e_pack import digest_bytes
from test_agent_e2e_semantics import observation
from test_agent_e2e_deployment import deployment, CAMPAIGN


def test_pointer_reads_response_fields_and_escaped_names():
    assert pointer({'a/b':{'~x':[1, 2]}}, '/a~1b/~0x/1') == 2
    with pytest.raises(PackError):
        pointer({}, 'not-a-pointer')


@pytest.mark.parametrize('endpoint', ['http://remote.example', 'https://user:pass@example.test',
                                      'https://example.test/?token=hidden'])
def test_untrusted_endpoint_rejected_before_credential_lookup(endpoint):
    with pytest.raises(PackError):
        HttpObserver(endpoint, 'NONEXISTENT_OBSERVER_CREDENTIAL')


def test_redirect_cannot_forward_observer_credentials():
    with pytest.raises(PackError, match='redirect'):
        NoRedirect().redirect_request(None, None, 302, '', {}, 'https://another.example')


def test_network_path_reference_and_credential_echo_are_rejected(monkeypatch):
    monkeypatch.setenv('SEMANTIC_TEST_TOKEN', 'private-test-token')
    observer = HttpObserver('https://candidate.example', 'SEMANTIC_TEST_TOKEN')
    with pytest.raises(PackError, match='origin'):
        observer.request({'method':'GET', 'path':'//another.example'})
    class Response(io.BytesIO):
        code = 200
    class Opener:
        def open(self, request, timeout):
            return Response(json.dumps({'echo':'private-test-token'}).encode())
    observer.opener = Opener()
    with pytest.raises(PackError, match='credential'):
        observer.request({'method':'GET', 'path':'/query'})


def test_http_observer_preserves_original_bytes(monkeypatch):
    monkeypatch.setenv('SEMANTIC_TEST_TOKEN', 'private-test-token')
    observer = HttpObserver('https://candidate.example', 'SEMANTIC_TEST_TOKEN')
    raw = b'{ "z": 1,\n  "a": [2, 3] }\n'
    class Response(io.BytesIO):
        code = 200
    class Opener:
        def open(self, request, timeout):
            return Response(raw)
    observer.opener = Opener()
    value = observer.request({'method':'GET', 'path':'/query'})
    assert base64.b64decode(value['rawBodyBase64']) == raw
    assert value['responseDigest'] == digest_bytes(raw)
    assert value['body'] == {'z':1, 'a':[2,3]}
    validate_response(value)


@pytest.mark.parametrize('mutation,reason', [
    (lambda value: value.pop('rawBodyBase64'), 'exact semantic response'),
    (lambda value: value.update(body={'invented':'success'}), 'parsed semantic body'),
    (lambda value: value.update(rawBodyBase64='not-base64!'), 'encoded semantic response'),
    (lambda value: value.update(rawBodyBase64=base64.b64encode(b'{}').decode()), 'response digest'),
    (lambda value: value.update(httpStatus=True), 'HTTP status'),
])
def test_response_evidence_rejects_unbound_or_changed_bytes(mutation, reason):
    value = response_evidence(200, b'{"observed":true}')
    mutation(value)
    with pytest.raises(PackError, match=reason):
        validate_response(value)


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'])
def test_response_evidence_rejects_ambiguous_json(raw):
    with pytest.raises(PackError):
        response_evidence(200, raw)


def test_runner_report_replays_observations_and_rejects_fabrication(tmp_path):
    # Transport fixtures exercise retention/replay only, never live qualification.
    campaign = {**CAMPAIGN, 'knowledge':{'revision':'test'}}
    config = {'schemaVersion':1, **campaign, 'target':'local-docker',
        'runtimeInterfaces':{'observed':'fixture schema'}, 'endpoint':'https://local-docker.example',
        'deployment':deployment('local-docker', campaign),
        'credentialEnv':'unused', 'sourceRequest':{'method':'GET','path':'/source'},
        'sourcePointer':'/body', 'cases':{}}
    replies = []
    for case in CASES:
        observed = observation(case)
        config['cases'][case] = {'requests':[{'method':'GET','path':'/operation'}],
            'result': {field:{'response':0, 'pointer':'/body/'+field} for field in observed['result']}}
        if case in SPATIAL_CASES:
            config['cases'][case]['source'] = {'requests':[
                {'method':'GET','path':'/points'}, {'method':'GET','path':'/polygons'}], 'result':{
                    'srid':{'response':0, 'pointer':'/body/srid'},
                    'pointLayer':{'response':0, 'pointer':'/body/collection'},
                    'polygonLayer':{'response':1, 'pointer':'/body/collection'}}}
            parts = []
            for part in ('sourceBefore','result','sourceAfter'):
                if part == 'result':
                    parts.append(observed[part])
                else:
                    parts.extend({'srid':3857, 'collection':observed[part][layer]}
                                 for layer in ('pointLayer','polygonLayer'))
        else:
            parts = [observed[part] for part in ('sourceBefore','result','sourceAfter')]
        replies.extend(response_evidence(200, json.dumps(part, indent=2).encode()) for part in parts)
    class Observer:
        def __init__(self, *args):
            self.replies = iter(copy.deepcopy(replies))
        def request(self, spec):
            return next(self.replies)
    root = tmp_path/'evidence'
    configuration = run(config, campaign, root, observer_factory=Observer)
    report = {'schemaVersion':1, 'producer':'independent-semantic-suite',
              **{k:campaign[k] for k in ('componentBindingDigest','knowledge')},
              'configurations':[configuration]}
    def resolve(directory, ref):
        path = directory/ref['path']
        assert digest_bytes(path.read_bytes()) == ref['sha256']
        return path
    validate_report(report, campaign, ['local-docker'], root, resolve)
    # Re-hashing a fabricated observation cannot bypass replay of the HTTP bytes.
    check = configuration['checks'][0]
    path = root/check['observation']['path']
    value = json.loads(path.read_text())
    value['observation']['result']['features'][0]['properties']['capacity'] = 900
    path.write_text(json.dumps(value), encoding='utf-8')
    check['observation']['sha256'] = digest_bytes(path.read_bytes())
    with pytest.raises(PackError, match='retained response projections'):
        validate_report(report, campaign, ['local-docker'], root, resolve)


@pytest.mark.parametrize('count', [0, -1, 17, True, '2'])
def test_source_capture_count_must_be_positive_and_bounded(count):
    with pytest.raises(PackError, match='source response count'):
        project_observation([], {'sourceCount':count, 'sourceFields':{}, 'result':{}})
