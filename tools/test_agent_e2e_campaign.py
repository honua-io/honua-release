"""Offline producer rejection tests. These fixtures never constitute live evidence."""
import base64
import copy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
import yaml

import agent_e2e_campaign as producer
from agent_e2e_pack import PackError, canonical_digest, load_json
from agent_e2e_state import preserve_history
from test_agent_e2e_gate import campaign, NOW  # Complete gate fixture; no network/model execution.


def identity():
    return {'GITHUB_REPOSITORY': 'honua-io/honua-release', 'GITHUB_REF': 'refs/heads/trunk',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a'*40,
            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}


@pytest.mark.parametrize('field,value', [('GITHUB_REF', 'refs/heads/fix'),
    ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REPOSITORY', 'fork/release'),
    ('GITHUB_SHA', 'trunk'), ('GITHUB_RUN_ID', '0')])
def test_identity_rejects_untrusted_producer(field, value):
    environment = identity()
    environment[field] = value
    with pytest.raises((PackError, ValueError)):
        producer.controller_identity(environment)


def test_shards_cover_exact_252_and_keep_c_held_out():
    pack = load_json(producer.PACK)
    qualified, development = [], []
    for target in pack['targets']:
        for shard in range(6):
            qualified += producer.selected_cases(pack, {'campaignMode': 'qualification'}, target, shard, 6)
            development += producer.selected_cases(pack, {'campaignMode': 'development'}, target, shard, 6)
    assert len(qualified) == len(set(qualified)) == 252
    assert sum(cid.endswith(':aws-eks') for cid in qualified) == 72
    assert len(development) == len(set(development)) == 168
    assert all(':C:' not in cid for cid in development)


def test_freeze_binds_complete_expectations_knowledge_and_candidate_before_execution(campaign, monkeypatch):
    monkeypatch.setattr(producer, 'ROOT', campaign)
    monkeypatch.setattr(producer, 'PACK', campaign/'pack.json')
    old = load_json(campaign/'campaign.json')
    expected = load_json(campaign/'expectations.json')
    producer.write(campaign/'reviewed-expectations.json', {'cases': expected['cases']})
    config = {'expectations': {'path': 'reviewed-expectations.json'},
              'knowledge': {'path': 'evidence/knowledge/knowledge-manifest.json'},
              'model': {'modelId': old['modelId'], 'returnedModelId': old['returnedModelId']}}
    root = campaign/'frozen'
    digest = producer.freeze(config, campaign/'lock.json', root, 'qualification',
        identity=producer.controller_identity(identity()), now=datetime(2026, 10, 14, 9, tzinfo=timezone.utc))
    frozen = load_json(root/'campaign.json')
    assert frozen['totalBudgetUsd'] == 500
    assert len(load_json(root/'expectations.json')['cases']) == 252
    assert frozen['knowledge'] == old['knowledge']
    assert digest == producer.digest_bytes((root/'campaign.json').read_bytes())
    assert frozen['qualificationLockDigest'] == producer.digest_bytes((campaign/'lock.json').read_bytes())
    with pytest.raises(FileExistsError):
        producer.freeze(config, campaign/'lock.json', root, 'qualification',
                        identity=producer.controller_identity(identity()))


def test_freeze_rejects_reduced_case_expectations(campaign, monkeypatch):
    monkeypatch.setattr(producer, 'ROOT', campaign)
    monkeypatch.setattr(producer, 'PACK', campaign/'pack.json')
    expected = load_json(campaign/'expectations.json')['cases']
    producer.write(campaign/'reviewed-expectations.json', {'cases': {k:v for k,v in expected.items() if ':aws-eks' not in k}})
    config = {'expectations': {'path': 'reviewed-expectations.json'}}
    with pytest.raises(PackError, match='exact 252 cases'):
        producer.freeze(config, campaign/'lock.json', campaign/'frozen', 'qualification',
                        identity=producer.controller_identity(identity()))


