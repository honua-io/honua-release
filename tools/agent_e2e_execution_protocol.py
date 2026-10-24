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
