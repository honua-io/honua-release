"""Read one stable authenticated existing generation without acquiring a lease."""
from __future__ import annotations

import json
import re

from agent_e2e_budget import validate
from agent_e2e_pack import digest_bytes
from controller_archive import require

MAX_STATE_BYTES = 8*1024*1024
HEAD = ('stateVersion', 'snapshotKey', 'snapshotVersion', 'snapshotDigest',
        'generationKey', 'generationVersion', 'generationDigest')


def observe(store):
    def head():
        row = store.db.get_item(TableName=store.table, Key=store.key, ConsistentRead=True).get('Item', {})
        require(all(field in row for field in HEAD), 'existing accounting head is absent or incomplete; recovery cannot initialize')
        require(all(isinstance(row[field], dict) and set(row[field]) == {'S'} and
                    isinstance(row[field]['S'], str) and row[field]['S'] for field in HEAD if field != 'stateVersion') and
                isinstance(row['stateVersion'], dict) and set(row['stateVersion']) == {'N'} and
                isinstance(row['stateVersion']['N'], str) and re.fullmatch(r'[1-9][0-9]*', row['stateVersion']['N']),
                'accounting head has invalid immutable version fields')
        return {field: row[field] for field in HEAD}

    first = head()
    prefix = 'agent-e2e/'+store.candidate[7:]+'/'
    generation = {name: first[field]['S'] for name, field in
                  (('key','generationKey'),('version','generationVersion'),('digest','generationDigest'))}
    require(generation['key'] == prefix+'initialization.json' and
            first['snapshotKey']['S'].startswith(prefix) and first['snapshotKey']['S'] != generation['key'],
            'accounting recovery generation/snapshot is outside the original candidate')

    def read(key, version, digest):
        require(isinstance(version, str) and version not in ('','null') and
                isinstance(digest, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', digest),
                'accounting recovery requires immutable version and digest custody')
        response = store.s3.get_object(Bucket=store.bucket, Key=key, VersionId=version)
        body = response['Body']
        try:
            require(response.get('VersionId') == version and type(response.get('ContentLength')) is int and
                    0 < response['ContentLength'] <= MAX_STATE_BYTES, 'accounting object version or bounded length differs')
            raw = body.read(MAX_STATE_BYTES+1)
            require(len(raw) == response['ContentLength'] and digest_bytes(raw) == digest,
                    'accounting recovery object digest or length differs')
            return json.loads(raw)
        finally:
            body.close()

    marker = read(generation['key'], generation['version'], generation['digest'])
    require(isinstance(marker, dict) and type(marker.get('schemaVersion')) is int and marker['schemaVersion'] == 1 and
            marker.get('candidateKey') == store.candidate,
            'accounting generation marker belongs to another candidate')
    snapshot = read(first['snapshotKey']['S'], first['snapshotVersion']['S'], first['snapshotDigest']['S'])
    require(isinstance(snapshot, dict) and snapshot.get('candidateKey') == store.candidate and
            type(snapshot.get('version')) is int and snapshot['version'] > 0 and
            snapshot['version'] == int(first['stateVersion']['N']) and snapshot.get('generationDigest') == generation['digest'] and
            isinstance(snapshot.get('payload'), dict), 'accounting snapshot is outside the original generation/version')
    payload = snapshot['payload']
    validate(payload['ledger'])
    require(payload['ledger']['candidateKey'] == store.candidate, 'accounting ledger candidate differs')
    require(head() == first, 'accounting head advanced during recovery; repeat observation without replacing state')
    return payload, generation
