"""Controller-only HTTP for fixture setup; never provisions infrastructure.

The caller supplies current lease/budget authorization and create-only retention.
Production retention must survive controller loss. Local files suffice only for
nonqualifying development probes. A retained intent never authorizes resending.
Retention acknowledgements provide a nonempty key and the SHA-256 canonical
digest of the exact value. The controller must enforce its hard action deadline.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from agent_e2e_mcp import parse
from agent_e2e_pack import PackError, canonical_digest, digest_bytes
from agent_e2e_semantics import MAX_RESPONSE_BYTES, response_evidence


def require(condition, message):
    if not condition:
        raise PackError(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise PackError('fixture setup refuses endpoint redirects')


class SetupHttp:
    def __init__(self, endpoint, credential_env, *, identity, authorize, retain,
                 credential_kind='api-key', private_values=(), timeout=300, clock=time.monotonic):
        require(isinstance(endpoint, str) and all(ord(c) > 32 for c in endpoint)
                and '\\' not in endpoint, 'invalid setup endpoint')
        try:
            url = urllib.parse.urlsplit(endpoint)
            url.port
        except ValueError:
            raise PackError('invalid setup endpoint') from None
        require(url.hostname and not url.username and not url.password and not url.query and not url.fragment
                and url.path in ('', '/') and (url.scheme == 'https' or
                url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')),
                'setup endpoint requires pinned TLS origin or loopback')
        require(isinstance(credential_env, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', credential_env)
                and credential_kind in ('api-key', 'bearer'), 'invalid setup credential reference')
        token = os.environ.get(credential_env)
        require(isinstance(token, str) and 0 < len(token) <= 8192 and all(ord(c) >= 32 and ord(c) != 127 for c in token),
                'missing or invalid setup credential')
        require(isinstance(identity, dict) and set(identity) == {'candidateKey', 'attemptId'}
                and isinstance(identity['candidateKey'], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', identity['candidateKey'])
                and isinstance(identity['attemptId'], str) and re.fullmatch(r'[A-Za-z0-9-]{1,80}', identity['attemptId']),
                'invalid setup identity')
        require(callable(authorize) and callable(retain), 'setup requires authorization and retention')
        require(isinstance(private_values, (tuple, list)) and len(private_values) <= 16
                and all(isinstance(value, str) and 0 < len(value) <= 8192 for value in private_values),
                'invalid private setup values')
        require(type(timeout) in (int, float) and 0 < timeout <= 600, 'invalid setup deadline')
        self.endpoint, self.token, self.kind = endpoint.rstrip('/'), token, credential_kind
        self.private_values = (token, *private_values)
        self.identity, self.authorize, self.retain = dict(identity), authorize, retain
        self.clock, self.deadline, self.attempted = clock, clock()+timeout, set()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def remaining(self):
        value = self.deadline-self.clock()
        require(value > 0, 'fixture setup deadline expired')
        return min(value, 30)

    def retention_ack(self, reference, value):
        require(isinstance(reference, dict) and isinstance(reference.get('key'), str)
                and 0 < len(reference['key']) <= 2048
                and all(ord(c) >= 32 and ord(c) != 127 for c in reference['key'])
                and reference.get('sha256') == canonical_digest(value),
                'invalid setup retention acknowledgement')
        try:
            text = json.dumps(reference, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise PackError('invalid setup retention acknowledgement') from None
        require(all(secret not in text and secret not in urllib.parse.unquote(text)
                    for secret in self.private_values), 'retention acknowledgement disclosed credential')
        return reference

    def request(self, step, method, path, *, body=None, content_type='application/json'):
        require(isinstance(step, str) and re.fullmatch(r'[a-z0-9-]{1,80}', step), 'invalid setup step identity')
        require(method in ('GET', 'POST', 'PUT') and isinstance(path, str) and path.startswith('/')
                and not path.startswith('//') and '\\' not in path and '#' not in path
                and all(ord(c) > 32 for c in path), 'invalid setup request')
        decoded_path = urllib.parse.unquote(path)
        require(all(secret not in path and secret not in decoded_path for secret in self.private_values),
                'setup request URL disclosed credential')
        require(body is None or type(body) is bytes and len(body) <= MAX_RESPONSE_BYTES, 'oversized setup request')
        require(method != 'GET' or body is None, 'GET setup request cannot carry a body')
        require(isinstance(content_type, str) and (content_type == 'application/json'
                or re.fullmatch(r'multipart/form-data; boundary=honua-[0-9a-f]{64}', content_type)),
                'unsupported setup content type')
        require(step not in self.attempted, 'setup step already attempted; observe without resending')
        self.remaining()
        self.authorize()
        intent = {'schemaVersion': 1, **self.identity, 'step': step, 'endpoint': self.endpoint,
                  'method': method, 'path': path, 'contentType': content_type,
                  'requestBodyDigest': digest_bytes(body or b'')}
        self.attempted.add(step)
        # Required to reject every pre-existing key, even identical intent bytes.
        intent_ref = self.retention_ack(self.retain('intent', step, intent), intent)
        self.authorize()
        timeout = self.remaining()
        headers = {'Content-Type': content_type, 'Accept': 'application/json'}
        headers['X-API-Key' if self.kind == 'api-key' else 'Authorization'] = (
            self.token if self.kind == 'api-key' else 'Bearer '+self.token)
        request = urllib.request.Request(self.endpoint+path, data=body, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                raw = bytearray()
                while True:
                    self.remaining()
                    chunk = response.read1(min(32768, MAX_RESPONSE_BYTES+1-len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    require(len(raw) <= MAX_RESPONSE_BYTES, 'setup response exceeds bound')
                for secret in self.private_values:
                    require(secret.encode() not in raw, 'setup response disclosed credential')
                    parse(bytes(raw), secret=secret)  # Also reject escaped values and ambiguous JSON.
                observation = response_evidence(response.code, bytes(raw))
        except PackError:
            raise
        except Exception:
            raise PackError('setup request failed; retain intent and reconcile without replay') from None
        record = {'schemaVersion': 1, **self.identity, 'step': step, 'intentDigest': canonical_digest(intent),
                  'intentReference': intent_ref, 'response': observation}
        response_ref = self.retention_ack(self.retain('response', step, record), record)
        return {'observation': observation, 'reference': response_ref}

    def json(self, step, method, path, body=None):
        encoded = None if body is None else json.dumps(body, allow_nan=False, separators=(',', ':')).encode()
        return self.request(step, method, path, body=encoded)

    def upload(self, step, raw, *, table, schema, srid):
        require(type(raw) is bytes and len(raw) <= MAX_RESPONSE_BYTES-4096, 'oversized fixture upload')
        require(all(isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value) for value in (table, schema))
                and type(srid) is int and srid > 0, 'invalid fixture upload target')
        boundary = 'honua-'+digest_bytes(raw)[7:]
        require(boundary.encode() not in raw, 'fixture upload contains multipart boundary')
        fields = {'TableName': table, 'TargetSchema': schema, 'SourceSrid': str(srid),
                  'TargetSrid': str(srid), 'OverwriteExisting': 'false'}
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                 for key, value in fields.items()]
        parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fixture.geojson"\r\nContent-Type: application/geo+json\r\n\r\n'.encode(),
                  raw, f'\r\n--{boundary}--\r\n'.encode()]
        return self.request(step, 'POST', '/api/v1/admin/import/upload', body=b''.join(parts),
                            content_type='multipart/form-data; boundary='+boundary)
