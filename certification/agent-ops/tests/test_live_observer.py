import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from live_observer import observe_layer


class Handler(BaseHTTPRequestHandler):
    corrupt = False
    credentials = []
    add_during_read = False
    id_reads = 0
    echo_key = False
    echo_other = ''

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.credentials.append(self.headers.get("X-API-Key"))
        path = urlparse(self.path)
        params = parse_qs(path.query)
        if not path.path.endswith("/query"):
            body = {"objectIdField": "id", "fields": [{"name": "id", "type": "integer"}]}
        elif "returnIdsOnly" in params:
            type(self).id_reads += 1
            body = {"objectIds": [1, 2, 3] if self.add_during_read and self.id_reads > 1 else [1, 2]}
        else:
            oid = int(params["objectIds"][0])
            body = {"features": [] if self.corrupt and oid == 2 else [{"attributes": {"id": oid, "name": "item"}, "geometry": {"x": oid, "y": 0}}]}
        if self.echo_key:
            body['echo'] = self.headers.get('X-API-Key')
        if self.echo_other:
            body['other'] = self.echo_other
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


def test_controller_reads_all_features_and_fails_on_missing():
    Handler.corrupt = False
    Handler.credentials = []
    Handler.add_during_read = False
    Handler.id_reads = 0
    Handler.echo_key = False
    Handler.echo_other = ''
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        result = observe_layer(base, "/layer", "controller-secret")
        assert result["count"] == 2
        assert [f["id"] for f in result["features"]] == [1, 2]
        assert all(len(f["geometry_sha256"]) == 64 for f in result["features"])
        assert len(result["evidence"]) == 5
        assert set(Handler.credentials) == {"controller-secret"}
        assert "controller-secret" not in json.dumps(result)
        Handler.corrupt = True
        with pytest.raises(ValueError, match="missing or ambiguous"):
            observe_layer(base, "/layer", "controller-secret")
        Handler.corrupt = False
        Handler.id_reads = 0
        Handler.add_during_read = True
        with pytest.raises(ValueError, match="inventory changed"):
            observe_layer(base, "/layer", "controller-secret")
        Handler.echo_key = True
        with pytest.raises(ValueError, match='disclosed a credential'):
            observe_layer(base, '/layer', 'controller-secret')
        Handler.echo_key = False
        Handler.echo_other = 'operator-secret-outside-environment'
        with pytest.raises(ValueError, match='disclosed a credential'):
            observe_layer(base, '/layer', 'controller-secret', sensitive_values=(Handler.echo_other,))
    finally:
        Handler.echo_key = False
        Handler.echo_other = ''
        server.shutdown()
        thread.join()
        server.server_close()


def test_database_snapshot_is_project_scoped_and_missing_table_fails(tmp_path, monkeypatch):
    import subprocess
    from live_observer import observe_database

    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}\n')
    calls = []
    def run(argv, **_kwargs):
        calls.append(argv)
        if 'ps' in argv:
            return subprocess.CompletedProcess(argv, 0, 'a' * 64, '')
        return subprocess.CompletedProcess(argv, 0, '{"honua_data.e2e_src_fs":{"fields":[{"name":"id","type":"integer"}],"rows":[{"id":1}]}}', '')
    monkeypatch.setattr(subprocess, 'run', run)
    project = 'ops-' + 'c' * 32
    snapshot = observe_database(project, str(compose), [('honua_data', 'e2e_src_fs')])
    assert snapshot['honua_data.e2e_src_fs']['rows'] == [{'id': 1}]
    assert calls[0][0:4] == ['docker', 'compose', '-p', project]
    assert calls[1][0:3] == ['docker', 'exec', 'a' * 64]
    with pytest.raises(ValueError, match='table'):
        observe_database(project, str(compose), [('public', 'foo;drop')])
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, '', 'missing'))
    with pytest.raises(RuntimeError, match='container identity unavailable'):
        observe_database(project, str(compose), [('honua_data', 'ops_absent')])


