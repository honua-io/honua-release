"""Signed, run-bound proposal evidence for a separate approval principal.

The signing key belongs to the proxy controller, never the model. The verifier's
public key and run binding must arrive through the controller, not the artifact.
"""
from __future__ import annotations

import base64
import json
import re
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

_TOOLS = {
    'O01': {'honua_propose_platform_release_convergence'},
    'O02': set(),
    'O05': set(),
    'O08': {'honua_propose_platform_release_convergence', 'honua_propose_finding', 'honua_propose_rollback'},
}
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z')


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _validate(ledger, binding):
    if not isinstance(ledger, dict) or set(ledger) != {'version', 'binding', 'proposals'}:
        raise ValueError('invalid ledger')
    if ledger['version'] != 1 or ledger['binding'] != binding or not isinstance(binding, dict):
        raise ValueError('ledger binding')
    if set(binding) != {'run_id', 'attempt', 'cell', 'scenario'} or any(
            not isinstance(v, str) or not v for v in binding.values()):
        raise ValueError('invalid run binding')
    allowed = _TOOLS.get(binding['scenario'])
    if allowed is None or not isinstance(ledger['proposals'], list) or len(ledger['proposals']) > 80:
        raise ValueError('invalid proposals')
    seen = set()
    for row in ledger['proposals']:
        if (not isinstance(row, dict) or set(row) != {'tool', 'proposalId'} or
                row['tool'] not in allowed or not isinstance(row['proposalId'], str) or
                not _ID.fullmatch(row['proposalId']) or row['proposalId'] in seen):
            raise ValueError('unapproved proposal')
        seen.add(row['proposalId'])


def seal(private_key: Ed25519PrivateKey, binding: dict, records: list[dict]) -> dict:
    ledger = {'version': 1, 'binding': binding, 'proposals': records}
    _validate(ledger, binding)
    return {'ledger': ledger, 'signature': base64.b64encode(private_key.sign(_bytes(ledger))).decode('ascii')}


def verify(public_key: Ed25519PublicKey, envelope: dict, binding: dict) -> list[dict]:
    if not isinstance(envelope, dict) or set(envelope) != {'ledger', 'signature'}:
        raise ValueError('invalid envelope')
    _validate(envelope['ledger'], binding)
    try:
        signature = base64.b64decode(envelope['signature'], validate=True)
        public_key.verify(signature, _bytes(envelope['ledger']))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise ValueError('invalid ledger signature') from exc
    return list(envelope['ledger']['proposals'])


def approve_ledger(public_key, envelope, binding, approver, policy):
    """Called by the approver principal, with its verification key provisioned independently."""
    if not callable(policy):
        raise ValueError("independent proposal policy required")
    approved = []
    for item in verify(public_key, envelope, binding):
        if policy(item) is not True:
            approved.append({**item, "status": "refused", "error": "policy"})
            continue
        try:
            response = approver.approve(item['proposalId'])
            approved.append({**item, 'status': 'approved', 'executionOperationId': response.get('executionOperationId')})
        except Exception as exc:
            # Retain the failed action but never disclose credential-bearing exception text.
            approved.append({**item, 'status': 'undetermined', 'error': type(exc).__name__})
            break
    return approved
