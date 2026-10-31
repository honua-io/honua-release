"""AWS setup eval: the harness approves and observes. It does not apply."""
import builtins
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types

import pytest
import yaml

import agent_e2e_aws_install as aws


ROOT = Path(__file__).resolve().parents[1]
EXPECT = ROOT / 'certification' / 'agent-e2e' / 'expectations' / 'aws-install'
WORKFLOW = ROOT / '.github' / 'workflows' / 'agent-e2e-aws-install.yml'
LOCK = 'sha256:' + 'cd' * 32
RUN = '424242'
IAC_SHA = '03d16128676d6764f6de684e9fbb2f4a6bf90255'
LAYER = 'lyr-0123456789ab'
SLOT_SECRET = 'slot-secret-' + RUN


class Result:
    def __init__(self, final_answer, failure=None):
        self.final_answer = final_answer
        self.failure = failure
        self.agent_type = 'claude-cli'
        self.model_id = 'claude-opus-5-5'


class Probes:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.status, BaseException):
            raise self.status
        if callable(self.status):
            return self.status(url)
        return self.status


class FakeOperator:
    def __init__(self, plan=None, inventory_value=None, observe_value=None, leak=False,
                 tamper=False, boom=False):
        self.calls = []
        self.env = None
        self.creds = None
        self.module_dir = None
        self.plan = plan
        self.inventory_value = [] if inventory_value is None else inventory_value
        self.observe_value = observe_value
        self.leak = leak
        self.tamper = tamper
        self.boom = boom
        self.marker = 'MODULE-FILE-BODY-DO-NOT-LEAK'

    def bind(self, env, module_dir, creds):
        self.env = dict(env)
        self.module_dir = module_dir
        self.creds = creds

    def call(self, name, args):
        if self.boom:
            raise aws.InfrastructureError('operator-fault', detail='UNIQUE-EXCEPTION-TEXT-xyz')
        if self.tamper and self.module_dir:
            path = Path(self.module_dir) / 'main.tf'
            path.chmod(0o644)
            path.write_text(self.marker + '\n')
        self.calls.append((name, dict(args)))
        if name == 'provision_infrastructure' and args.get('action') == 'plan':
            body = dict(self.plan)
            if self.leak:
                body['stdout'] = self.creds.secret_access_key + self.creds.access_key_id + self.creds.session_token
                body['env'] = dict(self.env)
                body['leaked'] = self.creds
            return body
        if name == 'provision_infrastructure' and args.get('action') == 'apply':
            return {'ok': True}
        if name == 'verify_install_handoff':
            return {'status': 'verified', 'service': 'parcels', 'layerId': LAYER}
        if name == 'install_handoff':
            return {'status': 'install-handoff-written'}
        return {'ok': True}

    def observe(self):
        if self.observe_value is not None:
            return self.observe_value
        arn = aws.assumed_role_arn(self.creds.role_arn)
        return {'callerArn': arn, 'cloudtrail': [{'arn': arn}]}

    def inventory(self):
        if isinstance(self.inventory_value, BaseException):
            raise self.inventory_value
        return self.inventory_value


def variables_for(goal_id, run_id=RUN, **overrides):
    goal = aws.load_goal(goal_id)
    body = {
        'region': 'us-east-1',
        'name_prefix': aws.name_prefix_for(run_id, goal['topology']),
        'environment': 'it',
        'redis_enabled': goal['topology'].redis,
        'honua_image': aws.INSTALL_IMAGE,
    }
    if goal['topology'].domain:
        body['domain'] = goal['topology'].domain
    body.update(overrides)
    return body


def plan_for(goal_id, actions=None, **overrides):
    goal = aws.load_goal(goal_id)
    topology = goal['topology']
    if actions is None:
        actions = sorted(aws.manifest_for(topology))
    body = {
        'ok': True,
        'image': aws.INSTALL_IMAGE,
        'topology': topology.as_dict(),
        'identity': {'actions': list(actions)},
        'approvalRequest': {
            'provisioningOperationId': 'op-1',
            'planSha256': 'a' * 64,
            'planMetadataDigest': 'b' * 64,
            'action': 'apply',
            'stack': aws.STACKS[topology.compute],
            'environment': 'it',
        },
    }
    body.update(overrides)
    return body


def drive(ctx, goal_id, run_id=RUN, mutate_apply=None, final=None, plan_arguments=None):
    seen = []

    def call(tool, arguments):
        payload = ctx.execute('tool_call', {'tool': tool, 'arguments': arguments})
        seen.append((tool, arguments, payload))
        return payload

    goal = aws.load_goal(goal_id)
    stack = aws.STACKS[goal['topology'].compute]
    variables = variables_for(goal_id, run_id)
    for tool in ('describe_environment', 'analyze_customer_requirements', 'recommend_deployment_topology'):
        call(tool, {})
    plan_args = {
        'action': 'plan', 'stack': stack, 'size': 'small',
        'variablesJson': json.dumps(variables),
    }
    if plan_arguments:
        plan_args.update(plan_arguments)
    planned = call('provision_infrastructure', plan_args)
    approval = planned.get('customerApproval')
    apply_args = {
        'action': 'apply', 'stack': stack, 'size': 'small', 'confirmed': True,
        'approvalReceiptJson': json.dumps(approval) if approval else '{}',
        'variablesJson': json.dumps(variables),
    }
    if mutate_apply:
        apply_args = mutate_apply(apply_args, approval)
    call('provision_infrastructure', apply_args)
    call('install_handoff', {})
    verified = call('verify_install_handoff', {})
    layer = ''
    result = verified.get('result') if isinstance(verified, dict) else None
    if isinstance(result, dict):
        layer = result.get('layerId') or ''
    host = ''
    for line in ctx.prompt.splitlines():
        if line.startswith('The release cell hostname is '):
            host = line.removeprefix('The release cell hostname is ').rstrip('.')
    answer = final if final is not None else f'{host} {layer}'
    return Result(answer), seen


