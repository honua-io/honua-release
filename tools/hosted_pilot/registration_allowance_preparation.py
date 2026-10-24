"""Checked controller preparation for a separate registrar admission and quote.

The controller needs no registrar/root credential. An independently retained
preparation overhead operation covers this controller task; the two newly
reserved rows cover only the later one-shot registrar and its cleanup.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_e2e_pack import canonical_digest, load_json
from controller_archive import canonical, digest, require
from controller_handoff import ROOT, bundle_identity, git
from registration_allowance import admission_key, retain_admission_and_reservations
from worker_mcp_configuration import authorize


ACTION = 'prepare-runtime-registration-budget'


def checked_source(path, sha256):
    require(path.startswith('certification/hosted-pilot/quotes/') and path.endswith('.json') and
            '..' not in path.split('/'), 'registrar quote source escaped its reviewed namespace')
    selected = ROOT/path
    require(selected.is_file() and not selected.is_symlink() and digest(selected.read_bytes()) == sha256,
            'registrar quote source bytes differ')
    git('ls-files', '--error-unmatch', '--', path)
    return selected


def provision(durable, configuration, *, source_reader=checked_source):
    require(isinstance(configuration, dict) and set(configuration) == {'operation', 'registration'},
            'registrar funding preparation needs separate overhead and new registration configuration')
    overhead, registration = configuration['operation'], configuration['registration']
    require(overhead['operationId'] != registration['operation']['operationId'] and
            not set(overhead['reservationIds'].values()) & set(registration['operation']['reservationIds'].values()),
            'registrar admission cannot reuse controller preparation overhead rows')
    guard = lambda: authorize(durable, overhead)
    original = retain_admission_and_reservations(registration, durable, source_reader=source_reader, guard=guard)
    guard()
    key = admission_key(registration)
    record = durable.state['scenarioOperations'][key]
    require(record['value'] == original, 'registrar admission result lost its original protected custody')
    return {'key':key, 'sha256':canonical_digest(record), 'admission':original,
            'maximumUsd':registration['maximumUsd'], 'activation':False, 'qualification':False}


def execute_preparation(bundle, output):
    from agent_e2e_state import DurableExecution, open_store
    from prepare import source_configuration
    document = load_json(Path(bundle)/'preparation.json')
    manifest, identity, state = bundle_identity(bundle, operation=document['operation'], case_id=None, target=None, shard=0, shards=6)
    outer = source_configuration(manifest['configuration'])
    require(identity['operation'] == outer['action'] == ACTION and outer['candidateKey'] == identity['candidateKey'] and
            outer['state'] == state and set(outer['configuration']) == {'operation', 'registration'} and
            outer['configuration']['registration']['candidateKey'] == outer['candidateKey'] and
            outer['configuration']['registration']['state'] == state and
            outer['configuration']['registration']['bootstrapArtifactDigest'] == outer['bootstrapArtifactDigest'],
            'registrar budget preparation differs from exact original source and accounting')
    require(datetime.fromisoformat(manifest['expiresAt'].replace('Z','+00:00')) > datetime.now(timezone.utc),
            'registrar funding preparation manifest expired')
    durable = DurableExecution(open_store(state, outer['candidateKey']), Path(bundle)/'budget-ledger.json')
    try:
        require(durable.state.get('bootstrapImport', {}).get('artifactDigest') == outer['bootstrapArtifactDigest'],
                'registrar funding cannot replace authenticated original bootstrap history')
        result = provision(durable, outer['configuration'])
        answer = {'schema':'honua.private-controller-preparation-result/v1', 'operation':ACTION,
            'candidateKey':outer['candidateKey'], 'sourceConfiguration':manifest['configuration'],
            'producerTaskArn':os.environ['PILOT_CONTROLLER_TASK_ARN'], 'result':result,
            'requiresSourceReviewBeforeFreeze':True, 'qualification':False}
        Path(output).write_bytes(canonical(answer)+b'\n')
    finally:
        durable.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['execute'])
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    try:
        arguments = parser.parse_args()
        execute_preparation(arguments.bundle, arguments.output)
    except Exception:
        print('private registrar funding refused; retain original admission, rows and liabilities', file=sys.stderr)
        sys.exit(1)
