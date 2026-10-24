"""Shared typed execution identity; no oracle, funding or provisioning inputs."""
import hashlib
import json
import re

from agent_e2e_errors import PackError


def require(value, reason):
    if not value:
        raise PackError(reason)


def identifier(value, name):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value), 'invalid native '+name)
    return value


def execution_id(value):
    require(isinstance(value, str) and re.fullmatch(r'execution-[0-9a-f]{32}', value), 'invalid original native execution ID')
    return value


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value), 'invalid retained native digest')
    return value


def canonical_digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    return 'sha256:'+hashlib.sha256(raw).hexdigest()


def validate_worker_bound(reply, original):
    """A worker-local read reference is metadata, never native authority."""
    require(isinstance(reply, dict) and isinstance(original, dict) and
            original.get('protocol') == 'honua.installed-worker-bind/v1' and
            reply.get('protocol') == 'honua.installed-worker-bound/v1' and
            set(reply) == set(original) | {'nativeRead'} and
            all(reply.get(k) == v for k, v in original.items() if k != 'protocol') and
            isinstance(reply.get('nativeRead'), dict) and set(reply['nativeRead']) == {'key', 'sha256'},
            'worker control reply belongs to another original execution')
    digest(reply['nativeRead']['sha256'])
    require(isinstance(reply['nativeRead']['key'], str) and re.fullmatch(r'bind-read-[0-9]{1,30}', reply['nativeRead']['key']),
            'worker local read reference is malformed')
    return reply