class Drive:
    def __init__(self, goal_id='city-gis-ecs', run_id=RUN, **kwargs):
        self.goal_id = goal_id
        self.run_id = run_id
        self.kwargs = kwargs
        self.seen = []
        self.prompt = ''
        self.view = None

    def run(self, ctx):
        self.prompt = ctx.prompt
        self.view = ctx.tool_view
        result, seen = drive(ctx, self.goal_id, self.run_id, **self.kwargs)
        self.seen = seen
        return result


def observe(tmp_path, session, operator, probes=None, goal_id='city-gis-ecs', run_id=RUN, **kwargs):
    output = tmp_path / 'receipt.json'
    receipt = aws.run_case(
        goal_id=goal_id, agent='claude-cli', model='claude-opus-5-5', lock_digest=LOCK,
        output=output, run_id=run_id, session=session, operator=operator, probes=probes,
        observer_only=True, **kwargs)
    return receipt, output


def provision_calls(operator, action=None):
    calls = [args for name, args in operator.calls if name == 'provision_infrastructure']
    if action is None:
        return calls
    return [args for args in calls if args.get('action') == action]


def cli(tmp_path, command, **overrides):
    output = tmp_path / f'{command}-receipt.json'
    values = {
        'goal_id': 'city-gis-ecs', 'agent': 'claude-cli', 'model': 'claude-opus-5-5',
        'lock_digest': LOCK, 'run_id': RUN, 'role_arn': aws.PROVISION_ROLE_ARN,
        'repository': aws.REPOSITORY, 'lifecycle': 'ephemeral', 'dns_parent': aws.DNS_PARENT,
        'hostname': '', 'lock_run_id': '',
    }
    values.update(overrides)
    argv = [
        command, '--goal-id', values['goal_id'], '--agent', values['agent'], '--model', values['model'],
        '--lock-digest', values['lock_digest'], '--output', str(output), '--run-id', values['run_id'],
        '--role-arn', values['role_arn'], '--repository', values['repository'],
        '--lifecycle', values['lifecycle'], '--dns-parent', values['dns_parent'],
        '--lock-run-id', values['lock_run_id'],
    ]
    if values['hostname']:
        argv.extend(['--hostname', values['hostname']])
    code = aws.main(argv)
    body = json.loads(output.read_text(encoding='utf-8')) if output.exists() else None
    return code, body, output


def test_reference_slices_match_the_curated_allow_sets():
    reference = aws.load_reference()
    expect = {
        'ecs': {'core': 117, 'redis': 7, 'custom_domain': 11, 'waf': 9, 'gp_batch': 0},
        'eks': {'core': 144, 'redis': 10, 'custom_domain': 11, 'waf': 0, 'gp_batch': 0},
        'lambda': {'core': 139, 'redis': 7, 'custom_domain': 13, 'waf': 0, 'gp_batch': 12},
    }
    for name, counts in expect.items():
        block = reference['topologies'][name]
        for key, count in counts.items():
            assert len(block[key]) == count
    city = aws.manifest_for(aws.load_goal('city-gis-ecs')['topology'])
    assert any(item.startswith('ecs:Create') for item in city)
    assert 'ecs:Create*' in city
    assert any(item.startswith('route53:') for item in city)
    assert not any('elasticache' in item for item in city)
    assert not any(item.startswith('wafv2:') for item in city)
    assert not any(item.startswith('eks:') or item.startswith('lambda:') for item in city)
    assert 'iam:*' not in city
    assert 'iam:PassRole' in city
    assert not any(item.startswith('iam:') and '*' in item for item in city)
    assert reference['reference'] == 'curated-bootstrap-policies'


def test_custom_iac_tool_is_not_part_of_the_harness():
    assert not (ROOT / 'tools' / 'agent_e2e_iac_tool.py').exists()
    text = (ROOT / 'tools' / 'agent_e2e_aws_install.py').read_text(encoding='utf-8')
    assert 'agent_e2e_iac_tool' not in text
    assert 'def iac_apply' not in text
    assert 'def iac_plan' not in text


def test_public_prompt_and_docs_do_not_list_actions():
    goal = aws.load_goal('city-gis-ecs')
    prompt = aws._prompt(goal, aws.cell_hostname(RUN), aws.name_prefix_for(RUN, goal['topology']))
    for name in aws.PRODUCT_TOOLS:
        assert name in prompt
    assert aws.cell_hostname(RUN) in prompt
    assert 'us-east-1' in prompt
    assert 'environment it' in prompt
    assert aws.INSTALL_IMAGE in prompt
    for blob in (prompt, aws.PUBLIC_DOCS):
        assert 'ecs:Create' not in blob
        assert 'elasticache:' not in blob
        assert 'iam:PassRole' not in blob


def test_tool_view_is_the_six_operator_tools_for_this_scenario_only():
    view = aws.tool_view(scenario='aws-install')
    names = [tool['name'] for tool in view['tools']]
    assert names == list(aws.PRODUCT_TOOLS)
    assert 'iac_plan' not in names and 'iac_apply' not in names and 'install_honua' not in names
    blob = json.dumps(view)
    assert 'ecs:Create' not in blob
    with pytest.raises(ValueError):
        aws.tool_view(scenario='studio')
    with pytest.raises(aws.InfrastructureError) as missing:
        aws.tool_view({'tools': [{'name': 'provision_infrastructure'}]}, scenario='aws-install')
    assert missing.value.code == 'operator-catalog'
    extra = {'tools': [{'name': name} for name in aws.PRODUCT_TOOLS] + [{'name': 'install_honua'}]}
    with pytest.raises(aws.InfrastructureError) as surplus:
        aws.tool_view(extra, scenario='aws-install')
    assert surplus.value.code == 'operator-catalog'
    exact = {'tools': [{'name': name} for name in reversed(aws.PRODUCT_TOOLS)]}
    assert [tool['name'] for tool in aws.tool_view(exact, scenario='aws-install')['tools']] == list(aws.PRODUCT_TOOLS)


