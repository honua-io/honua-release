"""Full-denominator gate replay using offline controller fixtures, never live evidence."""
import copy
from datetime import timedelta
import json
from uuid import UUID

import pytest

import agent_e2e_gate as gate
import agent_e2e_native_bindings as native
from agent_e2e_pack import PackError
from agent_e2e_semantics import response_evidence
from test_agent_e2e_gate import campaign, validate, write


def original(path, value, observer, candidate):
    intent = {'schemaVersion': 1, 'candidateKey': candidate, 'attemptId': observer['attemptId'],
        'step': 'offline-contract-only', 'endpoint': observer['endpoint'], 'credentialKind': observer['credentialKind'],
        'method': 'GET', 'path': path, 'contentType': 'application/json', 'requestBodyDigest': gate.digest(b'')}
    response = {'schemaVersion': 1, 'candidateKey': candidate, 'attemptId': observer['attemptId'],
        'step': intent['step'], 'intentDigest': gate.canonical_digest(intent),
        'intentReference': {'key': 'controller-retained-fixture/'+path, 'sha256': gate.canonical_digest(intent)},
        'response': response_evidence(200, json.dumps(value, sort_keys=True).encode())}
    return {'intent': intent, 'response': response}


def install_native_expectations(root, monkeypatch):
    pack, frozen = gate.read_json(root/'pack.json'), gate.read_json(root/'campaign.json')
    expectations, trusted = gate.read_json(root/'expectations.json'), gate.read_json(root/'trusted.json')
    registry = gate.read_json(root/'registry.json')
    # The existing 252-case gate fixture has two generic assertions in each
    # scenario. Exercise a real supported publication predicate in its first
    # nine cases while retaining the entire 252-case inventory and other checks.
    pack['scenarios'][0]['assertions'] = ['approval.pending', 'filter.exact']
    write(root/'pack.json', pack)
    monkeypatch.setattr(gate, 'FROZEN_PACK_BYTES', (root/'pack.json').read_bytes())
    frozen['packDigest'] = gate.digest((root/'pack.json').read_bytes())
    refs = []
    binding_paths = []
    for index, reference in enumerate(trusted['receipts']):
        receipt_path = root/'receipts'/reference['path']
        receipt = gate.read_json(receipt_path)
        receipt['packDigest'] = frozen['packDigest']
        if receipt['scenarioId'] == 'P01':
            relations = {'itemId': str(UUID(int=100+index)), 'versionId': str(UUID(int=200+index)),
                'contentHash': 'a'*64, 'route': '/maps/fixture-'+str(index), 'visibility': 'public',
                'requesterPrincipalId': 'fixture-requester-'+str(index)}
            observer = {'endpoint': 'https://isolated-fixture.invalid', 'attemptId': 'observer-fixture-'+str(index),
                        'credentialKind': 'bearer'}
            declaration = {'profile': native.PROFILE, 'fields': ['proposalId'], 'observers': {'1': observer},
                           'relationships': relations}
            proposal_id = 'native-fixture-proposal-'+str(index)
            operation_id = 'native-fixture-operation-'+str(index)
            instant = (gate.timestamp(receipt['startedAt'])+timedelta(seconds=10)).isoformat()
            submission = {'success': True, 'data': {'operationId': native.OPERATION,
                'operationInstanceId': operation_id, 'status': 'RequiresApproval', 'proposalId': proposal_id,
                'auditId': 'native-fixture-audit-'+str(index), 'correlationId': 'native-fixture-correlation-'+str(index),
                'createdAt': instant}}
            proposal = {'proposalId': proposal_id, 'kind': 'StudioDraftMutation', 'status': 'AwaitingApproval',
                'requestedBy': relations['requesterPrincipalId'], 'createdAt': instant,
                'diff': [field+'='+relations[field] for field in ('itemId', 'versionId', 'contentHash', 'route', 'visibility')]}
            report = {'schemaVersion': 1, 'profile': native.PROFILE, 'name': 'publication',
                'context': {key: receipt[key] for key in native.IDENTITY},
                'contractDigest': gate.canonical_digest(declaration), 'stage': 'proposal',
                'records': {'submissionHandle': original('/api/v1/operations/handles/'+operation_id,
                    submission, observer, frozen['componentBindingDigest']),
                    'proposal': original('/api/v1/admin/proposals/'+proposal_id, proposal, observer, frozen['componentBindingDigest'])},
                'values': {'proposalId': proposal_id}}
            report_path = root/'evidence'/f'native-binding-fixture-{index}.json'
            report_ref = write(report_path, report)
            binding_paths.append(report_path)
            observation = {'proposalId': proposal_id, 'status': 'AwaitingApproval', 'publishedResourceIds': []}
            observation_ref = write(root/'evidence'/f'native-observation-fixture-{index}.json', observation)
            case = expectations['cases'][receipt['caseId']]
            del case['assertions']['catalog.exact']
            case['assertions']['approval.pending'] = {**observation, 'proposalId': {'$native': 'publication.proposalId'}}
            case['nativeBindings'] = {'publication': declaration}
            oracle_path = root/'evidence'/receipt['oracle']['path']
            oracle = gate.read_json(oracle_path)
            oracle['assertions'] = [assertion for assertion in oracle['assertions'] if assertion['id'] != 'catalog.exact']
            oracle['assertions'].append({'id': 'approval.pending', 'status': 'pass', 'evidence': observation_ref})
            oracle['nativeBindings'] = {'publication': report_ref}
            receipt['oracle'] = write(oracle_path, oracle)
        new_ref = write(receipt_path, receipt)
        refs.append(new_ref)
        registry['attempts'][index]['packDigest'] = receipt['packDigest']
        registry['attempts'][index]['receiptDigest'] = new_ref['sha256']
    write(root/'expectations.json', expectations)
    frozen['expectationsDigest'] = gate.digest((root/'expectations.json').read_bytes())
    write(root/'campaign.json', frozen)
    write(root/'registry.json', registry)
    trusted.update(receipts=refs, campaignDigest=gate.digest((root/'campaign.json').read_bytes()),
                   attemptRegistryDigest=gate.digest((root/'registry.json').read_bytes()))
    write(root/'trusted.json', trusted)
    return binding_paths


