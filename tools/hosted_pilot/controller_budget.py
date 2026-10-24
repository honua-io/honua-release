"""Read an existing authenticated allowance; atomically consume dispatch capacity."""
from __future__ import annotations
from decimal import Decimal
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_budget import validate
from controller_archive import canonical, digest, require


def read_version(s3, bucket, key, version, *, limit=8 * 1024 * 1024):
    require(version and version != 'null', 'controller custody requires an immutable S3 version')
    response = s3.get_object(Bucket=bucket, Key=key, VersionId=version)
    raw = response['Body'].read(limit + 1)
    require(len(raw) <= limit and response.get('VersionId') == version, 'controller custody object version/size differs')
    return raw


def read_allowance(config, s3, db):
    """No lease acquisition, initialization, new reservation or ledger mutation."""
    state, candidate = config['state'], config['candidateKey']
    row = db.get_item(TableName=state['table'], Key={'candidateKey': {'S': candidate}}, ConsistentRead=True).get('Item', {})
    require(row.get('generationDigest', {}).get('S') == config['generationDigest'], 'existing controller accounting generation is missing or differs; never initialize a dispatch allowance')
    prefix = 'agent-e2e/' + candidate[7:] + '/'
    require(row.get('generationKey', {}).get('S') == prefix + 'initialization.json' and row.get('snapshotKey', {}).get('S', '').startswith(prefix), 'controller accounting custody path differs')
    marker_raw = read_version(s3, state['bucket'], row['generationKey']['S'], row['generationVersion']['S'])
    marker = json.loads(marker_raw)
    require(digest(marker_raw) == config['generationDigest'] and marker.get('candidateKey') == candidate, 'controller initialization provenance differs')
    snapshot_raw = read_version(s3, state['bucket'], row['snapshotKey']['S'], row['snapshotVersion']['S'])
    snapshot = json.loads(snapshot_raw)
    require(digest(snapshot_raw) == row['snapshotDigest']['S'] and snapshot.get('generationDigest') == config['generationDigest'] and snapshot.get('candidateKey') == candidate and snapshot.get('version') == int(row['stateVersion']['N']), 'controller accounting snapshot binding differs')
    ledger = snapshot['payload']['ledger']
    validate(ledger)
    require(ledger['candidateKey'] == candidate, 'controller reservation belongs to another candidate')
    reservations = [item for item in ledger.get('operations', []) if item['id'] == config['reservationId']]
    require(len(reservations) == 1, 'existing controller shared-operation reservation missing')
    reservation = reservations[0]
    require(reservation['operationId'] == config['operationId'] and reservation['purpose'] == 'shared-environment' and reservation['kind'] == 'infrastructure' and reservation['state'] == 'reserved' and reservation['actualUsd'] is None and reservation['basisDigest'] == config['basisDigest'], 'controller operation is not a retained reserved shared-environment liability')
    ceiling = Decimal(config['perDispatchCeilingUsd'])
    require(ceiling.is_finite() and ceiling > 0 and type(config['maximumDispatches']) is int and 1 <= config['maximumDispatches'] <= 256 and ceiling * config['maximumDispatches'] <= Decimal(reservation['amountUsd']), 'controller dispatch capacity exceeds its original reservation')
    # Backend overhead and dispatch capacity cannot spend the same original row.
    for allocation in snapshot['payload'].get('nativeOverheadAllocations', {}).values():
        require(config['reservationId'] not in canonical(allocation).decode(), 'controller reservation already allocated to backend overhead')
    return row


def consume(config, *, db, row, invocation, binding_digest, execution_arn):
    """Claim once under the observed native state head; lost acknowledgements reuse it."""
    claim_key = {'claimKey': {'S': 'dispatch:' + invocation}}
    existing = db.get_item(TableName=config['claimTable'], Key=claim_key, ConsistentRead=True).get('Item')
    if existing:
        require(existing.get('bindingDigest', {}).get('S') == binding_digest and existing.get('executionArn', {}).get('S') == execution_arn, 'immutable controller invocation already belongs to different inputs')
        return False
    counter = digest(canonical({key: config[key] for key in ('candidateKey', 'generationDigest', 'reservationId')}))
    config_digest = digest(canonical(config))
    db.transact_write_items(TransactItems=[
        {'ConditionCheck': {'TableName': config['state']['table'], 'Key': {'candidateKey': {'S': config['candidateKey']}},
            'ConditionExpression': 'generationDigest = :generation AND stateVersion = :version AND snapshotDigest = :snapshot AND snapshotVersion = :objectVersion',
            'ExpressionAttributeValues': {':generation': row['generationDigest'], ':version': row['stateVersion'], ':snapshot': row['snapshotDigest'], ':objectVersion': row['snapshotVersion']}}},
        {'Update': {'TableName': config['claimTable'], 'Key': {'claimKey': {'S': 'capacity:' + counter}},
            'ConditionExpression': '(attribute_not_exists(configDigest) OR configDigest = :config) AND (attribute_not_exists(used) OR used < :maximum)',
            'UpdateExpression': 'SET configDigest = :config ADD used :one',
            'ExpressionAttributeValues': {':config': {'S': config_digest}, ':maximum': {'N': str(config['maximumDispatches'])}, ':one': {'N': '1'}}}},
        {'Put': {'TableName': config['claimTable'], 'Item': claim_key | {'bindingDigest': {'S': binding_digest}, 'executionArn': {'S': execution_arn},
            'candidateKey': {'S': config['candidateKey']}, 'generationDigest': {'S': config['generationDigest']},
            'reservationId': {'S': config['reservationId']}, 'basisDigest': {'S': config['basisDigest']}},
            'ConditionExpression': 'attribute_not_exists(claimKey)'}}])
    return True