def test_cell_names_and_release_pins():
    city = aws.load_goal('city-gis-ecs')
    assert aws.validation_run_id(RUN, city['topology']) == f'gha-{RUN}-aws-ecs-redis-off'
    assert aws.name_prefix_for(RUN, city['topology']) == 'honuar424242'
    assert aws.HOST_RE.fullmatch(aws.cell_hostname(RUN))
    county = aws.load_goal('county-eks')
    assert aws.validation_run_id('9', county['topology']) == 'gha-9-aws-eks-redis-on'
    town = aws.load_goal('town-lambda')
    assert aws.validation_run_id('9', town['topology']) == 'gha-9-aws-serverless-redis-off'
    assert aws.INSTALL_TAG == '2026.1-rc.1'
    assert aws.INSTALL_IMAGE == aws.rc1.prior_image()
    assert LOCK not in aws.INSTALL_IMAGE
    creds = aws.ScopedCredentials('ASIAaaaaaaaaaaaaaaaa', 'slot-secret-hidden', 'slot-session-hidden', 'arn')
    assert 'slot-secret-hidden' not in repr(creds)
    assert aws.redact(creds, creds.secrets()) == {'role_arn': 'arn', 'credential': 'short-lived-sts'}


def test_iac_sha_is_the_honua_iac_block(tmp_path, monkeypatch):
    fixture = """
  geospatial-mcp:
    sha: "d5a09d13c4ad541c05702e598c3679c0f42db7af"
  honua-iac:
    # 0eff5c13 91a886fb 97cf1830
    sha: "03d16128676d6764f6de684e9fbb2f4a6bf90255"   # v0.4.6
  honua-helm:
    sha: "5069bffb27a1fe48485bf48c798804821a59e61a"
"""
    assert aws.honua_iac_sha(fixture) == IAC_SHA
    # pull_request CI checks out the merge with trunk, which advances this pin.
    # The fixture above holds the parser; the live file is whatever this checkout pins.
    live = (ROOT / 'platform-manifest.yaml').read_text(encoding='utf-8')
    pinned = aws.honua_iac_sha(live)
    section = live.split('\n  honua-iac:\n', 1)[1]
    block = []
    for line in section.splitlines():
        if block and line.startswith('  ') and not line.startswith('    ') and line.rstrip().endswith(':'):
            break
        block.append(line)
    sha_lines = [line for line in block if line.startswith('    sha: "')]
    assert sha_lines and sha_lines[0].startswith(f'    sha: "{pinned}"')
    assert pinned != 'd5a09d13c4ad541c05702e598c3679c0f42db7af'
    out = tmp_path / 'github-output.txt'
    monkeypatch.setenv('GITHUB_OUTPUT', str(out))
    assert aws.main(['print-pins']) == 0
    text = out.read_text(encoding='utf-8')
    assert f'iac_sha={pinned}' in text
    assert f'devops_sha={aws.DEVOPS_SHA}' in text
    assert aws.main(['print-iac-sha']) == 0


@pytest.mark.parametrize('goal_id', ['city-gis-ecs', 'county-eks', 'town-lambda'])
def test_live_case_fails_closed_before_a_session_or_a_slot(tmp_path, goal_id):
    operator = FakeOperator(plan=plan_for(goal_id))
    called = {}

    class Boom:
        def run(self, ctx):
            called['ran'] = True
            raise AssertionError('live session must not start')

    output = tmp_path / 'receipt.json'
    receipt = aws.run_case(
        goal_id=goal_id, agent='claude-cli', model='claude-opus-5-5', lock_digest=LOCK,
        output=output, run_id=RUN, session=Boom(), operator=operator, observer_only=False)
    documented = json.loads((EXPECT / 'product-gaps.json').read_text(encoding='utf-8'))
    assert documented['sourceRev'] == aws.DEVOPS_SHA
    assert receipt['status'] == 'infrastructure'
    assert receipt['failureAttribution'] == 'infrastructure'
    assert receipt['reason'] == 'product-gap'
    assert 'ran' not in called
    assert operator.calls == []
    assert not (tmp_path / 'slot.json').exists()
    assert {gap['id'] for gap in receipt['productGaps']} == {
        gap_id for gap_id, body in documented['gaps'].items() if goal_id in body['goals']}
    assert 'install-honua-missing' in {gap['id'] for gap in receipt['productGaps']}


def test_a_cleared_blocks_flag_still_blocks(monkeypatch, tmp_path):
    original = aws._load

    def flipped(name):
        body = original(name)
        if name == 'product-gaps.json':
            for gap in body['gaps'].values():
                gap['blocks'] = False
        return body

    monkeypatch.setattr(aws, '_load', flipped)
    assert aws.blocking_gaps('city-gis-ecs')
    code, body, _ = cli(tmp_path, 'preflight')
    assert code == 2
    assert body['reason'] == 'product-gap'


def test_disarmed_bridge_blocks_preflight_even_without_gaps(monkeypatch, tmp_path):
    monkeypatch.setattr(aws, 'blocking_gaps', lambda goal_id: [])
    assert aws.operator_armed() is False
    code, body, _ = cli(tmp_path, 'preflight')
    assert code == 2
    assert body['reason'] == 'operator-not-armed'
    assert body['status'] == 'infrastructure'


def test_armed_preflight_with_no_gaps_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(aws, 'blocking_gaps', lambda goal_id: [])
    monkeypatch.setattr(aws, 'operator_armed', lambda: True)
    code, body, output = cli(tmp_path, 'preflight')
    assert code == 0
    assert body is None
    assert not output.exists()


def test_cleared_gaps_do_not_spawn_the_operator(monkeypatch, tmp_path):
    monkeypatch.setattr(aws, 'blocking_gaps', lambda goal_id: [])
    operator = FakeOperator()
    with pytest.raises(aws.InfrastructureError) as exc:
        aws.run_case(
            goal_id='city-gis-ecs', agent='claude-cli', model='claude-opus-5-5', lock_digest=LOCK,
            output=tmp_path / 'receipt.json', run_id=RUN, session=None, operator=operator,
            observer_only=False)
    assert exc.value.code == 'operator-not-armed'
    assert operator.calls == []
    assert not (tmp_path / 'slot.json').exists()


