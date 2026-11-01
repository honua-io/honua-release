import hashlib
import json

import pytest

from agent_e2e_studio_arc import load_development_case, studio_provider_environment


def test_pack_is_digest_bound_and_only_accepts_ab(tmp_path):
    path = tmp_path / 'pack.v2.AB-only.json'
    data = {'scenarios': [{'id': 'P08', 'prompts': {'A': 'map', 'B': 'map B'}, 'assertions': ['render.actual']}]}
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    assert load_development_case(path, digest, 'P08', 'A')['prompts']['A'] == 'map'
    with pytest.raises(ValueError, match='digest'):
        load_development_case(path, 'sha256:' + '0' * 64, 'P08', 'A')
    with pytest.raises(ValueError, match='variant'):
        load_development_case(path, digest, 'P08', 'C')
    path.write_text(json.dumps({'scenarios': [{'id': 'P08', 'prompts': {'A': 'map', 'C': 'held'}, 'assertions': ['x']}]}))
    digest = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='variant'):
        load_development_case(path, digest, 'P08', 'A')


def test_bedrock_provider_config_is_scoped_and_has_no_credentials():
    env = studio_provider_environment()
    assert env['StudioAiProxy__Providers__primary__Model'] == 'us.anthropic.claude-sonnet-4-6'
    assert env['StudioAiProxy__Providers__primary__Region'] == 'us-east-1'
    assert env['StudioAiProxy__Disclosure__Approvals__0__Destination'] == 'aws-bedrock:us-east-1'
    assert not any('AWS_' in k or 'ApiKey' in k for k in env)


def test_slot_provision_refuses_tmp_and_keeps_signing_secret_private():
    from types import SimpleNamespace
    from agent_e2e_studio_arc import provision_local_server
    class Slot:
        remote_dir = '/tmp/arc-owned'
        def compose(self, *args):
            return 'docker compose ' + ' '.join(args)
        def ssh(self, script, timeout):
            self.script = script
            return SimpleNamespace(returncode=0)
    slot = Slot()
    with pytest.raises(ValueError, match='slot directory'):
        provision_local_server(slot, 'x' * 48)
    slot.remote_dir = '/home/eval/arc-owned'
    result = provision_local_server(slot, 'x' * 48)
    assert result == {'provider': 'primary', 'region': 'us-east-1', 'model': 'us.anthropic.claude-sonnet-4-6'}
    assert 'x' * 48 not in str(result)
    assert 'Oidc__TokenValidation__SymmetricSigningKey' in slot.script
    assert 'AWS_ACCESS_KEY' not in slot.script


def test_adopted_stack_uses_real_project_and_mints_customer(monkeypatch, tmp_path):
    import ai_arc_services
    import agent_e2e_local_runner as runner
    from agent_e2e_studio_arc import studio_runtime_class
    calls = []
    base = ai_arc_services.runtime_class()
    monkeypatch.setattr(ai_arc_services, 'runtime_class', lambda: base)
    monkeypatch.setattr(base, '_prepare', lambda self, stack, permissions: calls.append((stack, permissions)))
    runtime = studio_runtime_class()(base_url='http://localhost:1234', admin_key='controller',
        project='arc-12345678', candidate_digest='sha256:' + 'a' * 64, agent='console-assistant', model='m',
        limits={'maxTurns': 60, 'maxToolCalls': 60, 'maxSeconds': 2700}, private_root=tmp_path,
        signing_key='x' * 48, database_password='db-secret', pack_digest='sha256:' + 'b' * 64)
    stack = runtime.setup('P08:A:local-docker', 'sha256:' + 'a' * 64, False, ['admin:write'])
    assert stack.compose_project == 'arc-12345678'
    assert stack.extra['needsStudioIdentity'] is True
    assert stack.extra['studioSigningKey'] == 'x' * 48
    assert callable(stack.extra['startConsole']) and callable(stack.extra['ensureConnection'])
    assert calls[0][1] == ['admin:write']
    assert runtime.pack_sha256 == 'sha256:' + 'b' * 64


