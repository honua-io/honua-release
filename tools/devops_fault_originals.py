"""Rejudge retained CONNECT journal against authenticated original event custody.

The tunnel is mechanical transport only. This join cannot turn event references
into authority: the caller must also validate the actual mutation, provider read
and birth-bound executor terminal originals to which these exact hashes refer.
"""
from devops_fault_tunnel import FaultProfile
from devops_provisioning_contract import document, require, OPERATION, sha256


def verify_fault_original(original: bytes, *, profile: FaultProfile, operation_id: str,
                          mutation_original: bytes, resource_original: bytes, terminal_original: bytes):
    profile.validate()
    require(type(operation_id) is str and OPERATION.fullmatch(operation_id), 'fault-join-operation')
    require(all(type(raw) is bytes and bool(raw) for raw in
                (mutation_original, resource_original, terminal_original)), 'fault-join-originals')
    expected_hashes = {'mutation-bound': sha256(mutation_original),
                       'armed': sha256(resource_original), 'restored': sha256(terminal_original)}
    value = document(original)
    require(set(value) == {'contextSha256', 'journal', 'journalExhausted', 'faultedConnections',
            'acceptedConnections', 'armed', 'closed', 'candidateQualified', 'sixAwsCellsQualified'},
            'fault-join-snapshot-shape')
    require(value['contextSha256'] == profile.context_sha256
            and value['journalExhausted'] is False and value['armed'] is False
            and value['closed'] is True and value['candidateQualified'] is False
            and value['sixAwsCellsQualified'] is False, 'fault-join-snapshot-state')
    require(type(value['faultedConnections']) is int and value['faultedConnections'] > 0
            and type(value['acceptedConnections']) is int and value['acceptedConnections'] > 0,
            'fault-join-no-actual-cut')
    journal = value['journal']
    require(type(journal) is list and 6 <= len(journal) <= 4096, 'fault-join-journal-bound')
    stage = 0
    drops = 0
    connections = 0
    previous_time = 0
    for ordinal, event in enumerate(journal):
        require(type(event) is dict and type(event.get('ordinal')) is int
                and event['ordinal'] == ordinal and type(event.get('monotonicNs')) is int
                and event['monotonicNs'] >= previous_time and event['monotonicNs'] > 0,
                'fault-join-journal-order')
        previous_time = event['monotonicNs']
        kind = event.get('kind')
        base = {'ordinal', 'kind', 'monotonicNs'}
        if kind == 'started':
            require(stage == 0 and ordinal == 0 and set(event) == base | {'contextSha256'}
                    and event['contextSha256'] == profile.context_sha256, 'fault-join-start')
            stage = 1
        elif kind in expected_hashes:
            expected_stage = {'mutation-bound': 1, 'armed': 2, 'restored': 3}[kind]
            require(stage == expected_stage and set(event) == base | {'originalSha256', 'operationId'}
                    and event['operationId'] == operation_id
                    and event['originalSha256'] == expected_hashes[kind], 'fault-join-lifecycle-original')
            stage += 1
        elif kind == 'closed':
            require(stage == 4 and set(event) == base,
                    'fault-join-close')
            stage = 5
        elif kind == 'connection-dropped':
            require(stage == 3 and set(event) == base | {'host', 'phase'}
                    and event['host'] in profile.fault_hosts
                    and event['phase'] in ('existing', 'new', 'connecting'), 'fault-join-cut-target')
            drops += 1
        elif kind == 'connected':
            require(1 <= stage <= 4 and set(event) == base | {'host'}
                    and event['host'] in profile.allowed_hosts, 'fault-join-passthrough-target')
            connections += 1
        else:
            # Closing an accepted socket can cause a worker's final read to
            # append connection-ended after closed; it cannot add new traffic.
            require(kind == 'connection-ended' and 1 <= stage <= 5 and set(event) == base,
                    'fault-join-unknown-event')
    require(stage == 5 and drops == value['faultedConnections']
            and connections == value['acceptedConnections'], 'fault-join-lifecycle-complete')
    return {'status': 'fault-transport-joined', 'originalSha256': sha256(original),
            'faultedConnections': drops, 'candidateQualified': False, 'sixAwsCellsQualified': False}
