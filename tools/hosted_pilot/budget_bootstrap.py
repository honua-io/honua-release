#!/usr/bin/env python3
"""Seal reviewed nonzero accounting under the actual campaign producer identity."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_bootstrap import validate_import
from agent_e2e_gate import component_binding
from agent_e2e_pack import canonical_digest, digest_bytes, load_json
from controller_archive import canonical, portable, require
from controller_handoff import ROOT, git


def reviewed(reference):
    require(set(reference) == {'path', 'sha256'}, 'reviewed budget source reference missing')
    portable(reference['path'])
    require(reference['path'].startswith(('certification/agent-e2e/', 'certification/hosted-pilot/history/')) and
            reference['path'].endswith('.json'), 'budget source escaped reviewed namespace')
    path = ROOT/reference['path']
    require(path.is_file() and not path.is_symlink() and digest_bytes(path.read_bytes()) == reference['sha256'], 'reviewed budget source changed')
    git('ls-files', '--error-unmatch', '--', reference['path'])
    return load_json(path)


def validate_history(history, ledger):
    require(history['schema'] == 'honua.hosted-pilot-budget-history/v1' and history['wholeAccountZeroSpend'] is False and
            not any(row['producerRuns'] for row in history['repositories'].values()) and
            not any(row['pilotEvents'] for row in history['cloudTrail'].values()),
            'pilot producer history requires reconciliation before initial import')
    # Gross account usage is intentionally a conservative unresolved attribution
    # hold; credits never reduce it. The extra dollar covers observed diagnostic
    # fees without pretending that billing has settled or stopped accruing.
    minimum = Decimal(history['billing']['grossUsageUsd']).to_integral_value(rounding=ROUND_CEILING)+1
    rows = [row for row in ledger['operations'] if row['id'] == 'initialization-history-usage']
    require(len(rows) == 1 and rows[0]['purpose'] == 'telemetry' and rows[0]['kind'] == 'infrastructure' and
            rows[0]['state'] == 'reserved' and rows[0]['actualUsd'] is None and
            rows[0]['basisDigest'] == canonical_digest(history) and Decimal(rows[0]['amountUsd']) >= minimum,
            'initial import must retain nonzero gross usage and diagnostic hold, never settle unknown charges as zero')


def produce(configuration, lock, provenance, lock_digest):
    require(set(configuration) == {'schemaVersion', 'candidateKey', 'history', 'ledger', 'expiresAt', 'liveLiabilityReferences'} and
            configuration['schemaVersion'] == 1 and configuration['candidateKey'] == component_binding(lock) and
            provenance['qualificationLockDigest'] == lock_digest and
            datetime.fromisoformat(configuration['expiresAt'].replace('Z', '+00:00')) > datetime.now(timezone.utc),
            'bootstrap source must bind its authenticated frozen candidate and live reviewed allowance')
    history = reviewed(configuration['history'])
    ledger = configuration['ledger']
    source = {'repository': 'honua-io/honua-release', 'workflow': '.github/workflows/agent-e2e-campaign.yml',
              'sourceSha': os.environ['GITHUB_SHA'], 'runId': int(os.environ['GITHUB_RUN_ID']), 'runAttempt': int(os.environ['GITHUB_RUN_ATTEMPT'])}
    artifact = {'schemaVersion': 1, 'candidateKey': configuration['candidateKey'], 'ledger': ledger, 'source': source}
    validate_import({'artifactDigest': canonical_digest(artifact), 'artifact': artifact}, configuration['candidateKey'])
    require(ledger['schemaVersion'] == 3, 'pilot bootstrap requires the original-history-preserving named owner amendment')
    validate_history(history, ledger)
    require(isinstance(configuration['liveLiabilityReferences'], list) and configuration['liveLiabilityReferences'],
            'existing legacy/live resources and cleanup liabilities must be explicitly reviewed and retained')
    # operationIds identify the operation group (row.operationId), whereas
    # row.id identifies one infrastructure/cleanup reservation in that group.
    for reference in configuration['liveLiabilityReferences']:
        liability = reviewed(reference)
        require(set(liability) == {'schema', 'candidateKey', 'operationIds', 'observation'} and
                liability['schema'] == 'honua.hosted-pilot-retained-liability/v1' and liability['candidateKey'] == configuration['candidateKey'] and
                isinstance(liability['operationIds'], list) and liability['operationIds'] and
                all(any(row['operationId'] == operation and row['state'] == 'reserved' and row['actualUsd'] is None
                        for row in ledger['operations']) for operation in liability['operationIds']),
                'live or legacy liability cannot disappear from the imported original ledger')
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('configuration', 'lock', 'provenance', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    args = parser.parse_args()
    require(os.environ.get('GITHUB_REF') == 'refs/heads/trunk' and
            os.environ.get('GITHUB_REPOSITORY') == 'honua-io/honua-release' and
            os.environ.get('GITHUB_WORKFLOW_REF') == 'honua-io/honua-release/.github/workflows/agent-e2e-campaign.yml@refs/heads/trunk',
            'bootstrap accounting must be sealed by the actual protected campaign producer')
    require(args.configuration.resolve() == ROOT/'certification/agent-e2e/bootstrap.json', 'use the fixed reviewed bootstrap source')
    git('ls-files', '--error-unmatch', '--', str(args.configuration))
    artifact = produce(load_json(args.configuration), load_json(args.lock), load_json(args.provenance), digest_bytes(args.lock.read_bytes()))
    args.output.write_bytes(canonical(artifact)+b'\n')
    print(json.dumps({'status': 'sealed', 'artifactDigest': digest_bytes(args.output.read_bytes()), 'candidateKey': artifact['candidateKey'], 'qualification': False}))


if __name__ == '__main__':
    main()