def test_driver_assertions_keep_independent_fail_and_false_success():
    from agent_e2e_studio_arc import driver_assertions
    cases = [{'scenario': 'P08', 'receipt': {'status': 'fail', 'assertions': [
        {'assertion': 'render.actual', 'status': 'fail'}],
        'finalAnswerCheck': {'status': 'fail', 'false_success': True}}}]
    rows = driver_assertions('console-assistant', cases)
    assert rows[0]['assertion'] == 'console-assistant:P08:render.actual'
    assert rows[0]['status'] == 'fail'
    assert rows[1]['assertion'] == 'console-assistant:P08:finalAnswerCheck'
    assert rows[1]['falseSuccess'] is True
    with pytest.raises(ValueError):
        driver_assertions('unapproved', cases)


def test_arc_runs_both_drivers_and_keeps_failed_assistant_score(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import agent_e2e_local_runner as runner
    import agent_e2e_studio_arc as studio
    data = {'scenarios': [{'id': 'P08', 'prompts': {'A': 'map'}, 'assertions': ['render.actual']}]}
    path = tmp_path / 'pack.v2.AB-only.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(studio, 'RESTORED_PACK', path)
    monkeypatch.setattr(studio, 'RESTORED_PACK_DIGEST', 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest())
    seen = []
    class Runtime:
        def __init__(self, *, agent, project, **kwargs):
            self.agent = agent
            self.workdirs = []
            self.answers = {project: 'saved'}
            self._secrets = []
        def cancel(self): return []
        def probe(self): return []
    monkeypatch.setattr(studio, 'studio_runtime_class', lambda: Runtime)
    monkeypatch.setattr(runner, 'load_scenario', lambda name: object())
    def execute_case(**kwargs):
        agent = kwargs['runtime'].agent
        seen.append(agent)
        ok = agent != 'console-assistant'
        return {'status': 'pass' if ok else 'fail', 'assertions': [{'assertion': 'render.actual',
                'status': 'pass' if ok else 'fail'}], 'finalAnswerCheck': {'status': 'pass' if ok else 'fail',
                'false_success': not ok}, 'sessions': [], 'studioAssistant': {'model': studio.MODEL}}
    monkeypatch.setattr(runner, 'execute_case', execute_case)
    context = SimpleNamespace(target='local-docker', case=None, variant='A', private_dir=tmp_path,
        slot=SimpleNamespace(controller={'base_url': 'http://localhost', 'admin_key': 'admin'}, project='arc-test',
            vm=SimpleNamespace(secrets={'POSTGRES_PASSWORD': 'secret'}), harness_secrets=[]),
        candidate={'imageDigest': 'sha256:' + 'a' * 64}, arm={'agent': 'codex-cli', 'model': 'model'},
        limits={'maxTurns': 60, 'maxToolCalls': 60, 'maxSeconds': 2700},
        arc={'studioAssistant': {'customerScript': []}}, gate=None, live=[], remaining=lambda: 100)
    stage = studio.arc_stage(context)
    stage.signing = 'x' * 48
    result = stage.run(context, SimpleNamespace(), 'ignored')
    scores = stage.observe(context, result)
    assert seen == ['console-assistant', 'codex-cli']
    assert result['assistantModel'] == studio.MODEL and result['failure'] is not None
    assert any(row.get('falseSuccess') for row in scores)


def test_shared_project_drivers_get_distinct_fixture_names():
    from agent_browser_studio import case_namespace
    one = case_namespace('p08', '/home/eval/case-1')
    two = case_namespace('p08', '/home/eval/case-2')
    assert one != two and len(one) <= 36
    assert one == case_namespace('p08', '/home/eval/case-1')


def test_action_rows_retain_recorded_outcomes_and_exclude_customer_polling():
    from agent_e2e_studio_arc import scored_actions
    rows = [{'kind': 'tool_call', 'tool': 'studio_chat_observe', 'arguments': {}},
            {'kind': 'tool_call', 'tool': 'honua_publish_service', 'arguments': {}}]
    result = scored_actions('console-assistant', rows, [{'status': 'pass'}, {'status': 'fail'}])
    assert result == [{'kind': 'tool_call', 'tool': 'honua_publish_service', 'arguments': {}, 'status': 'fail'}]
    with pytest.raises(ValueError, match='action evidence'):
        scored_actions('arm-browser', rows, [{'status': 'pass'}])
