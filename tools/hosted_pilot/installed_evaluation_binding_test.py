"""Read-only binding controls; SDK-shaped fixtures do not qualify an installation."""
import copy
import json
from types import SimpleNamespace

import pytest

import installed_evaluation_binding as binding
from accounting_snapshot import HEAD, observe
from agent_e2e_pack import PackError, canonical_digest, digest_bytes
from agent_e2e_runtime_configuration import _verified_declaration
from test_accounting_recovery import AwsStore
from test_agent_e2e_interactive_budget import interactive
from test_agent_e2e_runtime_configuration import declaration
import test_native_runtime_registration as native_protocol


@pytest.fixture
def installation(declaration, tmp_path, monkeypatch):
    verified = declaration.load()
    protocol = native_protocol.NativeRegistrationProtocol()
    protocol.setUp()
    protocol.candidate = verified['candidateKey']
    protocol.durable.store = SimpleNamespace(candidate=verified['candidateKey'], bucket='retained', table='state')
    protocol.durable.state = copy.deepcopy(declaration.durable.state)
    protocol.request = verified['registrationRequest']
    protocol.keys = {kind: value['issued'] for kind, value in verified['keys'].items()}
    protocol.references = {kind: value['reference'] for kind, value in verified['keys'].items()}
    protocol.provenance = {'sourceConfiguration': verified['declarationReference'], 'nativeBootstrap': verified['nativeBootstrap'],
        'fundingBinding': verified['fundingBinding'], 'operation': verified['operation'],
        'workerProofs': [worker['proof'] for worker in verified['workers']], 'evidence': verified['evidence']}
    source_calls = []
    monkeypatch.setattr(binding, 'source_guard', lambda commit: source_calls.append(commit))

    def read(path, sha256):
        actual = tmp_path/path
        if not actual.exists(): actual = binding.ROOT/path
        assert digest_bytes(actual.read_bytes()) == sha256
        return actual

    def loader(reference, **kwargs):
        assert not hasattr(kwargs['durable'], '_require_live_lease')
        assert not hasattr(kwargs['durable'], '_persist')
        return _verified_declaration(reference, **kwargs, source_reader=read,
            worker_verifier=lambda *args, **options: copy.deepcopy(declaration.verified))

    monkeypatch.setattr(binding, '_verified_declaration', loader)
    try:
        yield SimpleNamespace(protocol=protocol, verified=verified, ref=declaration.ref,
                              source_calls=source_calls, loader=loader)
    finally:
        protocol.doCleanups()


def publish(installation):
    protocol = installation.protocol
    aws = AwsStore(protocol.candidate, {'bucket': 'retained', 'table': 'state'})
    aws.store.acquire()
    aws.store.commit(protocol.durable.state)
    aws.store.release()
    aws.readonly = True
    aws.calls.clear()
    return aws


def export(installation, aws):
    return binding.export_binding(installation.ref, store=aws.store, s3=aws.s3,
        ecs=None, states=None, release_commit='a'*40)


def test_complete_registration_exports_original_digest_without_any_cloud_mutation(installation):
    result = installation.protocol.provision()
    aws = publish(installation)
    objects, head = copy.deepcopy(aws.objects), copy.deepcopy(aws.row)
    actual = export(installation, aws)
    assert actual == {'schemaVersion': 1, 'candidateKey': installation.protocol.candidate,
        'runtimeId': 'runtime-original', 'registrationDigest': result['registration']['registrationDigest'],
        'evidence': {'region': 'us-east-1', 'bucket': 'retained'},
        'release': {'root': str(binding.ROOT), 'commit': 'a'*40}}
    assert aws.objects == objects and aws.row == head
    assert aws.calls and {operation for operation, _ in aws.calls} == {'GetItem', 'GetObjectVersion'}
    assert all(body.closed for body in aws.bodies)
    assert len(installation.source_calls) >= 4
    for secret in installation.protocol.secrets.values(): assert secret not in json.dumps(actual)


def test_original_lost_post_acknowledgement_can_export_from_completed_get_readback(installation):
    installation.protocol.lose_post = True
    with pytest.raises(PackError, match='reconcile without replay'): installation.protocol.provision()
    installation.protocol.provision()
    assert len(installation.protocol.posts()) == 1
    assert export(installation, publish(installation))['registrationDigest'] == installation.protocol.original['registrationDigest']


@pytest.mark.parametrize('lost_post_ack', [False, True])
def test_failed_setup_observation_then_recovery_exports_only_original_post_authority(installation, lost_post_ack):
    protocol = installation.protocol
    original_open = protocol.open
    failed = False
    def transient_setup(request, timeout):
        nonlocal failed
        if request.get_header('X-api-key') == protocol.secrets['setup'] and not failed:
            failed = True
            return native_protocol.Response(503, {'error': 'transient-setup-read'})
        return original_open(request, timeout)
    protocol.open = transient_setup
    with pytest.raises(PackError, match='did not succeed'): protocol.provision()
    assert not protocol.posts()
    failed_records = copy.deepcopy(protocol.durable.state['scenarioOperations'])
    protocol.lose_post = lost_post_ack
    if lost_post_ack:
        with pytest.raises(PackError, match='reconcile without replay'): protocol.provision()
    result = protocol.provision()
    assert len(protocol.posts()) == 1
    assert all(protocol.durable.state['scenarioOperations'][key] == row for key,row in failed_records.items())
    assert export(installation, publish(installation))['registrationDigest'] == result['registration']['registrationDigest']