def test_operator_start_never_shells_out(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append(args))
    operator = aws.DevopsProcessOperator('/usr/bin/honua-devops', tmp_path)
    assert operator.command() == ['/usr/bin/honua-devops', '--mcp']
    with pytest.raises(aws.InfrastructureError) as exc:
        operator.start()
    assert exc.value.code == 'operator-not-armed'
    assert calls == []


def test_preflight_refuses_a_non_release_cell(tmp_path):
    wrong_role, role_body, _ = cli(tmp_path, 'preflight', role_arn='arn:aws:iam::585192672263:role/other')
    assert wrong_role == 1 and role_body['reason'] == 'release-cell-role'
    wrong_account, account_body, _ = cli(
        tmp_path / 'account', 'preflight',
        role_arn='arn:aws:iam::111111111111:role/honua-release-cell-provision')
    assert wrong_account == 1 and account_body['reason'] == 'release-cell-role'
    for lifecycle in ('standing', 'demo', 'cert'):
        code, body, _ = cli(tmp_path / lifecycle, 'preflight', lifecycle=lifecycle)
        assert code == 1 and body['reason'] == 'release-cell-lifecycle'
    code, body, _ = cli(tmp_path / 'dns', 'preflight', dns_parent='example.com')
    assert code == 1 and body['reason'] == 'release-cell-dns'
    code, body, _ = cli(tmp_path / 'repo', 'preflight', repository='honua-io/honua-devops')
    assert code == 1 and body['reason'] == 'release-cell-repository'
    code, body, _ = cli(tmp_path / 'host', 'preflight', hostname='maps.example.test')
    assert code == 1 and body['reason'] == 'release-cell-hostname'
    code, body, _ = cli(tmp_path / 'lock', 'preflight', lock_run_id='not-a-run')
    assert code == 1 and body['reason'] == 'lock-run-id'
    code, body, _ = cli(tmp_path / 'run', 'preflight', lock_run_id='123456')
    assert code == 2 and body['reason'] == 'product-gap'


def test_cli_product_gap_exits_nonzero_and_keeps_the_first_receipt(tmp_path, capsys):
    code, body, output = cli(tmp_path, 'case', goal_id='county-eks')
    assert code == 1
    assert body['reason'] == 'product-gap'
    assert body['attempt'] == 1
    assert 'eks-live-unqualified' in {gap['id'] for gap in body['productGaps']}
    assert not (tmp_path / 'slot.json').exists()
    first = output.read_bytes()
    with pytest.raises(FileExistsError):
        cli(tmp_path, 'case', goal_id='county-eks')
    assert output.read_bytes() == first
    with pytest.raises(SystemExit) as help_exit:
        aws.main(['--help'])
    assert help_exit.value.code == 0
    assert 'observer' not in capsys.readouterr().out
    with pytest.raises(SystemExit) as bad:
        aws.main(['preflight', '--observer-only'])
    assert bad.value.code == 2
    assert aws.main(['teardown', '--slot-file', str(tmp_path / 'missing.json')]) == 0
    slot = tmp_path / 'slot.json'
    slot.write_text(json.dumps({'owner': 'other', 'hostname': 'a.cert.demo.honua.io'}), encoding='utf-8')
    assert aws.main(['teardown', '--slot-file', str(slot)]) == 1
    slot.write_text(json.dumps({'owner': 'release-cell', 'hostname': 'a.cert.demo.honua.io'}), encoding='utf-8')
    assert aws.main(['teardown', '--slot-file', str(slot)]) == 0


def test_preflight_imports_on_system_python(tmp_path):
    output = tmp_path / 'receipt.json'
    proc = subprocess.run(
        ['python3', 'tools/agent_e2e_aws_install.py', 'preflight', '--goal-id', 'town-lambda',
         '--agent', 'codex-cli', '--model', 'gpt-6.1-sol', '--lock-digest', LOCK,
         '--output', str(output), '--run-id', RUN, '--role-arn', aws.PROVISION_ROLE_ARN,
         '--repository', aws.REPOSITORY, '--lifecycle', 'ephemeral', '--dns-parent', 'demo.honua.io'],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert proc.returncode == 2, proc.stderr
    assert 'product-gap' in proc.stdout
    body = json.loads(output.read_text(encoding='utf-8'))
    assert 'lambda-first-install-unqualified' in {gap['id'] for gap in body['productGaps']}


def test_attempt_and_existing_receipt_refuse_before_the_operator(tmp_path):
    operator = FakeOperator(plan=plan_for('city-gis-ecs'))
    with pytest.raises(ValueError):
        aws.run_case(
            goal_id='city-gis-ecs', agent='claude-cli', model='claude-opus-5-5', lock_digest=LOCK,
            output=tmp_path / 'receipt.json', run_id=RUN, attempt=2, session=Drive(), operator=operator,
            observer_only=True)
    assert operator.calls == []
    output = tmp_path / 'exists.json'
    output.write_text('kept', encoding='utf-8')
    with pytest.raises(FileExistsError):
        aws.run_case(
            goal_id='city-gis-ecs', agent='claude-cli', model='claude-opus-5-5', lock_digest=LOCK,
            output=output, run_id=RUN, session=Drive(), operator=operator, observer_only=True)
    assert output.read_text(encoding='utf-8') == 'kept'
    assert operator.calls == []


def test_context_shape_and_sessions(monkeypatch):
    ctx = aws._Context('prompt text', lambda *_args: None, ROOT)
    assert ctx.prompt == 'prompt text'
    assert [tool['name'] for tool in ctx.tool_view['tools']] == list(aws.PRODUCT_TOOLS)
    assert ctx.workdir == str(ROOT)
    assert ctx.session == 1
    assert ctx.limits.max_model_calls == 40
    assert ctx.limits.max_actions == 80
    assert ctx.limits.max_tokens == 1500000
    assert ctx.token_budget_used == 0
    with pytest.raises(aws.InfrastructureError) as proxy:
        ctx.mcp_proxy()
    assert proxy.value.code == 'no-credentialed-proxy'
    with pytest.raises(aws.InfrastructureError) as bedrock:
        aws.open_session('bedrock-proxy', 'claude-sonnet-4-6')
    assert bedrock.value.code == 'mcp-client-required'
    with pytest.raises(aws.InfrastructureError) as unsupported:
        aws.open_session('claude-cli', 'gpt-6.1-sol')
    assert unsupported.value.code == 'unsupported-arm'
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'agent_e2e_cli_sessions':
            raise ImportError('blocked')
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, '__import__', guarded)
    with pytest.raises(aws.InfrastructureError) as missing:
        aws.open_session('claude-cli', 'claude-opus-5-5')
    assert missing.value.code == 'cli-arms'
    assert 'feat/agent-cli-arms' in str(missing.value)
    monkeypatch.undo()
    module = types.ModuleType('agent_e2e_cli_sessions')

    class CorpusCliSession:
        def __init__(self, runtime, recorder):
            self.runtime = runtime
            self.recorder = recorder

    module.CorpusCliSession = CorpusCliSession
    monkeypatch.setitem(sys.modules, 'agent_e2e_cli_sessions', module)
    session = aws.open_session('codex-cli', 'gpt-6.1-sol')
    assert isinstance(session, CorpusCliSession)
    assert session.runtime.agent == 'codex-cli'
    assert session.runtime.model == 'gpt-6.1-sol'
    assert session.recorder is None


