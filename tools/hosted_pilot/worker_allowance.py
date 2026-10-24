"""Read original per-worker provisioning liabilities; never initialize or mutate state."""
from __future__ import annotations
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_budget import validate
from controller_archive import canonical


def basis(config):
    # Immutable attempt/resource/time bindings, independently computable before
    # the authenticated owner reserves its operation. No circular ledger digest.
    return 'sha256:'+hashlib.sha256(canonical({key: value for key, value in config.items() if key != 'accounting'})).hexdigest()


def verify(config, work, *, aws, require, include_payload=False):
    accounting = config['accounting']
    state = accounting['state']
    candidate = config['candidate_digest']
    require(set(accounting) == {'state', 'generationDigest', 'bootstrapArtifactDigest', 'operation', 'maximumUsd'} and
            set(state) == {'region', 'bucket', 'table'} and state['region'] == config['region'] and
            re.fullmatch(r'sha256:[a-f0-9]{64}', candidate), 'worker original accounting namespace missing')
    name = 'honua-agent-'+candidate[7:31]
    require(re.fullmatch(r'[0-9]{12}', config['account_id']) and state == {'region': config['region'],
            'bucket': name+'-'+config['account_id'], 'table': name+'-state'},
            'worker cannot substitute another accounting namespace')
    row = aws('dynamodb', 'get-item', '--region', state['region'], '--table-name', state['table'], '--key',
              json.dumps({'candidateKey': {'S': candidate}}), '--consistent-read').get('Item', {})
    require(row.get('generationDigest', {}).get('S') == accounting['generationDigest'], 'worker original generation missing or changed; never initialize another allowance')
    prefix = 'agent-e2e/'+candidate[7:]+'/'
    require(row.get('generationKey', {}).get('S') == prefix+'initialization.json' and row.get('snapshotKey', {}).get('S', '').startswith(prefix), 'worker accounting custody differs')
    def read(key, version, label):
        require(isinstance(version, str) and version not in ('', 'null'), 'worker accounting requires immutable object versions')
        destination = Path(work)/('worker-accounting-'+label+'.json')
        metadata = aws('s3api', 'get-object', '--region', state['region'], '--bucket', state['bucket'], '--key', key, '--version-id', version, str(destination))
        require(metadata.get('VersionId') == version, 'worker accounting object immutable version differs')
        require(destination.stat().st_size <= 8*1024*1024, 'worker original accounting exceeds bounded size')
        raw = destination.read_bytes()
        return json.loads(raw), 'sha256:'+hashlib.sha256(raw).hexdigest()
    marker, marker_digest = read(row['generationKey']['S'], row['generationVersion']['S'], 'generation')
    require(marker_digest == accounting['generationDigest'] and marker.get('candidateKey') == candidate, 'worker initialization provenance differs')
    snapshot, snapshot_digest = read(row['snapshotKey']['S'], row['snapshotVersion']['S'], 'snapshot')
    require(snapshot_digest == row['snapshotDigest']['S'] and snapshot['candidateKey'] == candidate and
            snapshot['generationDigest'] == accounting['generationDigest'] and snapshot['version'] == int(row['stateVersion']['N']), 'worker original snapshot differs')
    payload = snapshot['payload']
    ledger = payload['ledger']
    validate(ledger)
    require(ledger['candidateKey'] == candidate and payload.get('bootstrapImport', {}).get('artifactDigest') == accounting['bootstrapArtifactDigest'],
            'worker configuration cannot substitute original authenticated spending history')
    if config.get('installed_slot') is not None:
        require(config['claim_table_name'] == name+'-claims', 'installed worker must use the original candidate claim table')
        from agent_e2e_interactive_budget import validate_retained_funding
        from installed_worker_receipt import pending_binding
        validate_retained_funding(payload)
        require(payload['ledger'].get('interactiveAccounting', {}).get('originalExpiresEpoch') == config['installed_slot']['original_funding_expires_epoch'],
                'installed worker cannot renew original authenticated funding expiry')
        pending_binding(payload, config)
    operation = accounting['operation']
    require(set(operation) == {'operationId', 'liabilityBasisDigest', 'reservationIds'} and
            operation['operationId'] == 'worker-'+config['attempt_id'] and operation['liabilityBasisDigest'] == basis(config) and
            set(operation['reservationIds']) == {'infrastructure', 'cleanup'} and len(set(operation['reservationIds'].values())) == 2,
            'worker needs distinct original operation/basis for its fixed attempt, image, MCP version and absolute lifetime')
    require(set(accounting['maximumUsd']) == {'infrastructure', 'cleanup'}, 'worker provisioning cost ceilings missing')
    for kind, reservation in operation['reservationIds'].items():
        maximum = Decimal(accounting['maximumUsd'][kind])
        require(maximum.is_finite() and maximum > 0, 'worker provisioning liabilities must be positive finite bounds')
        rows = [entry for entry in ledger.get('operations', []) if entry['id'] == reservation]
        require(len(rows) == 1 and rows[0]['operationId'] == operation['operationId'] and rows[0]['purpose'] == 'shared-environment' and
                rows[0]['kind'] == kind and rows[0]['basisDigest'] == operation['liabilityBasisDigest'] and rows[0]['state'] == 'reserved' and
                rows[0]['actualUsd'] is None and maximum <= Decimal(rows[0]['amountUsd']), 'worker provisioning is not covered by original retained infrastructure/cleanup liability')
    result = {'candidateKey': candidate, 'generationDigest': accounting['generationDigest'], 'snapshotDigest': snapshot_digest,
            'operationId': operation['operationId'], 'basisDigest': operation['liabilityBasisDigest']}
    if include_payload:
        result['payload'] = payload
    return result