def test_reviewed_inputs_and_frozen_bundle_remain_identical_after_private_source_relocation(campaign, monkeypatch, tmp_path):
    import shutil
    from types import SimpleNamespace
    original = campaign
    old = load_json(original/'campaign.json')
    expected = load_json(original/'expectations.json')
    producer.write(original/'reviewed-expectations.json', {'cases': expected['cases']})
    (original/'backend-entry.py').write_text('raise RuntimeError("preflight must not import code")\n')
    producer.write(original/'backend-settings.json', {'offline': True})
    pricing_path = original/'certification/agent-e2e/model-pricing.json'
    pricing_path.parent.mkdir(parents=True)
    pricing = {'modelId': old['modelId'], 'returnedModelId': old['returnedModelId'], 'region': 'us-east-1',
               'inputUsdPerMillion': '1', 'outputUsdPerMillion': '2'}
    profile = {'inferenceProfileId': old['modelId'],
        'inferenceProfileArn': 'arn:aws:bedrock:us-east-1:123456789012:inference-profile/'+old['modelId'],
        'status': 'ACTIVE', 'type': 'SYSTEM_DEFINED',
        'models': [{'modelArn': 'arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0'}]}
    profile_path = original/'certification/agent-e2e/model-profile.json'
    producer.write(profile_path, profile)
    pricing.update(profile={'path': 'certification/agent-e2e/model-profile.json',
        'sha256': producer.digest_bytes(profile_path.read_bytes())}, profileArn=profile['inferenceProfileArn'],
        tokenCountingModelArn=profile['models'][0]['modelArn'])
    producer.write(pricing_path, pricing)
    def ref(relative):
        return {'path': relative, 'sha256': producer.digest_bytes((original/relative).read_bytes())}
    config = {'schemaVersion': 1, 'bootstrap': {'artifactDigest': 'sha256:'+'d'*64}, 'state': {},
        'backend': {'kind': 'in-process', 'script': 'backend-entry.py',
            'scriptSha256': producer.digest_bytes((original/'backend-entry.py').read_bytes()),
            'configuration': ref('backend-settings.json')}, 'expectations': ref('reviewed-expectations.json'),
        'knowledge': ref('evidence/knowledge/knowledge-manifest.json'),
        'model': {**pricing, 'pricingDigest': producer.digest_bytes(pricing_path.read_bytes())}}
    producer.write(original/'certification/agent-e2e/campaign-inputs.json', config)
    def use_source(root):
        monkeypatch.setattr(producer, 'ROOT', root)
        monkeypatch.setattr(producer, 'PACK', root/'pack.json')
        monkeypatch.setattr(producer, 'CONFIG', root/'certification/agent-e2e/campaign-inputs.json')
    monkeypatch.setenv('TERMINAL_MODEL_NAME', old['modelId'])
    # The surrounding source archive/index is authenticated by the handoff;
    # this fixture only accepts the same nonmutating git tracked-file queries.
    def tracked(argv, **kwargs):
        assert argv[:1] == ['git'] and argv[3:5] == ['ls-files', '--error-unmatch']
        assert not Path(argv[-1]).is_absolute()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(producer.subprocess, 'run', tracked)
    use_source(original)
    reviewed = producer.inputs('development')
    assert reviewed == config
    producer.freeze(reviewed, original/'lock.json', original/'portable-frozen', 'development',
                    identity=producer.controller_identity(identity()))
    relocated = tmp_path/'private-controller/source'
    shutil.copytree(original, relocated)
    use_source(relocated)
    assert producer.inputs('development') == load_json(relocated/'portable-frozen/producer-inputs.json') == config
    assert str(original) not in json.dumps(config)


@pytest.mark.parametrize('shard,shards', [(-1, 6), (6, 6), (0, 0), (0, 73), (True, 6)])
def test_invalid_shards_fail(shard, shards):
    with pytest.raises(PackError):
        producer.selected_cases(load_json(producer.PACK), {'campaignMode': 'qualification'}, 'local-docker', shard, shards)