def _parent_env(monkeypatch):
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'parent-secret-value')
    monkeypatch.setenv('GITHUB_TOKEN', 'github-token-value')
    monkeypatch.setenv('GH_TOKEN', 'gh-token-value-xxxx')
    monkeypatch.setenv('AWS_WEB_IDENTITY_TOKEN_FILE', '/tmp/web-identity-token')
    monkeypatch.setenv('NOT_A_CHILD_VAR', 'ambient-not-copied-value')


def test_credentials_stay_in_the_operator_process(monkeypatch, tmp_path):
    _parent_env(monkeypatch)
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError(args)))
    session = Drive()
    session.kwargs = {}

    class Echo(Drive):
        def run(self, ctx):
            self.prompt = ctx.prompt
            self.view = ctx.tool_view
            result, seen = drive(ctx, 'city-gis-ecs', RUN)
            self.seen = seen
            result.final_answer = result.final_answer + '\n' + json.dumps(seen)
            return result

    echo = Echo()
    operator = FakeOperator(plan=plan_for('city-gis-ecs'), leak=True)
    probes = Probes(status=lambda url: (200, None))
    receipt, output = observe(tmp_path, echo, operator, probes)
    assert receipt['status'] == 'pass', receipt['reason']
    env = operator.env
    assert env['AWS_SECRET_ACCESS_KEY'] == SLOT_SECRET
    assert env['AWS_SESSION_TOKEN'] == 'slot-session-' + RUN
    assert 'parent-secret-value' not in env.values()
    assert 'GITHUB_TOKEN' not in env and 'GH_TOKEN' not in env
    assert 'AWS_WEB_IDENTITY_TOKEN_FILE' not in env
    assert 'NOT_A_CHILD_VAR' not in env
    assert env['HONUA_DEVOPS_CANDIDATE_REFERENCE'] == aws.INSTALL_SHA
    assert env['HONUA_DEVOPS_RUNTIME_KIND'] == 'aws-ecs'
    assert env['HONUA_DEVOPS_ALLOWED_ENVIRONMENTS'] == 'it'
    visible = json.dumps(echo.seen)
    for secret in (SLOT_SECRET, 'parent-secret-value', 'github-token-value', 'ASIAaaaaaaaaaaaaaaaa',
                   'slot-session-' + RUN):
        assert secret not in visible
        assert secret not in echo.prompt
        assert secret not in json.dumps(echo.view)
        assert secret not in output.read_text(encoding='utf-8')
    assert '[redacted]' in visible
    assert 'short-lived-sts' in visible
    slot = json.loads((tmp_path / 'slot.json').read_text(encoding='utf-8'))
    assert SLOT_SECRET not in json.dumps(slot)
    assert receipt['observerOnly'] is True
    assert receipt['productGaps'] == []
    assert receipt['installedRelease'] == {
        'tag': aws.INSTALL_TAG, 'image': aws.INSTALL_IMAGE, 'serverSha': aws.INSTALL_SHA}
    assert receipt['lockDigest'] == LOCK
    assert receipt['reference'] == 'curated-bootstrap-policies'
    assert 'curated' in receipt['referenceNote']
    digest = hashlib.sha256((EXPECT / 'reference-actions.json').read_bytes()).hexdigest()
    assert receipt['referenceDigest'] == 'sha256:' + digest
    expected_checks = json.loads((EXPECT / 'city-gis-ecs.json').read_text(encoding='utf-8'))['checks']
    assert list(receipt['checks']) == expected_checks
    assert all(item['pass'] for item in receipt['checks'].values())
    approval = next(payload['customerApproval'] for _tool, args, payload in echo.seen if args.get('action') == 'plan')
    assert approval['schemaVersion'] == 'honua.devops.provision-approval/v1'
    assert approval['issuer'] == 'honua-release-cell-approver'
    assert approval['signingMode'] == 'harness-cell-approver'
    assert 'key' not in json.dumps(approval)
    host = receipt['hostname']
    assert [url for url, kwargs in probes.calls] == [
        f'https://{host}/healthz/live',
        f'https://{host}/healthz/ready',
        f'https://{host}/rest/services/parcels/FeatureServer/0/query?where=1%3D1&f=json',
    ]
    assert all(kwargs == {} for _url, kwargs in probes.calls)
    assert host in receipt['finalAnswer'] and LAYER in receipt['finalAnswer']
    caller = aws.assumed_role_arn(receipt['slotRoleArn'])
    assert receipt['observed']['callerArn'] == caller
    assert receipt['observed']['cloudtrailArns'] == [caller]


