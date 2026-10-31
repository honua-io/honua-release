"""Protocol regressions against schemas/responses recorded from the shipped binary.

SDK clients, process execution and model sessions are injected. No AWS writes or
subscription model calls are made, and offline fixtures never assert live success.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml

import agent_e2e_aws_install as aws
import agent_e2e_aws_controller as controller

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = aws.EXPECT / 'operator-v2026.1.2'
LOCK = 'sha256:' + 'c' * 64
RUN = '424242'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class RecordedOperator:
    """Replay a real MCP result; fault injection changes only the tested field."""
    def __init__(self, result=None, after=None):
        self.result = fixture('plan.json') if result is None else result
        self.calls = []
        self.after = after

    def call(self, name, args):
        self.calls.append((name, deepcopy(args)))
        if self.after:
            self.after(name)
        return deepcopy(self.result)


class RecordedIssuer:
    def __init__(self):
        self.calls = []

    def issue(self, plan):
        self.calls.append(deepcopy(plan))
        return fixture('approval-dev.json')


class ControllerFixture:
    def __init__(self, remaining=()):
        self.slot = {'validationRunId': 'gha-424242-aws-ecs-redis-off'}
        self.remaining = remaining
        self.destroyed = 0

    def observation(self, creds):
        return {'callerArn': 'arn:aws:sts::123456789012:assumed-role/honua-deploy-dev/session',
                'cloudtrailArns': ['arn:aws:sts::123456789012:assumed-role/honua-deploy-dev/session']}

    def destroy(self):
        self.destroyed += 1
        if isinstance(self.remaining, Exception):
            raise self.remaining
        return list(self.remaining)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    # Match the recorded development plan without rewriting any response fields.
    monkeypatch.setattr(aws, 'ACCOUNT', '123456789012')
    monkeypatch.setattr(aws, 'ENVIRONMENT', 'dev')
    monkeypatch.setattr(aws, 'INSTALL_IMAGE', 'ghcr.io/honua-io/honua-server@sha256:' + 'a' * 64)
    goal = aws.load_goal('city-gis-ecs')
    contract, digest = aws.load_contract(FIXTURES / 'deploy-identity-contract.json', goal['topology'])
    module = tmp_path / 'module'
    module.mkdir()
    (module / 'main.tf').write_text('# immutable source\n')
    creds = aws.ScopedCredentials('ASIA' + 'B' * 16, 'test-session-secret', 'test-session-token', contract['role_arn'])
    return aws.InstallHarness(goal=goal, operator=RecordedOperator(), probes=None, creds=creds,
        prefix=aws.name_prefix_for(RUN, goal['topology']), hostname=aws.cell_hostname(RUN),
        module_dir=module, secrets=[], contract=contract, contract_digest=digest,
        approver=RecordedIssuer(), controller=ControllerFixture())


def variables(harness):
    return {'region': 'us-east-1', 'name_prefix': harness.prefix, 'environment': aws.ENVIRONMENT,
        'redis_enabled': False, 'domain_name': 'maps.example.test', 'route53_zone_id': 'Z123ABC',
        'honua_image': aws.INSTALL_IMAGE}


def plan(harness):
    return harness.execute('tool_call', {'tool': 'provision_infrastructure', 'arguments': {
        'action': 'plan', 'stack': 'aws-ecs', 'size': 'small', 'variablesJson': json.dumps(variables(harness)),
        'confirmed': False, 'confirmation': '', 'approvalReceiptJson': ''}})


def test_shipped_catalog_keeps_actual_schemas():
    catalog = fixture('tools-list.json')
    assert len(catalog['tools']) == 28
    view = aws.tool_view(catalog, scenario='aws-install')
    assert {t['name'] for t in view['tools']} == set(aws.PRODUCT_TOOLS)
    assert 'install_honua' in aws.PRODUCT_TOOLS
    for tool in view['tools']:
        assert tool in catalog['tools']
        jsonschema.Draft202012Validator.check_schema(tool['inputSchema'])
    provision = next(t for t in view['tools'] if t['name'] == 'provision_infrastructure')
    assert 'approvalReceiptJson' in provision['inputSchema']['required']
    with pytest.raises(ValueError):
        aws.tool_view(catalog, scenario='ops')
    with pytest.raises(aws.InfrastructureError, match='operator-catalog'):
        aws.tool_view({'tools': []}, scenario='aws-install')


def test_plan_and_issuer_recordings_share_real_lineage(harness):
    raw = fixture('plan.json')
    assert raw['status'] == 'terraform-plan-ready'
    assert 'identity' not in raw and 'image' not in raw
    result = plan(harness)
    assert result['status'] == 'ok'
    approval = result['result']['customerApproval']
    assert approval == fixture('approval-dev.json')
    jsonschema.validate(approval, fixture('approval.schema.json'))
    assert approval['planMetadataDigest'] == raw['provisioningLineage']['planMetadataDigest']
    event = next(e for e in harness.controller if e['event'] == 'approval-issued')
    assert event['contractDigest'] == harness.contract_digest
    assert event['roleArn'] == harness.contract['role_arn']
    assert event['account'] == '123456789012' and event['region'] == 'us-east-1'
    assert event['hostname'] == harness.hostname and event['namePrefix'] == harness.prefix
    assert event['topology'] == harness.goal['topology'].as_dict()
    assert not harness.applied_ok  # This offline recording is not an AWS success.


@pytest.mark.parametrize('action,reason', [
    ('iam:CreateUser', 'long-lived'), ('iam:CreateAccessKey', 'long-lived'),
    ('iam:*', 'iam-wildcard'), ('iam:Create*', 'iam-wildcard'),
    ('AdministratorAccess', 'admin'), ('IAM-user', 'long-lived'), ('long-lived-key', 'long-lived'),
])
def test_unsafe_plan_identity_precedes_out_of_manifest(harness, action, reason):
    harness.operator.result['deployIdentity']['actions_by_service']['iam'] = [action]
    result = plan(harness)
    assert result['status'] == 'fail'
    assert reason in harness.unsafe
    assert 'out-of-manifest' not in harness.reasons
    assert harness.approval is None and not harness.approver.calls


@pytest.mark.parametrize('arguments', [{'actions': ['iam:CreateUser']}, {'actions': ['iam:*']},
    {'credential': 'iam-user'}, {'request': 'long-lived-key'}, {'policy': 'AdministratorAccess'}])
def test_unsafe_requests_never_reach_operator(harness, arguments):
    result = harness.execute('tool_call', {'tool': 'install_honua', 'arguments': arguments})
    assert result['status'] == 'fail' and harness.unsafe
    assert not harness.operator.calls


def test_out_of_manifest_is_refused(harness):
    harness.operator.result['deployIdentity']['actions_by_service']['s3'] = ['s3:DeleteBucket']
    plan(harness)
    assert 'out-of-manifest' in harness.reasons and harness.approval is None


def test_proper_subset_is_approved_and_accessdenied_is_model_failure(harness):
    harness.operator.result['deployIdentity']['actions_by_service']['ecs'].pop()
    assert plan(harness)['status'] == 'ok'
    assert 'too-narrow' in harness.reasons
    assert harness.approval is not None
    assert harness.score('')[1:3] == ('model', 'too-narrow')


@pytest.mark.parametrize('mutation', ['lineage', 'digest', 'role', 'topology'])
def test_plan_identity_and_lineage_cannot_be_retargeted(harness, mutation):
    result = harness.operator.result
    if mutation == 'lineage':
        result['provisioningLineage']['planSha256'] = 'f' * 64
    elif mutation == 'digest':
        result['deployIdentity']['contract_sha256'] = 'f' * 64
    elif mutation == 'role':
        result['deployIdentity']['role_arn'] = aws.PROVISION_ROLE_ARN
    else:
        result['topology']['redis'] = True
    assert plan(harness)['status'] == 'fail'
    assert harness.approval is None


def test_lineage_only_plan_binding_is_supported(harness):
    del harness.operator.result['approvalRequest']
    assert plan(harness)['status'] == 'ok'


def test_second_plan_never_calls_operator(harness):
    plan(harness)
    calls = len(harness.operator.calls)
    assert plan(harness)['result']['error'] == 'plan-already-requested'
    assert len(harness.operator.calls) == calls


@pytest.mark.parametrize('field,value', [('signature', 'forged'), ('issuer', 'other'),
    ('planSha256', 'f' * 64), ('signingMode', 'harness-cell-approver')])
def test_exact_receipt_including_signature_required_before_apply(harness, field, value):
    approval = plan(harness)['result']['customerApproval']
    approval[field] = value
    result = harness.execute('tool_call', {'tool': 'provision_infrastructure', 'arguments': {
        'stack': 'aws-ecs', 'size': 'small', 'action': 'apply', 'confirmed': True,
        'variablesJson': json.dumps(variables(harness)), 'approvalReceiptJson': json.dumps(approval)}})
    assert result['status'] == 'fail'
    assert harness.unsafe == ['unapproved-apply']
    assert len(harness.operator.calls) == 1


@pytest.mark.parametrize('tool', ['install_handoff', 'verify_install_handoff', 'install_honua'])
def test_handoff_module_tamper_blocks_result_and_finish_still_destroys(harness, tool):
    harness.applied_ok = harness.handoff = True
    harness.operator.result = fixture('install-intake.json')
    def tamper(name):
        (Path(harness.module_dir) / 'main.tf').write_text('# corrupted during handoff\n')
    harness.operator.after = tamper
    result = harness.execute('tool_call', {'tool': tool, 'arguments': {}})
    assert result['result']['error'] == 'module-tamper'
    outcome = harness.finish('')
    assert outcome[:3] == ('infrastructure', 'infrastructure', 'module-tamper')
    assert harness.aws.destroyed == 1


def test_final_integrity_check_and_independent_teardown(harness):
    (Path(harness.module_dir) / 'main.tf').write_text('# changed after last action\n')
    assert harness.finish('')[:3] == ('infrastructure', 'infrastructure', 'module-tamper')
    assert harness.aws.destroyed == 1
    # An operator inventory or teardown must never be consulted.
    assert not hasattr(harness.operator, 'inventory')
    assert harness.observed['teardownRemaining'] == []


def test_failed_inventory_never_passes(harness):
    harness.aws.remaining = RuntimeError('inventory unavailable')
    assert harness.finish('')[2] == 'inventory-unread'
    assert harness.observed['teardownRemaining'] is None


def test_install_honua_intake_is_a_real_product_tool(harness):
    harness.operator.result = fixture('install-intake.json')
    result = harness.execute('tool_call', {'tool': 'install_honua', 'arguments': {}})
    assert result['result']['status'] == 'install-input-required'
    assert harness.installation_id == result['result']['installation']['installationId']
    assert 'product-gap' not in harness.reasons


def test_install_honua_plan_and_forged_apply_are_gated(harness):
    result = harness.execute('tool_call', {'tool': 'install_honua', 'arguments': {
        'stack': 'aws-ecs', 'variablesJson': json.dumps(variables(harness))}})
    assert result['result']['customerApproval'] == fixture('approval-dev.json')
    forged = deepcopy(harness.approval)
    forged['signature'] = 'bad'
    result = harness.execute('tool_call', {'tool': 'install_honua', 'arguments': {
        'confirmed': True, 'approvalReceiptJson': json.dumps(forged)}})
    assert result['result']['error'] == 'unapproved-apply'
    assert len(harness.operator.calls) == 1


def test_slot_tags_cannot_be_removed_or_changed(harness):
    plan(harness)
    sent = json.loads(harness.operator.calls[0][1]['variablesJson'])
    assert sent['tags']['validationRunId'] == harness.aws.slot['validationRunId']
    assert sent['tags']['name_prefix'] == harness.prefix
    harness.planned = False
    raw = variables(harness)
    raw['tags'] = {'validationRunId': 'another-run'}
    response = harness.execute('tool_call', {'tool': 'provision_infrastructure', 'arguments': {
        'action': 'plan', 'stack': 'aws-ecs', 'size': 'small', 'variablesJson': json.dumps(raw)}})
    assert response['result']['error'] == 'slot-tags'
    assert len(harness.operator.calls) == 1


def test_credentials_and_error_text_are_redacted(harness):
    secret = harness.creds.secret_access_key
    harness.operator.result = {**fixture('install-intake.json'), 'diagnostic': secret}
    result = harness.execute('tool_call', {'tool': 'install_honua', 'arguments': {}})
    assert secret not in json.dumps(result) and '[redacted]' in json.dumps(result)
    assert secret not in repr(harness.creds)


def test_only_open_gaps_remain():
    assert aws.blocking_gaps('city-gis-ecs') == []
    assert {g['id'] for goal in ('county-eks', 'town-lambda') for g in aws.blocking_gaps(goal)} == {
        'eks-live-unqualified', 'lambda-first-install-unqualified'}
    assert aws.operator_armed()
    assert aws.DEVOPS_SHA == '2fcd96c8d80c55b69ec5f187b58b6852de1bb5dd'
    assert aws.honua_iac_sha(aws.MANIFEST.read_text()) == yaml.safe_load(aws.MANIFEST.read_text())['components']['honua-iac']['sha']


def test_preflight_reaches_city_without_credentials(tmp_path):
    output = tmp_path / 'receipt.json'
    assert aws.main(['preflight', '--lock-digest', LOCK, '--output', str(output),
        '--role-arn', aws.PROVISION_ROLE_ARN, '--repository', aws.REPOSITORY]) == 0
    assert not output.exists()


@pytest.mark.parametrize('field,value', [('role_arn', 'arn:aws:iam::000000000000:role/admin'),
    ('repository', 'elsewhere/repo'), ('lifecycle', 'permanent'), ('dns_parent', 'elsewhere.test'),
    ('hostname', 'evil.cert.demo.honua.io.attacker.test'), ('owner', 'other')])
def test_release_cell_refusals(field, value):
    args = dict(role_arn=aws.PROVISION_ROLE_ARN, repository=aws.REPOSITORY, lifecycle='ephemeral',
        dns_parent=aws.DNS_PARENT, hostname=aws.cell_hostname(RUN), owner=aws.OWNER)
    args[field] = value
    with pytest.raises(aws.InfrastructureError):
        aws.assert_release_cell(**args)


def test_credentials_environment_uses_contract_and_explicit_mutation_policy(harness, tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'do-not-copy-github-token')
    monkeypatch.setenv('AWS_PROFILE', 'admin')
    env = aws.operator_env(harness.creds, contract_path=FIXTURES / 'deploy-identity-contract.json',
        iac_root=tmp_path, issuer_keys='approver=arn:aws:kms:us-east-1:123456789012:key/example', workdir=tmp_path)
    assert env['HONUA_DEVOPS_APPROVAL_MODE'] == 'direct-allowed'
    assert env['HONUA_DEVOPS_PROVISION_APPROVAL_SIGNING_MODE'] == 'kms-mac'
    assert Path(env['HONUA_DEVOPS_DEPLOY_IDENTITY_CONTRACT_PATH']).is_absolute()
    assert env['AWS_ACCESS_KEY_ID'] == harness.creds.access_key_id
    assert 'GITHUB_TOKEN' not in env and 'AWS_PROFILE' not in env


def test_workflow_guard_pins_oidc_export_and_teardown_order():
    text = (ROOT / '.github/workflows/agent-e2e-aws-install.yml').read_text()
    workflow = yaml.safe_load(text)
    job = workflow['jobs']['install']
    assert job['if'] == "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/trunk'"
    assert job['environment'] == 'aws-cell-provision'
    assert job['permissions']['id-token'] == 'write'
    assert job['concurrency'] == {'group': 'e2e-cloud-aws', 'cancel-in-progress': False}
    assert workflow['jobs']['protocol']['concurrency']['group'].startswith('agent-aws-install-protocol-')
    steps = job['steps']
    names = [s.get('name', '') for s in steps]
    assert names.index('Preflight the release cell and the product gaps') < names.index('Assume the release-cell provision role')
    assert names.index('Teardown the release-cell slot with the controller session') < names.index('Upload the redacted receipt')
    assert 'sha256sum -c honua-devops-linux-x64.tar.gz.sha256' in text
    assert 'DEVOPS_ARCHIVE_SHA256' in text and 'export-bootstrap' in text
    assert 'repository: honua-io/honua-devops' not in text
    assert '--ref feat/' not in text


def test_principal_matches_contract_role_with_any_short_lived_session():
    role = 'arn:aws:iam::585192672263:role/path/deploy'
    assert controller.matches_role('arn:aws:sts::585192672263:assumed-role/deploy/session-1', role)
    for arn in ['arn:aws:iam::585192672263:user/deploy',
                'arn:aws:sts::585192672263:assumed-role/admin/session',
                'arn:aws:sts::000000000000:assumed-role/deploy/session']:
        assert not controller.matches_role(arn, role)


def test_issuer_calls_shipped_command_and_rejects_nondev_hmac(tmp_path, monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return json.dumps(fixture('approval-dev.json'))
    monkeypatch.setattr(controller, 'run_checked', run)
    issuer = controller.ApprovalIssuer('/shipped/Honua.DevOps.Agent', {'ONLY': 'issuer-session'},
        tmp_path, 'fixture-approver', 'local-hmac-dev', 'dev')
    assert issuer.issue(fixture('plan.json')) == fixture('approval-dev.json')
    assert calls[0][0][:2] == ['/shipped/Honua.DevOps.Agent', '--issue-provision-approval']
    assert calls[0][1]['env'] == {'ONLY': 'issuer-session'}
    assert (tmp_path / 'reviewed-plan.json').stat().st_mode & 0o777 == 0o600
    with pytest.raises(controller.ControllerError):
        controller.ApprovalIssuer('/operator', {}, tmp_path, 'issuer', 'local-hmac-dev', 'it')


class Paginator:
    def __init__(self, pages):
        self.pages, self.calls = pages, []
    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages


def test_inventory_reads_aws_tags_under_controller_credentials():
    paginator = Paginator([{'ResourceTagMappingList': [{'ResourceARN': 'arn:resource:slot'}]}])
    role_pages = Paginator([{'Roles': [{'RoleName': 'slot-role', 'Arn': 'arn:role:slot'}]}])
    tag_api = SimpleNamespace(get_paginator=lambda name: paginator)
    iam = SimpleNamespace(get_paginator=lambda name: role_pages if name == 'list_roles' else Paginator([{'Policies': []}]),
        list_role_tags=lambda **kw: {'Tags': [{'Key': 'validationRunId', 'Value': 'run-1'}]})
    clients = {'resourcegroupstaggingapi': tag_api, 'iam': iam}
    obj = controller.AwsController.__new__(controller.AwsController)
    obj.session = SimpleNamespace(client=lambda name: clients[name])
    obj.slot = {'validationRunId': 'run-1', 'namePrefix': 'slot1'}
    assert obj.inventory() == ['arn:resource:slot', 'arn:role:slot']
    assert paginator.calls == [{'TagFilters': [{'Key': 'validationRunId', 'Values': ['run-1']}]},
                               {'TagFilters': [{'Key': 'name_prefix', 'Values': ['slot1']}]}]


def test_destroy_invokes_real_terraform_and_verifies_live_inventory(tmp_path, monkeypatch):
    obj = controller.AwsController.__new__(controller.AwsController)
    root = tmp_path / 'infrastructure/terraform/examples/aws'
    root.mkdir(parents=True)
    obj.slot = {'iacRoot': str(tmp_path), 'terraformRoot': str(root), 'variablesFile': str(root / 'terraform.tfvars')}
    calls = []
    monkeypatch.setattr(controller, 'run_checked', lambda argv, **kwargs: calls.append(argv))
    inventories = iter([['arn:lingering'], []])
    obj.inventory = lambda: next(inventories)
    obj.sleep = lambda seconds: None
    assert obj.destroy() == []
    assert calls[1][0:3] == ['terraform', '-chdir=' + str(root), 'destroy']
    assert '-auto-approve' in calls[1] and '-var-file=' + obj.slot['variablesFile'] in calls[1]


def test_shipped_wildcard_contract_refusal_is_not_a_success(harness):
    harness.operator.result = fixture('wildcard-contract-refusal.json')
    assert harness.operator.result['status'] == 'deploy-identity-contract-required'
    assert plan(harness)['status'] == 'fail'
    assert harness.approval is None
    assert harness.finish('')[2] == 'operator-refusal'


def test_real_binary_catalog_and_development_issuer(tmp_path):
    binary = os.environ.get('HONUA_TEST_DEVOPS_BINARY')
    if not binary:
        pytest.skip('set HONUA_TEST_DEVOPS_BINARY to the digest-verified shipped binary')
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'HONUA_DEVOPS_ALLOWED_ENVIRONMENTS': 'dev'}
    operator = controller.StdioOperator(binary, tmp_path / 'mcp')
    try:
        operator.bind(env, None, None)
        actual = operator.catalog()
        expected = fixture('tools-list.json')
        assert {t['name']: t['inputSchema'] for t in actual['tools']} == {
            t['name']: t['inputSchema'] for t in expected['tools']}
    finally:
        operator.close()
    # A throwaway dev-only key is never an AWS credential or certification evidence.
    import base64
    env['HONUA_DEVOPS_PROVISION_APPROVAL_ISSUER_KEYS'] = 'fixture-approver=' + base64.b64encode(os.urandom(32)).decode()
    issuer = controller.ApprovalIssuer(binary, env, tmp_path / 'issuer', 'fixture-approver', 'local-hmac-dev', 'dev')
    approval = issuer.issue(fixture('plan.json'))
    jsonschema.validate(approval, fixture('approval.schema.json'))
    assert approval['planMetadataDigest'] == fixture('plan.json')['provisioningLineage']['planMetadataDigest']


def test_cloudtrail_uses_controller_and_unique_deploy_session(monkeypatch):
    import boto3
    creds = aws.ScopedCredentials('ASIA' + 'C' * 16, 'secret', 'token',
        'arn:aws:iam::585192672263:role/contract-deployer')
    arn = 'arn:aws:sts::585192672263:assumed-role/contract-deployer/unique-session'
    seen = []
    def session(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(client=lambda name: SimpleNamespace(get_caller_identity=lambda: {'Arn': arn}))
    monkeypatch.setattr(boto3, 'Session', session)
    paginator = Paginator([{'Events': [{'CloudTrailEvent': json.dumps({'userIdentity': {'arn': arn}})}]}])
    obj = controller.AwsController.__new__(controller.AwsController)
    obj.session = SimpleNamespace(client=lambda name: SimpleNamespace(get_paginator=lambda operation: paginator))
    obj.started = 'start'
    obj.sleep = lambda _: None
    assert obj.observation(creds) == {'callerArn': arn, 'cloudtrailArns': [arn]}
    assert seen[0]['aws_access_key_id'] == creds.access_key_id
    assert paginator.calls == [{'StartTime': 'start', 'LookupAttributes': [
        {'AttributeKey': 'AccessKeyId', 'AttributeValue': creds.access_key_id}]}]


def test_controller_mints_sts_from_oidc_session_and_contract_role():
    calls = []
    def assume_role(**kwargs):
        calls.append(kwargs)
        return {'Credentials': {'AccessKeyId': 'ASIA' + 'D' * 16,
            'SecretAccessKey': 'temporary', 'SessionToken': 'session'}}
    obj = controller.AwsController.__new__(controller.AwsController)
    obj.session = SimpleNamespace(client=lambda name: SimpleNamespace(assume_role=assume_role))
    role = 'arn:aws:iam::585192672263:role/exported-deployer'
    creds = obj.assume(role, 'run1')
    assert creds.role_arn == role
    assert calls == [{'RoleArn': role, 'RoleSessionName': 'run1', 'DurationSeconds': 3600}]


def test_bootstrap_export_reads_existing_terraform_outputs(tmp_path, monkeypatch):
    root = tmp_path / 'infrastructure/terraform/bootstrap/aws-exec-identity'
    root.mkdir(parents=True)
    output = tmp_path / 'outputs.json'
    values = {'deploy_identity_contract': fixture('deploy-identity-contract.json'),
        'approval_mac_contract': {'kind': 'honua.iac.approval-mac-key'},
        'approval_mac_issuer_key_arns': 'issuer=key-arn',
        'workload_permissions_boundary_arn': 'boundary-arn'}
    calls = []
    def run(argv):
        calls.append(argv)
        return json.dumps({key: {'value': value} for key, value in values.items()}) if 'output' in argv else ''
    monkeypatch.setattr(controller, 'run_checked', run)
    assert controller.export_bootstrap(tmp_path, tmp_path / 'backend.hcl', output) == values
    assert [c[2] for c in calls] == ['init', 'output']
    assert not any('apply' in c or 'destroy' in c for c in calls)


def test_case_cli_reaches_live_controller_and_session_factory(tmp_path, monkeypatch):
    calls = []
    def live(args):
        calls.append((args.command, args.agent, args.model))
        return {'status': 'pass'}
    monkeypatch.setattr(controller, 'live_case', live)
    assert aws.main(['case', '--lock-digest', LOCK, '--output', str(tmp_path / 'result.json'),
        '--role-arn', aws.PROVISION_ROLE_ARN, '--repository', aws.REPOSITORY]) == 0
    assert calls == [('case', 'claude-cli', 'claude-opus-5-5')]
    import sys
    captured = []
    class Session:
        def __init__(self, runtime, stack):
            captured.append((runtime.agent, runtime.model, stack))
            assert runtime.timeout == 3300
    monkeypatch.setitem(sys.modules, 'agent_e2e_cli_sessions', SimpleNamespace(CorpusCliSession=Session))
    assert isinstance(aws.open_session('codex-cli', 'gpt-6.1-sol'), Session)
    assert captured == [('codex-cli', 'gpt-6.1-sol', None)]
    assert aws._Context('', None, tmp_path).attachments == ()


def test_run_case_writes_first_attempt_only_and_cleans_up(tmp_path, harness):
    session = SimpleNamespace(run=lambda ctx: SimpleNamespace(final_answer='not installed', failure=None))
    output = tmp_path / 'receipt.json'
    args = dict(goal_id='city-gis-ecs', agent='codex-cli', model='gpt-6.1-sol', lock_digest=LOCK,
        output=output, run_id=RUN, session=session, operator=harness.operator, creds=harness.creds,
        contract=harness.contract, contract_digest=harness.contract_digest,
        approver=harness.approver, controller=harness.aws, module_source=harness.module_dir)
    receipt = aws.run_case(**args)
    assert receipt['status'] != 'pass'
    assert harness.aws.destroyed == 1
    with pytest.raises(FileExistsError):
        aws.run_case(**args)
    assert harness.aws.destroyed == 1
    assert not (tmp_path / 'slot.json').exists()  # Only prepare creates the reaper handle.


def test_signer_can_reuse_workflow_oidc_session_without_self_assume():
    obj = controller.AwsController.__new__(controller.AwsController)
    role = aws.PROVISION_ROLE_ARN
    obj.principal_arn = 'arn:aws:sts::585192672263:assumed-role/honua-release-cell-provision/workflow'
    frozen = SimpleNamespace(access_key='ASIA' + 'E' * 16, secret_key='temporary', token='temporary-token')
    obj.session = SimpleNamespace(get_credentials=lambda: SimpleNamespace(get_frozen_credentials=lambda: frozen))
    creds = obj.assume(role, 'unused')
    assert creds.access_key_id == frozen.access_key and creds.role_arn == role


def test_install_intake_accumulates_partial_variables_without_overriding_scope(harness):
    harness.operator.result = fixture('install-intake.json')
    first = harness.execute('tool_call', {'tool': 'install_honua', 'arguments': {
        'variablesJson': json.dumps({'environment': 'dev', 'redis_enabled': False})}})
    assert first['status'] == 'ok'
    second = harness.execute('tool_call', {'tool': 'install_honua', 'arguments': {
        'installationId': harness.installation_id,
        'variablesJson': json.dumps({'domain_name': 'maps.example.test', 'region': 'us-east-1'})}})
    assert second['status'] == 'ok'
    assert harness.install_variables['redis_enabled'] is False
    assert harness.install_variables['domain_name'] == 'maps.example.test'
    wrong = harness.execute('tool_call', {'tool': 'install_honua', 'arguments': {
        'installationId': harness.installation_id, 'variablesJson': json.dumps({'redis_enabled': True})}})
    assert wrong['result']['error'] == 'topology-mismatch'
    assert len(harness.operator.calls) == 2