def test_saved_map_requires_actual_reopen_and_fresh_matching_draft(monkeypatch):
    import live_observer
    item = '11111111-1111-4111-8111-111111111111'
    version = '22222222-2222-4222-8222-222222222222'
    draft = '33333333-3333-4333-8333-333333333333'
    envelope = {'family': 'map', 'body': {'layers': ['a']},
                'validation': {'status': 'valid', 'diagnostics': [], 'generatedAt': '2026-10-31T00:00:00Z'}}
    calls = []
    def read(base, path, key, *, method='GET', timeout=10, sensitive_values=()):
        calls.append((method, path))
        if method == 'POST':
            data = {'draftId': draft, 'envelope': envelope}
        elif 'package-drafts' in path:
            data = {'draftId': draft, 'itemId': item, 'baseVersionId': version, 'envelope': envelope}
        else:
            data = {'itemId': item, 'versionId': version, 'envelope': envelope}
        return {'data': data}, {'status': 200}
    monkeypatch.setattr(live_observer, '_controller_json', read)
    result = live_observer.reopen_saved_map('http://127.0.0.1:1', item, version, 'controller')
    assert result['draft_id'] == draft
    assert [call[0] for call in calls] == ['GET', 'POST', 'GET']
    def corrupt(base, path, key, *, method='GET', timeout=10, sensitive_values=()):
        body, evidence = read(base, path, key, method=method, timeout=timeout)
        if 'package-drafts' in path:
            body['data']['envelope'] = {'family': 'map', 'body': {'layers': []}}
        return body, evidence
    monkeypatch.setattr(live_observer, '_controller_json', corrupt)
    with pytest.raises(ValueError, match='differs'):
        live_observer.reopen_saved_map('http://127.0.0.1:1', item, version, 'controller')


def test_reopened_map_only_allows_fresh_validation_timestamp():
    from copy import deepcopy
    from live_observer import same_reopened_envelope
    saved = {'family': 'map', 'body': {'layers': ['a']},
             'validation': {'status': 'valid', 'diagnostics': [], 'generatedAt': '2026-10-31T00:00:00Z'}}
    reopened = deepcopy(saved)
    reopened['validation']['generatedAt'] = '2026-10-31T01:00:00Z'
    assert same_reopened_envelope(saved, reopened)
    reopened['body']['layers'] = []
    assert not same_reopened_envelope(saved, reopened)
    reopened = deepcopy(saved)
    reopened['validation']['generatedAt'] = 'not-a-time'
    assert not same_reopened_envelope(saved, reopened)


def test_runtime_requires_every_expected_healthy_replica(tmp_path, monkeypatch):
    import subprocess
    from live_observer import observe_runtime
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}')
    project, cid, digest = 'ops-' + 'a' * 32, 'c' * 64, 'sha256:' + 'b' * 64
    image = 'server@' + digest
    def run(argv, **kwargs):
        if 'ps' in argv: stdout = cid
        elif 'image' in argv: stdout = digest
        else: stdout = digest + '|running|healthy'
        return subprocess.CompletedProcess(argv, 0, stdout, '')
    monkeypatch.setattr(subprocess, 'run', run)
    assert observe_runtime(project, str(compose), image, 1)['replicas'][0]['image_id'] == digest
    with pytest.raises(ValueError, match='missing or duplicate'):
        observe_runtime(project, str(compose), image, 2)


def test_service_settings_requires_access_policy_and_matching_identity(monkeypatch):
    import live_observer
    def read(*args, **kwargs):
        return {'success': True, 'data': {'serviceName': 'shelters',
                'enabledProtocols': ['featureserver'], 'accessPolicy': {'allowAnonymous': False}}}, {'status': 200}
    monkeypatch.setattr(live_observer, '_controller_json', read)
    assert live_observer.observe_service_settings('http://127.0.0.1:1', 'shelters', 'controller')['settings']['accessPolicy']['allowAnonymous'] is False
    def missing(*args, **kwargs):
        return {'success': True, 'data': {'serviceName': 'shelters', 'enabledProtocols': []}}, {'status': 200}
    monkeypatch.setattr(live_observer, '_controller_json', missing)
    with pytest.raises(ValueError, match='ACL missing'):
        live_observer.observe_service_settings('http://127.0.0.1:1', 'shelters', 'controller')


def test_source_geometry_requires_exact_complete_matching_inventory(tmp_path, monkeypatch):
    import subprocess
    from live_observer import compare_source_geometry
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}')
    project = 'ops-' + 'a' * 32
    features = [{'properties': {'source_id': 'one'}, 'geometry': {'type': 'Point', 'coordinates': [1, 2]}}]
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, '{"expected":1,"actual":1,"matching":1}', '')
    monkeypatch.setattr(subprocess, 'run', run)
    assert compare_source_geometry(project, str(compose), 'ops_town', features, srid=4326)['counts']['matching'] == 1
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, '{"expected":1,"actual":1,"matching":0}', ''))
    with pytest.raises(ValueError, match='geometries do not match'):
        compare_source_geometry(project, str(compose), 'ops_town', features, srid=4326)
    with pytest.raises(ValueError, match='invalid'):
        compare_source_geometry(project, str(compose), 'ops_town;drop', features, srid=4326)