def reattest_binding(root, path, transform):
    binding = gate.read_json(path)
    transform(binding)
    reference = write(path, binding)
    receipt_path = root/'receipts'/'0.json'
    receipt = gate.read_json(receipt_path)
    oracle_path = root/'evidence'/receipt['oracle']['path']
    oracle = gate.read_json(oracle_path)
    oracle['nativeBindings']['publication'] = reference
    receipt['oracle'] = write(oracle_path, oracle)
    receipt_ref = write(receipt_path, receipt)
    trusted, registry = gate.read_json(root/'trusted.json'), gate.read_json(root/'registry.json')
    trusted['receipts'][0] = receipt_ref
    registry['attempts'][0]['receiptDigest'] = receipt_ref['sha256']
    write(root/'registry.json', registry)
    trusted['attemptRegistryDigest'] = gate.digest((root/'registry.json').read_bytes())
    write(root/'trusted.json', trusted)


def test_native_pending_bindings_pass_all_original_252_cases_without_rewriting_frozen_expectations(campaign, monkeypatch):
    paths = install_native_expectations(campaign, monkeypatch)
    frozen_bytes = (campaign/'expectations.json').read_bytes()
    assert len(paths) == 9
    result = validate(campaign)
    assert result['casesExpected'] == result['casesPassed'] == 252
    assert result['attempts'] == 252
    assert (campaign/'expectations.json').read_bytes() == frozen_bytes


@pytest.mark.parametrize('change', ['different-case', 'model-id', 'changed-raw', 'wrong-relationship'])
def test_reattested_native_envelope_cannot_substitute_model_ids_or_cross_case_originals(campaign, monkeypatch, change):
    paths = install_native_expectations(campaign, monkeypatch)
    def transform(report):
        if change == 'different-case': report['context']['caseId'] = 'P01:B:local-docker'
        elif change == 'model-id': report['values']['proposalId'] = 'model-authored-substitute'
        elif change == 'changed-raw': report['records']['proposal']['response']['response']['rawBodyBase64'] = 'e30='
        elif change == 'wrong-relationship':
            pair = report['records']['proposal']
            payload = copy.deepcopy(pair['response']['response']['body'])
            payload['diff'][0] = 'itemId='+str(UUID(int=9999))
            pair['response']['response'] = response_evidence(200, json.dumps(payload).encode())
    reattest_binding(campaign, paths[0], transform)
    with pytest.raises((gate.Invalid, PackError)):
        validate(campaign)
