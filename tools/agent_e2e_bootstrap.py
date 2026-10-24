"""Authenticate bootstrap accounting before importing it into new remote state.

The producer must retain candidate-wide reservations before Terraform apply and
seal all resulting charges/unknown liabilities in this artifact. This module
does not provision infrastructure, infer a price, or authenticate a self-declared
digest. Expected source/run/digest arguments come from trusted controller config.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re

from agent_e2e_budget import validate as validate_budget
from agent_e2e_pack import PackError, canonical_digest, digest_bytes
from verify_agent_e2e_campaign import REPOSITORY, WORKFLOW, authenticate, call


def validate_import(value, candidate):
    if not isinstance(value, dict) or set(value) != {'artifactDigest', 'artifact'}:
        raise PackError('invalid bootstrap import binding')
    if not isinstance(value['artifactDigest'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['artifactDigest']):
        raise PackError('invalid bootstrap artifact digest')
    artifact = value['artifact']
    if (not isinstance(artifact, dict) or set(artifact) != {'schemaVersion', 'candidateKey', 'ledger', 'source'}
            or type(artifact['schemaVersion']) is not int or artifact['schemaVersion'] != 1
            or artifact['candidateKey'] != candidate):
        raise PackError('bootstrap artifact belongs to another candidate or format')
    source = artifact['source']
    if (not isinstance(source, dict) or set(source) != {'repository', 'workflow', 'sourceSha', 'runId', 'runAttempt'}
            or source['repository'] != REPOSITORY or source['workflow'] != WORKFLOW
            or not isinstance(source['sourceSha'], str) or not re.fullmatch(r'[0-9a-f]{40}', source['sourceSha'])
            or type(source['runId']) is not int or source['runId'] <= 0
            or type(source['runAttempt']) is not int or source['runAttempt'] <= 0):
        raise PackError('invalid bootstrap producer identity')
    ledger = artifact['ledger']
    validate_budget(ledger)
    if any(field in ledger for field in ('interactiveAccounting', 'interactiveEntries', 'interactivePaidAdmissions')):
        raise PackError('bootstrap import cannot reinitialize interactive execution history')
    if (ledger['candidateKey'] != candidate or ledger['schemaVersion'] not in (2, 3) or ledger['entries']
            or not ledger['operations']):
        raise PackError('bootstrap must preserve operation accounting without claiming model attempts')
    if ledger['schemaVersion'] == 3 and (ledger['policyAmendment']['priorLedger']['schemaVersion'] != 2 or
            ledger.get('paidAdmissions')):
        raise PackError('bootstrap amendment requires actual prior version-2 operation history without paid model attempts')
    bootstrap_kinds = {row['kind'] for row in ledger['operations'] if row['purpose'] == 'bootstrap'}
    if bootstrap_kinds != {'infrastructure', 'cleanup'}:
        raise PackError('bootstrap requires retained infrastructure and cleanup reservations')
    canonical_digest(value)  # Reject nonfinite/nonserializable metadata before any write.
    return copy.deepcopy(ledger)


def verify_import(path, expected_digest, candidate, source_sha, run_id, run_attempt, *, invoke=call):
    path = Path(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise PackError('bootstrap artifact exceeds size bound')
    raw = path.read_bytes()
    if digest_bytes(raw) != expected_digest:
        raise PackError('bootstrap artifact differs from trusted digest')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise PackError('duplicate bootstrap JSON field')
            result[key] = item
        return result
    value = {'artifactDigest':expected_digest, 'artifact':json.loads(raw, object_pairs_hook=unique)}
    validate_import(value, candidate)
    expected_source = {'repository':REPOSITORY, 'workflow':WORKFLOW, 'sourceSha':source_sha,
                       'runId':run_id, 'runAttempt':run_attempt}
    if value['artifact']['source'] != expected_source:
        raise PackError('bootstrap producer differs from trusted source/run')
    metadata = invoke(['gh', 'api', f'repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}'])
    if (metadata['id'] != run_id or metadata['run_attempt'] != run_attempt or metadata['head_sha'] != source_sha
            or metadata['head_branch'] != 'trunk' or metadata['path'] != WORKFLOW
            or metadata['status'] != 'completed' or metadata['conclusion'] != 'success'
            or metadata['event'] != 'workflow_dispatch'):
        raise PackError('bootstrap is not an exact successful trusted producer run')
    authenticate(path, source_sha, run_id, run_attempt, invoke=invoke)
    if path.read_bytes() != raw:
        raise PackError('bootstrap artifact changed during authentication')
    return value