def test_development_scope_can_admit_one_implemented_case_before_full_backend_exists():
    pack = load_json(producer.PACK)
    selected, required, supported = producer.execution_scope(pack, {'campaignMode': 'development'},
        None, 0, 6, ['P10:A:local-docker'])
    assert selected == ['P10:A:local-docker']
    assert supported == set(selected)
    assert 'independent-oracles' in required
    assert 'native-batch' not in required and 'fault-injection' not in required
    assert 'independent-semantic-suite' not in required
    with pytest.raises(PackError, match='held-out'):
        producer.execution_scope(pack, {'campaignMode': 'development'}, None, 0, 6, ['P10:C:local-docker'])
    with pytest.raises(PackError, match='reduced case inventory'):
        producer.execution_scope(pack, {'campaignMode': 'qualification'}, None, 0, 6, ['P10:A:local-docker'])
    with pytest.raises(PackError, match='explicit implemented'):
        producer.execution_scope(pack, {'campaignMode': 'development'}, None, 0, 6, None)


def test_development_capabilities_follow_actual_assertions():
    pack = load_json(producer.PACK)
    _, required, _ = producer.execution_scope(pack, {'campaignMode': 'development'},
        None, 0, 6, ['P14:A:local-docker'])
    assert {'separate-approvals', 'bootstrap-revocation'} <= required


@pytest.mark.parametrize('attribution', ['model', 'product', 'undetermined', None])
def test_retry_refuses_all_but_retained_independent_infrastructure_failure(campaign, attribution):
    frozen = load_json(campaign/'campaign.json')
    receipt = load_json(campaign/'receipts/0.json')
    receipt.update(status='fail', failureAttribution=attribution)
    state = {'attemptArtifacts': {'one': {'receipt': receipt}}}
    with pytest.raises(PackError, match='does not authorize'):
        producer.next_attempt(state, frozen, receipt['caseId'])


def test_only_one_existing_infrastructure_retry_and_no_unresolved_replay(campaign):
    frozen = load_json(campaign/'campaign.json')
    receipt = load_json(campaign/'receipts/0.json')
    receipt.update(status='fail', failureAttribution='infrastructure')
    state = {'attemptArtifacts': {'one': {'receipt': receipt}}}
    assert producer.next_attempt(state, frozen, receipt['caseId']) == 2
    key = canonical_digest({'campaignId': frozen['campaignId'], 'caseId': receipt['caseId'], 'attempt': 2})
    state['journals'] = {key: [{'event': 'start'}]}
    with pytest.raises(PackError, match='do not redispatch'):
        producer.next_attempt(state, frozen, receipt['caseId'])
    state.pop('journals')
    second = copy.deepcopy(receipt)
    second['attempt'] = 2
    state['attemptArtifacts']['two'] = {'receipt': second}
    with pytest.raises(PackError, match='retry budget exhausted'):
        producer.next_attempt(state, frozen, receipt['caseId'])


def test_new_durable_fields_cannot_be_erased_or_rewritten():
    previous = {'campaigns': {'one': 'digest'}, 'attemptArtifacts': {'attempt': {'receipt': {'status': 'fail'}}}}
    for field in previous:
        changed = copy.deepcopy(previous)
        changed[field].clear()
        with pytest.raises(PackError, match='retained campaign evidence'):
            preserve_history(previous, changed)
    preserve_history(previous, copy.deepcopy(previous))


class Durable:
    def __init__(self, state=None):
        self.state = state or {}
        self.persisted = 0

    def _require_live_lease(self):
        pass

    def _persist(self):
        self.persisted += 1


def fixture_export(root):
    receipt = load_json(root/'receipts/0.json')
    result = {k: copy.deepcopy(receipt[k]) for k in ('attempt', 'status', 'failureAttribution', 'usage')}
    artifacts = {p.relative_to(root/'evidence').as_posix(): base64.b64encode(p.read_bytes()).decode()
                 for p in (root/'evidence').rglob('*') if p.is_file()}
    return result, {'protocol': 'honua.agent-e2e.backend/v1', 'receipt': receipt, 'evidence': artifacts}