@pytest.mark.parametrize('mutation', ['failed-completion-reference', 'missing-successful-setup'])
def test_recovery_cannot_substitute_earlier_failed_setup_for_original_post_authority(installation, mutation):
    protocol = installation.protocol
    original_open = protocol.open
    failed = False
    def transient_setup(request, timeout):
        nonlocal failed
        if request.get_header('X-api-key') == protocol.secrets['setup'] and not failed:
            failed = True
            return native_protocol.Response(503, {'error': 'transient-setup-read'})
        return original_open(request, timeout)
    protocol.open = transient_setup
    with pytest.raises(PackError): protocol.provision()
    result = protocol.provision()
    records = protocol.durable.state['scenarioOperations']
    prefix = next(key.removesuffix('/completed') for key in records if key.endswith('/completed'))
    failed_key, failed_row = next((key,row) for key,row in records.items()
        if key.startswith(prefix+'/http/response/setup-target-') and row['response']['httpStatus'] == 503)
    if mutation == 'failed-completion-reference':
        records[prefix+'/completed']['value']['nativeOriginals']['setup-target'] = {'key':failed_key,'sha256':canonical_digest(failed_row)}
    else:
        records.pop(result['nativeOriginals']['setup-target']['key'])
    with pytest.raises((ValueError, PackError)): export(installation, publish(installation))


@pytest.mark.parametrize('mutation', ['missing-completion', 'missing-post-intent', 'missing-setup', 'declared-digest',
    'changed-provenance', 'response-scope', 'raw-response', 'current-permissions', 'final-registration'])
def test_authenticated_snapshot_does_not_make_missing_or_changed_native_proof_valid(installation, mutation):
    result = installation.protocol.provision()
    records = installation.protocol.durable.state['scenarioOperations']
    completed = next(key for key, row in records.items() if row.get('producer') == binding.PRODUCER and key.endswith('/completed'))
    prefix = completed.removesuffix('/completed')
    if mutation == 'missing-completion': records.pop(completed)
    elif mutation == 'missing-post-intent': records.pop(prefix+'/http/intent/register')
    elif mutation == 'missing-setup':
        records.pop(next(key for key in records if key.startswith(prefix+'/http/response/setup-target-')))
    elif mutation == 'declared-digest': records[completed]['value']['registration']['registrationDigest'] = 'sha256:'+'0'*64
    elif mutation == 'changed-provenance': records[completed]['value']['provenance']['workerProofs'] = []
    else:
        name = 'registration-final' if mutation == 'final-registration' else 'after-task-effective'
        ref = records[completed]['value']['nativeOriginals'][name]
        row = records[ref['key']]
        if mutation == 'response-scope': row['candidateKey'] = 'sha256:'+'0'*64
        elif mutation == 'raw-response': row['response']['rawBodyBase64'] = 'e30='
        else:
            from agent_e2e_semantics import response_evidence
            body = row['response']['body']
            if mutation == 'current-permissions': body['data']['permissions'] = ['admin:write']
            else: body['registrationDigest'] = 'sha256:'+'0'*64
            row['response'] = response_evidence(200, json.dumps(body).encode())
        ref['sha256'] = canonical_digest(row)
    with pytest.raises((ValueError, PackError)): export(installation, publish(installation))


def test_head_advance_after_snapshot_refuses_without_lease_or_replacement_state(installation):
    installation.protocol.provision()
    aws = publish(installation)
    count = 0
    def advance():
        nonlocal count
        count += 1
        if count == 3: aws.row['stateVersion'] = {'N': '2'}
    aws.on_get_head = advance
    with pytest.raises(ValueError, match='head changed'): export(installation, aws)
    assert {operation for operation, _ in aws.calls} == {'GetItem', 'GetObjectVersion'}


def test_expiry_after_original_snapshot_refuses_before_worker_verification(installation, monkeypatch):
    installation.protocol.provision()
    aws = publish(installation)
    expiry = installation.verified['originalFundingExpiresEpoch']
    monkeypatch.setattr(binding.time, 'time', lambda: expiry)
    with pytest.raises(ValueError, match='funding expired'): export(installation, aws)


def test_snapshot_head_return_preserves_existing_two_value_reader_contract(installation):
    installation.protocol.provision()
    aws = publish(installation)
    payload, generation = observe(aws.store)
    observed_payload, observed_generation, head = observe(aws.store, include_head=True)
    assert (observed_payload, observed_generation) == (payload, generation)
    assert head == {field: aws.row[field] for field in HEAD}


def test_source_change_after_initial_read_refuses_export(installation, monkeypatch):
    installation.protocol.provision()
    aws = publish(installation)
    calls = []
    def guard(commit):
        calls.append(commit)
        if len(calls) == 3: raise ValueError('source changed')
    monkeypatch.setattr(binding, 'source_guard', guard)
    with pytest.raises(ValueError, match='source changed'): export(installation, aws)
