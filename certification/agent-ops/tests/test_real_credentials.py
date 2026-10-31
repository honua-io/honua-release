import json
import hashlib
import pytest
from pathlib import Path
from real_credentials import mint_operator


def retain(kind, body):
    raw = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    return {'key': kind, 'sha256': 'sha256:' + hashlib.sha256(raw).hexdigest()}


def test_issued_operator_secret_never_enters_receipt(monkeypatch):
    secret = 'a-different-private-credential'
    class Response:
        def __init__(self, status, body): self.status, self.body = status, body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return json.dumps(self.body).encode()
    responses = iter([Response(201, {'data': {'key': secret, 'apiKey': {
        'id': '11111111-1111-4111-8111-111111111111', 'name': 'site-operator', 'permissions': ['admin']}}}),
        Response(200, {}), Response(200, {'data': {
            'id': '11111111-1111-4111-8111-111111111111', 'permissions': ['admin'], 'canAuthenticate': True}})])
    monkeypatch.setattr('real_credentials.open_local', lambda request, timeout: next(responses))
    monkeypatch.setattr('real_seed._verify_runtime', lambda *args: {})
    credential = mint_operator('http://127.0.0.1:1234', 'controller-secret', project='ops-' + 'a' * 32,
                               compose_file=Path('/tmp/compose'), image_ref='server@sha256:' + 'a' * 64,
                               authorize=lambda: None, retain=retain)
    assert secret not in repr(credential)
    assert secret not in json.dumps(credential.public_receipt())
    assert credential.principal_id == '11111111-1111-4111-8111-111111111111'


def test_same_key_is_refused(monkeypatch):
    class Response:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return json.dumps({'data': {'key': 'same-controller-secret', 'apiKey': {
            'id': '11111111-1111-4111-8111-111111111111', 'name': 'site-operator', 'permissions': ['admin']}}}).encode()
    monkeypatch.setattr('real_credentials.open_local', lambda request, timeout: Response())
    monkeypatch.setattr('real_seed._verify_runtime', lambda *args: {})
    with pytest.raises(ValueError, match='incomplete'):
        mint_operator('http://127.0.0.1:1234', 'same-controller-secret', project='ops-' + 'a' * 32,
                      compose_file=Path('/tmp/compose'), image_ref='server@sha256:' + 'a' * 64,
                      authorize=lambda: None, retain=retain)


def test_unacknowledged_intent_prevents_request(monkeypatch):
    monkeypatch.setattr('real_seed._verify_runtime', lambda *args: {})
    monkeypatch.setattr('real_credentials.open_local', lambda *args, **kwargs: pytest.fail('network call'))
    with pytest.raises(ValueError, match='retention not acknowledged'):
        mint_operator('http://127.0.0.1:1234', 'controller-secret', project='ops-' + 'a' * 32,
                      compose_file=Path('/tmp/compose'), image_ref='server@sha256:' + 'a' * 64,
                      authorize=lambda: None, retain=lambda kind, body: None)