def test_retains_real_export_without_manufacturing_outcome(campaign, monkeypatch):
    monkeypatch.setattr(producer, 'PACK', campaign/'pack.json')
    frozen = load_json(campaign/'campaign.json')
    result, exported = fixture_export(campaign)
    durable = Durable()
    producer.retain_attempt(durable, frozen, exported['receipt']['caseId'], result, exported)
    assert durable.persisted == 1
    assert durable.state['registry']['attempts'][0]['status'] == result['status']
    assert list(durable.state['attemptArtifacts'].values())[0]['receipt'] == exported['receipt']
    with pytest.raises(PackError, match='already retained'):
        producer.retain_attempt(durable, frozen, exported['receipt']['caseId'], result, exported)


@pytest.mark.parametrize('change', ['status', 'modelId', 'usage', 'caseId', 'mode', 'path', 'encoding'])
def test_receipt_cannot_relabel_controller_or_escape_evidence(campaign, monkeypatch, change):
    monkeypatch.setattr(producer, 'PACK', campaign/'pack.json')
    frozen = load_json(campaign/'campaign.json')
    result, exported = fixture_export(campaign)
    cid = exported['receipt']['caseId']
    if change == 'status': exported['receipt']['status'] = 'fail'
    elif change == 'usage': exported['receipt']['usage']['modelTurns'] = 100
    elif change == 'mode': exported['receipt']['mode'] = 'mock'
    elif change == 'path': exported['evidence']['../outside'] = 'e30='
    elif change == 'encoding': exported['evidence']['bad'] = '%%%'
    else: exported['receipt'][change] = 'another'
    with pytest.raises((PackError, ValueError)):
        producer.retain_attempt(Durable(), frozen, cid, result, exported)


def test_full_backend_unavailable_blocks_before_model_and_state(campaign, monkeypatch):
    frozen = load_json(campaign/'campaign.json')
    monkeypatch.setattr(producer, 'controller_identity', lambda: frozen['controller'])
    config = {'backend': {}}
    producer.write(campaign/'producer-inputs.json', config)
    class Missing:
        def call(self, *_args):
            return {'status': 'ready', 'capabilities': [], 'caseIds': []}
    def forbidden(*_args):
        pytest.fail('must not open paid execution/state before backend readiness')
    monkeypatch.setattr(producer, 'open_store', forbidden)
    with pytest.raises(PackError, match='all 252 cases including EKS'):
        producer.execute(config, campaign, 'local-docker', 0, 6, backend=Missing())


def test_complete_offline_fixture_finalization_runs_unchanged_full_gate(campaign, monkeypatch):
    """Prove bundle compatibility; fixture data is never attested or uploaded."""
    monkeypatch.setattr(producer, 'PACK', campaign/'pack.json')
    frozen = load_json(campaign/'campaign.json')
    monkeypatch.setattr(producer, 'controller_identity', lambda: frozen['controller'])
    root = campaign/'producer-final'
    root.mkdir()
    for name, source in [('campaign.json', 'campaign.json'), ('qualification-lock.json', 'lock.json'),
                         ('expectations.json', 'expectations.json')]:
        (root/name).write_bytes((campaign/source).read_bytes())
    config = {'backend': {}, 'state': {}}
    producer.write(root/'producer-inputs.json', config)
    artifacts = {p.relative_to(campaign/'evidence').as_posix(): base64.b64encode(p.read_bytes()).decode()
                 for p in (campaign/'evidence').rglob('*') if p.is_file()}
    attempts = {}
    for index, path in enumerate((campaign/'receipts').glob('*.json')):
        receipt = load_json(path)
        key = canonical_digest({k: receipt[k] for k in ('campaignId', 'caseId', 'attempt')})
        attempts[key] = {'receipt': receipt, 'evidence': artifacts if index == 0 else {}}
    durable = Durable({'ledger': load_json(campaign/'ledger.json'), 'registry': load_json(campaign/'registry.json'),
        'campaigns': {frozen['campaignId']: canonical_digest(frozen)}, 'attemptArtifacts': attempts})
    durable.close = lambda: None
    monkeypatch.setattr(producer, 'DurableExecution', lambda *_args: durable)
    monkeypatch.setattr(producer, 'open_store', lambda *_args: None)
    validate = producer.gate.validate
    calls = []
    def actual_gate(*args):
        calls.append(args)
        return validate(*args, now=NOW)
    monkeypatch.setattr(producer.gate, 'validate', actual_gate)
    trusted = load_json(campaign/'trusted.json')
    class Export:
        def call(self, operation, payload, **kwargs):
            assert operation == 'export_campaign'
            return {'protocol': 'honua.agent-e2e.backend/v1', 'evidence': {},
                    **{k: trusted[k] for k in ('knowledgeManifest', 'semanticReport', 'operationReports')}}
    result = producer.finalize(config, root, backend=Export())
    assert len(calls) == 1 and result['status'] == 'pass'
    assert result['casesExpected'] == result['casesPassed'] == 252
    assert len(load_json(root/'trusted-inventory.json')['receipts']) == 252