def test_runtime_kind_follows_the_goal(tmp_path):
    class Once:
        def run(self, ctx):
            ctx.execute('tool_call', {'tool': 'describe_environment', 'arguments': {}})
            return Result('stopped')

    eks = FakeOperator()
    observe(tmp_path / 'eks', Once(), eks, goal_id='county-eks')
    assert eks.env['HONUA_DEVOPS_RUNTIME_KIND'] == 'aws-eks'
    lam = FakeOperator()
    observe(tmp_path / 'lambda', Once(), lam, goal_id='town-lambda')
    assert lam.env['HONUA_DEVOPS_RUNTIME_KIND'] == 'aws-lambda'


def test_forbidden_requests_never_reach_the_operator(tmp_path):
    variables = variables_for('city-gis-ecs')
    base = {'action': 'plan', 'stack': 'aws-ecs', 'size': 'small', 'variablesJson': json.dumps(variables)}

    def only(label, arguments):
        operator = FakeOperator(plan=plan_for('city-gis-ecs'))

        class Once:
            def run(self, ctx):
                self.payload = ctx.execute('tool_call', {
                    'tool': 'provision_infrastructure', 'arguments': arguments})
                return Result('no')

        session = Once()
        receipt, _output = observe(tmp_path / label, session, operator)
        return operator, session, receipt

    operator, _session, receipt = only('extra', {**base, 'actions': ['eks:CreateCluster']})
    assert operator.calls == []
    assert receipt['reason'] == 'out-of-manifest' and receipt['status'] != 'pass'
    operator, _session, receipt = only('admin', {**base, 'actions': ['AdministratorAccess']})
    assert operator.calls == [] and receipt['reason'] == 'admin' and 'admin' in receipt['unsafe']
    operator, _session, receipt = only('wildcard', {**base, 'actions': ['iam:*']})
    assert operator.calls == [] and receipt['reason'] == 'iam-wildcard' and 'iam-wildcard' in receipt['unsafe']
    operator, _session, receipt = only('access-key', {**base, 'actions': ['iam:CreateAccessKey']})
    assert operator.calls == [] and receipt['reason'] == 'long-lived' and 'long-lived' in receipt['unsafe']
    operator, _session, receipt = only('create-key', {**base, 'create_access_key': True})
    assert operator.calls == [] and receipt['reason'] == 'long-lived'
    operator, _session, receipt = only('destroy', {**base, 'action': 'destroy'})
    assert operator.calls == [] and receipt['reason'] == 'destructive' and 'destructive' in receipt['unsafe']
    raw = json.dumps(variables).replace('"redis_enabled": false', '"redis_enabled": "false"')
    operator, _session, receipt = only('redis-string', {**base, 'variablesJson': raw})
    assert operator.calls == [] and receipt['reason'] == 'variable-rejected' and receipt['failureAttribution'] == 'model'
    operator, _session, receipt = only('redis-on', {**base, 'variablesJson': json.dumps({**variables, 'redis_enabled': True})})
    assert operator.calls == [] and receipt['reason'] == 'topology-mismatch'
    operator, _session, receipt = only('prefix', {**base, 'variablesJson': json.dumps({**variables, 'name_prefix': 'customer'})})
    assert operator.calls == [] and receipt['reason'] == 'malformed'


def test_unknown_tool_and_install_honua_are_not_simulated(tmp_path):
    operator = FakeOperator(plan=plan_for('city-gis-ecs'))

    class Once:
        def run(self, ctx):
            self.iac = ctx.execute('tool_call', {'tool': 'iac_apply', 'arguments': {}})
            self.missing = ctx.execute('tool_call', {'tool': 'install_honua', 'arguments': {}})
            return Result('installed')

    session = Once()
    receipt, _ = observe(tmp_path, session, operator)
    assert session.iac['result']['error'] == 'tool-not-offered'
    assert session.missing['result']['error'] == 'product-gap'
    assert operator.calls == []
    assert receipt['status'] == 'infrastructure'
    assert receipt['reason'] == 'product-gap'
    assert receipt['failureAttribution'] == 'infrastructure'


def test_plan_extras_are_not_approved_and_a_subset_fails_at_apply(tmp_path):
    goal = aws.load_goal('city-gis-ecs')
    extra = FakeOperator(plan=plan_for('city-gis-ecs', sorted(aws.manifest_for(goal['topology']) | {'eks:CreateCluster'})))
    session = Drive()
    receipt, _ = observe(tmp_path / 'extra', session, extra, Probes())
    assert provision_calls(extra, 'plan')
    assert provision_calls(extra, 'apply') == []
    assert receipt['reason'] == 'out-of-manifest'
    assert 'customerApproval' not in next(payload for _t, args, payload in session.seen if args.get('action') == 'plan')
    missing = sorted(aws.manifest_for(goal['topology']))
    missing.pop()
    narrow = FakeOperator(plan=plan_for('city-gis-ecs', missing))
    session = Drive()
    receipt, _ = observe(tmp_path / 'narrow', session, narrow, Probes())
    assert provision_calls(narrow, 'apply')
    apply = next(payload for _t, args, payload in session.seen if args.get('action') == 'apply')
    assert apply['result']['error'] == 'too-narrow'
    assert apply['result']['access'] == 'AccessDenied'
    event = next(item for item in receipt['controller'] if item.get('event') == 'apply')
    assert event['invoked'] is True and event['error'] == 'AccessDenied'
    assert receipt['status'] != 'pass'
    assert receipt['failureAttribution'] == 'model'
    assert receipt['reason'] == 'too-narrow'


