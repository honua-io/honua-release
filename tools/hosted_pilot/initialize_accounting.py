#!/usr/bin/env python3
"""Create one retained accounting generation from an authenticated nonzero import.

The protected workflow performs this write only after infrastructure staging and
source review. Existing/missing-head historical state can never be overwritten.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_bootstrap import verify_import
from agent_e2e_pack import canonical_digest, load_json
from agent_e2e_state import DurableExecution, open_store, preserve_history
from budget_bootstrap import reviewed, validate_history
from controller_archive import canonical, require
from controller_handoff import ROOT, git
from fixture_handoff import sdk


def initialize(config, artifact, ledger_path, *, verify=verify_import, open_state=open_store, execution=DurableExecution, sts=None, recover=False):
    require(type(recover) is bool, 'accounting recovery must be an explicit workflow mode')
    require(set(config) == {'schemaVersion', 'candidateKey', 'state', 'initializerRoleArn', 'bootstrap', 'history', 'expiresAt'} and
            config['schemaVersion'] == 1 and re.fullmatch(r'sha256:[a-f0-9]{64}', config['candidateKey']) and
            datetime.fromisoformat(config['expiresAt'].replace('Z', '+00:00')) > datetime.now(timezone.utc),
            'reviewed initialization binding missing or expired')
    candidate = config['candidateKey']
    role = config['initializerRoleArn']
    require(re.fullmatch(r'arn:aws:iam::585192672263:role/honua-pilot-[a-z0-9-]+-accounting-initializer', role), 'dedicated isolated initialization role required')
    identity = (sts or sdk('sts', 'us-east-1')).get_caller_identity()
    require(identity['Account'] == '585192672263' and identity['Arn'].startswith(role.replace(':iam::', ':sts::').replace(':role/', ':assumed-role/')+'/'),
            'actual AWS caller is not the reviewed accounting initializer')
    name = 'honua-agent-'+candidate[7:31]
    require(config['state'] == {'region': 'us-east-1', 'bucket': name+'-585192672263', 'table': name+'-state'},
            'initializer cannot substitute a fresh accounting namespace')
    original = config['bootstrap']
    require(set(original) == {'artifactDigest', 'sourceSha', 'runId', 'runAttempt'}, 'original successful bootstrap producer binding required')
    imported = verify(artifact, original['artifactDigest'], candidate, original['sourceSha'], original['runId'], original['runAttempt'])
    history = reviewed(config['history'])
    if not recover:
        validate_history(history, imported['artifact']['ledger'])
    require(not Path(ledger_path).exists(), 'initialization scratch ledger already exists; reconcile rather than replace')
    store = open_state(config['state'], candidate)
    # Recovery can only open an authenticated existing generation. It never
    # supplies bootstrap to the constructor or falls back to initialization.
    durable = execution(store, ledger_path, initialize=not recover, bootstrap=None if recover else imported)
    try:
        require(durable.state.get('bootstrapImport') == imported, 'state differs from authenticated original bootstrap')
        if recover:
            # The original report is retained history, not a new allowance. Its
            # age cannot authorize fresh initialization, nor erase later spend.
            validate_history(history, imported['artifact']['ledger'], require_fresh=False)
            preserve_history({'ledger': imported['artifact']['ledger']}, {'ledger': durable.state['ledger']})
        else:
            require(durable.state['ledger'] == imported['artifact']['ledger'], 'created state differs from authenticated original liabilities')
        generation = store.generation
        require(isinstance(generation, dict) and set(generation) == {'key', 'version', 'digest'} and generation['version'] not in ('', 'null'),
                'actual immutable generation receipt missing')
        return {'schema': 'honua.hosted-pilot-accounting-initialization/v1', 'candidateKey': candidate, 'state': config['state'],
            'generation': generation, 'ledgerDigest': canonical_digest(durable.state['ledger']),
            'bootstrapLedgerDigest': canonical_digest(imported['artifact']['ledger']),
            'bootstrapArtifactDigest': original['artifactDigest'], 'qualification': False,
            'mode': 'recovered' if recover else 'created',
            'modelExecution': 'not-executed-by-recovery' if recover else 'not-run'}
    finally:
        durable.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('configuration', 'artifact', 'ledger', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    parser.add_argument('--recover', action='store_true', help='Reconstruct a receipt from the matching existing generation; never initialize')
    args = parser.parse_args()
    require(os.environ.get('GITHUB_REF') == 'refs/heads/trunk' and os.environ.get('GITHUB_REPOSITORY') == 'honua-io/honua-release' and
            os.environ.get('GITHUB_WORKFLOW_REF') == 'honua-io/honua-release/.github/workflows/hosted-pilot-initialize.yml@refs/heads/trunk',
            'accounting initialization requires the checked protected workflow')
    require(args.configuration.resolve() == ROOT/'certification/agent-e2e/initialize.json', 'use the fixed reviewed initialization source')
    git('ls-files', '--error-unmatch', '--', str(args.configuration))
    result = initialize(load_json(args.configuration), args.artifact, args.ledger, recover=args.recover)
    args.output.write_bytes(canonical(result)+b'\n')
    print('Original nonzero accounting imported. Review the actual immutable generation receipt before enabling dispatch.')


if __name__ == '__main__':
    main()