@pytest.mark.parametrize('change', [None, 'digest', 'path', 'symlink'])
def test_bulk_evidence_export_uses_exact_private_controller_path(tmp_path, change):
    class Export:
        def call(self, operation, payload, **kwargs):
            path = Path(payload['exportPath'])
            value = {'protocol': 'honua.agent-e2e.backend/v1', 'evidence': {}}
            producer.write(path, value)
            digest = producer.digest_bytes(path.read_bytes())
            if change == 'symlink':
                alternate = path.with_suffix('.other')
                path.rename(alternate)
                path.symlink_to(alternate)
            return {'protocol': 'honua.agent-e2e.backend/v1', 'export': {
                'path': str(path) if change != 'path' else '/etc/passwd',
                'sha256': digest if change != 'digest' else 'sha256:'+'0'*64}}
    if change is None:
        result = producer.export_backend(Export(), 'export_attempt', {'caseId': 'case'}, tmp_path)
        assert result['evidence'] == {}
    else:
        with pytest.raises(PackError):
            producer.export_backend(Export(), 'export_attempt', {'caseId': 'case'}, tmp_path)


def test_workflow_attests_only_after_complete_gate_and_preserves_targets():
    workflow = yaml.safe_load((producer.ROOT/'.github/workflows/agent-e2e-campaign.yml').read_text())
    jobs = workflow['jobs']
    assert jobs['develop']['if'] == "inputs.mode == 'development'"
    assert '--case-id' in json.dumps(jobs['develop'])
    assert 'attest-build-provenance' not in json.dumps(jobs['develop'])
    previous = 'freeze'
    for job, target in [('docker', 'local-docker'), ('ecs', 'aws-ecs-redis-on'),
                        ('eks', 'aws-eks'), ('lambda', 'aws-lambda'), ('batch', 'aws-batch')]:
        assert jobs[job]['needs'] == previous
        assert jobs[job]['runs-on'] == 'ubuntu-latest'
        text = json.dumps(jobs[job])
        assert '--target '+target in text
        assert 'continue-on-error' not in text
        previous = job
    final = jobs['finalize']
    assert final['needs'] == 'batch'
    assert final['if'] == "inputs.mode == 'qualification'"
    steps = final['steps']
    validate_index = next(i for i, s in enumerate(steps) if '--operation finalize' in s.get('run', ''))
    attest_index = next(i for i, s in enumerate(steps) if 'attest-build-provenance' in s.get('uses', ''))
    assert validate_index < attest_index
    assert 'campaign/campaign.json' in steps[attest_index]['with']['subject-path']
    assert 'campaign/trusted-inventory.json' in steps[attest_index]['with']['subject-path']