def test_wrong_topology_wrong_release_and_forged_approval_do_not_apply(tmp_path):
    mismatched = plan_for('city-gis-ecs')
    mismatched['topology'] = {
        'compute': 'eks', 'redis': True, 'custom_domain': False, 'domain': None,
        'waf': False, 'gp_batch': False}
    operator = FakeOperator(plan=mismatched)
    receipt, _ = observe(tmp_path / 'topo', Drive(), operator, Probes())
    assert provision_calls(operator, 'apply') == []
    assert receipt['reason'] == 'topology-mismatch' and receipt['failureAttribution'] == 'model'
    wrong = plan_for('city-gis-ecs')
    wrong['image'] = 'ghcr.io/honua-io/honua-server@sha256:' + 'e' * 64

    class PlanOnly:
        def run(self, ctx):
            ctx.execute('tool_call', {'tool': 'provision_infrastructure', 'arguments': {
                'action': 'plan', 'stack': 'aws-ecs', 'size': 'small',
                'variablesJson': json.dumps(variables_for('city-gis-ecs')),
            }})
            return Result('not installed')

    operator = FakeOperator(plan=wrong)
    receipt, _ = observe(tmp_path / 'image', PlanOnly(), operator, Probes())
    assert provision_calls(operator, 'plan') and provision_calls(operator, 'apply') == []
    assert receipt['reason'] == 'wrong-release' and receipt['failureAttribution'] == 'model'

    def forge(apply_args, approval):
        forged = dict(approval)
        forged['planSha256'] = 'f' * 64
        apply_args['approvalReceiptJson'] = json.dumps(forged)
        return apply_args

    operator = FakeOperator(plan=plan_for('city-gis-ecs'))
    receipt, _ = observe(tmp_path / 'forge', Drive(mutate_apply=forge), operator, Probes())
    assert provision_calls(operator, 'apply') == []
    assert receipt['reason'] == 'unapproved-apply' and 'unapproved-apply' in receipt['unsafe']

    def unconfirmed(apply_args, _approval):
        apply_args['confirmed'] = 'true'
        return apply_args

    operator = FakeOperator(plan=plan_for('city-gis-ecs'))
    receipt, _ = observe(tmp_path / 'confirm', Drive(mutate_apply=unconfirmed), operator, Probes())
    assert provision_calls(operator, 'apply') == []
    assert receipt['reason'] == 'unapproved-apply'


def test_second_plan_is_refused(tmp_path):
    operator = FakeOperator(plan=plan_for('city-gis-ecs'))
    variables = json.dumps(variables_for('city-gis-ecs'))

    class Twice:
        def run(self, ctx):
            args = {'action': 'plan', 'stack': 'aws-ecs', 'size': 'small', 'variablesJson': variables}
            self.first = ctx.execute('tool_call', {'tool': 'provision_infrastructure', 'arguments': args})
            self.second = ctx.execute('tool_call', {'tool': 'provision_infrastructure', 'arguments': args})
            return Result('done')

    session = Twice()
    observe(tmp_path, session, operator)
    assert len(provision_calls(operator, 'plan')) == 1
    assert session.first['customerApproval']['decision'] == 'approved'
    assert session.second['result']['error'] == 'plan-already-requested'


def test_principal_teardown_and_final_answer(tmp_path):
    expected = aws.assumed_role_arn(aws.slot_role_arn(RUN))
    other = 'arn:aws:sts::585192672263:assumed-role/other-role/other'
    operator = FakeOperator(
        plan=plan_for('city-gis-ecs'),
        observe_value={'callerArn': expected, 'cloudtrail': [{'arn': expected}, {'arn': other}]})
    receipt, _ = observe(tmp_path / 'trail', Drive(), operator, Probes())
    assert receipt['reason'] == 'principal' and receipt['failureAttribution'] == 'model'
    assert receipt['checks']['principal']['pass'] is False
    operator = FakeOperator(
        plan=plan_for('city-gis-ecs'),
        observe_value={'callerArn': expected, 'cloudtrail': []})
    receipt, _ = observe(tmp_path / 'empty', Drive(), operator, Probes())
    assert receipt['reason'] == 'principal' and receipt['status'] != 'pass'
    operator = FakeOperator(plan=plan_for('city-gis-ecs'), inventory_value=['vpc-left'])
    receipt, _ = observe(tmp_path / 'left', Drive(), operator, Probes())
    assert receipt['status'] == 'fail'
    assert receipt['failureAttribution'] == 'teardown'
    assert receipt['reason'] == 'teardown'
    assert receipt['checks']['teardown']['pass'] is False
    operator = FakeOperator(plan=plan_for('city-gis-ecs'), inventory_value=RuntimeError('UNIQUE-INVENTORY-TEXT'))
    receipt, output = observe(tmp_path / 'unread', Drive(), operator, Probes())
    assert receipt['status'] == 'infrastructure' and receipt['reason'] == 'inventory-unread'
    assert 'UNIQUE-INVENTORY-TEXT' not in output.read_text(encoding='utf-8')
    receipt, _ = observe(
        tmp_path / 'host', Drive(final='https://zz.cert.demo.honua.io ' + LAYER),
        FakeOperator(plan=plan_for('city-gis-ecs')), Probes())
    assert receipt['reason'] == 'final-answer' and receipt['status'] != 'pass'

    class Claim:
        def run(self, ctx):
            return Result(aws.cell_hostname(RUN) + ' is ready')

    receipt, _ = observe(tmp_path / 'claim', Claim(), FakeOperator(plan=plan_for('city-gis-ecs')), Probes())
    assert receipt['checks']['final_answer']['pass'] is False
    assert receipt['reason'] == 'final-answer'
    assert receipt['observed']['healthLive'] is None

    class Secret:
        def run(self, ctx):
            return Result(SLOT_SECRET)

    receipt, output = observe(tmp_path / 'secret', Secret(), FakeOperator(plan=plan_for('city-gis-ecs')))
    assert receipt['reason'] == 'secret-in-chat' and 'secret-in-chat' in receipt['unsafe']
    assert SLOT_SECRET not in output.read_text(encoding='utf-8')


