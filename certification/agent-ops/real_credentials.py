"""Create a separate trial-only operator credential without persisting its secret.

The caller must separately bind the runtime and authorize this disposable-stack
setup. This module alone does not grant access to a priced scenario.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass, field
from uuid import UUID
from datetime import datetime, timedelta, timezone
from pathlib import Path

from local_http import open_local, require_loopback


@dataclass(frozen=True)
class OperatorCredential:
    principal_id: str
    secret: str = field(repr=False)
    permissions: tuple[str, ...] = ()

    def public_receipt(self) -> dict:
        return {'principal_id': self.principal_id, 'permissions': list(self.permissions),
                'credential_sha256': hashlib.sha256(self.secret.encode()).hexdigest()}


def mint_operator(base_url: str, controller_key: str, *, project: str, compose_file: Path,
                  image_ref: str, authorize, retain, timeout: float = 10) -> OperatorCredential:
    require_loopback(base_url)
    if not controller_key or not callable(authorize) or not callable(retain):
        raise ValueError('controller credential, authorization and durable retention required')
    def save(kind: str, body: dict) -> None:
        encoded = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
        receipt = retain(kind, body)
        if (not isinstance(receipt, dict) or not isinstance(receipt.get('key'), str) or not receipt['key'] or
            receipt.get('sha256') != 'sha256:' + hashlib.sha256(encoded).hexdigest()):
            raise ValueError('credential event retention not acknowledged')

    from real_seed import _verify_runtime
    if '@sha256:' not in image_ref:
        raise ValueError('immutable trial image required')
    def authorize_bound():
        authorize()
        _verify_runtime(project, compose_file, image_ref, image_ref.split('@', 1)[1], base_url)
    authorize_bound()
    expiry = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    payload = json.dumps({'name': 'site-operator', 'permissions': ['admin'], 'expiresAt': expiry}, separators=(',', ':')).encode()
    save('intent', {'operation': 'create-operator', 'request_sha256': hashlib.sha256(payload).hexdigest()})
    authorize_bound()
    request = urllib.request.Request(base_url.rstrip('/') + '/api/v1/admin/api-keys/', data=payload,
                                     headers={'X-API-Key': controller_key, 'Content-Type': 'application/json'}, method='POST')
    try:
        with open_local(request, timeout=timeout) as response:
            raw = response.read(65537)
            status = response.status
        if status != 201 or len(raw) > 65536:
            raise ValueError('operator credential creation failed')
        document = json.loads(raw)
        data = document.get('data') if isinstance(document, dict) else None
        key = data.get('key') if isinstance(data, dict) else None
        record = data.get('apiKey') if isinstance(data, dict) else None
        if (not isinstance(key, str) or not 16 <= len(key) <= 8192 or
            not all(33 <= ord(char) <= 126 for char in key) or key == controller_key or
            not isinstance(record, dict) or record.get('name') != 'site-operator' or
            record.get('permissions') != ['admin']):
            raise ValueError('operator credential response incomplete')
        try:
            principal = str(UUID(record.get('id')))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError('operator principal identity invalid') from exc
        credential = OperatorCredential(principal, key, tuple(record['permissions']))
        save('response', credential.public_receipt())
        authorize_bound()
        # Prove the issued secret is accepted for a read without retaining the
        # one-time secret response or echoing the credential into any artifact.
        probe = urllib.request.Request(base_url.rstrip('/') + '/api/v1/admin/services/', headers={'X-API-Key': key})
        with open_local(probe, timeout=timeout) as response:
            probe_status = response.status
            response.read(65537)
        if probe_status != 200:
            raise ValueError('operator credential not accepted')
        authorize_bound()
        permissions_request = urllib.request.Request(
            base_url.rstrip('/') + f'/api/v1/admin/api-keys/{principal}/effective-permissions',
            headers={'X-API-Key': controller_key})
        with open_local(permissions_request, timeout=timeout) as response:
            permissions_raw, permissions_status = response.read(65537), response.status
        if permissions_status != 200 or len(permissions_raw) > 65536:
            raise ValueError('operator effective permissions unavailable')
        permissions_body = json.loads(permissions_raw)
        effective = permissions_body.get('data') if isinstance(permissions_body, dict) else None
        if (not isinstance(effective, dict) or effective.get('id') != principal or
            effective.get('permissions') != ['admin'] or effective.get('canAuthenticate') is not True):
            raise ValueError('operator effective permissions differ')
        save('verification', {'principal_id': principal, 'http_status': permissions_status,
                              'response_sha256': hashlib.sha256(permissions_raw).hexdigest()})
    except Exception as exc:
        save('failure', {'operation': 'create-operator', 'failure_type': type(exc).__name__})
        raise
    return credential