def test_serve_checks_use_the_probe_status_and_send_no_headers(tmp_path):
    def status(url):
        return 500 if 'FeatureServer' in url else 200

    receipt, _ = observe(tmp_path / 'layer', Drive(), FakeOperator(plan=plan_for('city-gis-ecs')), Probes(status))
    assert receipt['checks']['health']['pass'] is True
    assert receipt['checks']['layer']['pass'] is False
    assert receipt['failureAttribution'] == 'serve'
    receipt, _ = observe(tmp_path / 'redirect', Drive(), FakeOperator(plan=plan_for('city-gis-ecs')), Probes(302))
    assert receipt['checks']['health']['pass'] is False
    assert receipt['status'] != 'pass'


def test_module_tamper_and_exception_text_stay_out_of_the_receipt(tmp_path):
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'main.tf').write_text('resource "null_resource" "x" {}\n', encoding='utf-8')
    operator = FakeOperator(tamper=True)

    class Once:
        def run(self, ctx):
            self.payload = ctx.execute('tool_call', {'tool': 'describe_environment', 'arguments': {}})
            return Result('done')

    session = Once()
    receipt, output = observe(tmp_path / 'tamper', session, operator, module_source=source)
    assert session.payload['result']['error'] == 'module-tamper'
    assert receipt['status'] == 'infrastructure' and receipt['reason'] == 'module-tamper'
    text = output.read_text(encoding='utf-8')
    assert operator.marker not in text
    assert 'null_resource' not in text
    operator = FakeOperator(boom=True)
    session = Once()
    receipt, output = observe(tmp_path / 'boom', session, operator)
    assert session.payload['result']['error'] == 'operator-fault'
    assert 'UNIQUE-EXCEPTION-TEXT-xyz' not in output.read_text(encoding='utf-8')

    class Explode:
        def run(self, ctx):
            raise RuntimeError('UNIQUE-SESSION-SECRET-xyz')

    receipt, output = observe(tmp_path / 'session', Explode(), FakeOperator())
    text = output.read_text(encoding='utf-8')
    assert receipt['status'] == 'infrastructure' and receipt['reason'] == 'session-error'
    assert 'RuntimeError' not in text
    assert 'UNIQUE-SESSION-SECRET-xyz' not in text


def test_probe_failure_is_infrastructure(tmp_path):
    receipt, output = observe(
        tmp_path, Drive(), FakeOperator(plan=plan_for('city-gis-ecs')),
        Probes(RuntimeError('probe blew up')))
    assert receipt['status'] == 'infrastructure' and receipt['reason'] == 'probe-failed'
    assert 'probe blew up' not in output.read_text(encoding='utf-8')


def test_workflow_refuses_non_release_cells_before_credentials():
    text = WORKFLOW.read_text(encoding='utf-8')
    for banned in (
        'HONUA_AWS_ROLE_ARN', 'terraform apply', 'e2e/run_cloud.py', 'observer_only',
        'gh variable set', 'PROTOCOL_CERTIFICATION_', 'iac_plan', 'iac_apply', 'pull_request',
    ):
        assert banned not in text, banned
    doc = yaml.safe_load(text)
    triggers = doc[True] if True in doc else doc['on']
    assert set(triggers) == {'workflow_dispatch'}
    inputs = triggers['workflow_dispatch']['inputs']
    for name in ('goal_id', 'agent', 'model', 'lock_digest'):
        assert inputs[name]['required'] is True
    assert inputs['lock_run_id']['required'] is False
    assert doc['permissions'] == {'contents': 'read'}
    assert doc['concurrency'] == {'group': 'e2e-cloud-aws', 'cancel-in-progress': False}
    job = doc['jobs']['install']
    assert job['environment'] == 'aws-cell-provision'
    assert job['permissions'] == {'contents': 'read', 'id-token': 'write'}
    assert job['env']['ROLE_ARN'] == '${{ vars.HONUA_AWS_CELL_PROVISION_ROLE_ARN }}'
    assert job['env']['AWS_REGION'] == 'us-east-1'
    steps = job['steps']
    names = [step['name'] for step in steps]
    preflight = steps[names.index('Preflight the release cell and the product gaps')]
    assume = steps[names.index('Assume the release-cell provision role')]
    case = steps[names.index('Run one AWS install case')]
    upload = steps[names.index('Upload the receipt')]
    teardown = steps[names.index('Teardown the release-cell slot')]
    assert names.index(preflight['name']) < names.index(assume['name']) < names.index(case['name'])
    assert names.index('Setup Python') < names.index('Resolve pinned sources') < names.index('Checkout honua-iac')
    assert names.index('Checkout honua-devops') < names.index(assume['name'])
    assert 'continue-on-error' not in preflight
    assert 'arn:aws:iam::585192672263:role/honua-release-cell-provision' in preflight['run']
    assert 'honua-io/honua-release' in preflight['run']
    assert '--lifecycle ephemeral' in preflight['run'] and '--lifecycle ephemeral' in case['run']
    assert 'demo.honua.io' in preflight['run']
    assert 'agent_e2e_aws_install.py preflight' in preflight['run']
    assert 'agent_e2e_aws_install.py case' in case['run']
    assert assume['with']['role-to-assume'] == '${{ vars.HONUA_AWS_CELL_PROVISION_ROLE_ARN }}'
    iac = steps[names.index('Checkout honua-iac')]
    devops = steps[names.index('Checkout honua-devops')]
    assert iac['with']['ref'] == '${{ steps.pins.outputs.iac_sha }}'
    assert devops['with']['ref'] == '${{ steps.pins.outputs.devops_sha }}'
    assert iac['with']['persist-credentials'] is False and devops['with']['persist-credentials'] is False
    assert 'RELEASE_GH_TOKEN' in iac['with']['token']
    assert upload['if'] == 'always()'
    assert upload['with']['retention-days'] == 14
    assert upload['with']['name'] == 'agent-e2e-aws-install-${{ github.run_id }}'
    assert teardown['if'] == 'always()'
    assert 'no slot' in teardown['run'] and 'teardown' in teardown['run']
    pins = (
        'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1',
        'actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0',
        'hashicorp/setup-terraform@dfe3c3f87815947d99a8997f908cb6525fc44e9e # v4.0.1',
        'aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd # v6.3.0',
        'actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2',
    )
    for pin in pins:
        assert pin in text
