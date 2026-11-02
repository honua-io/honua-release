import hashlib
import json

import pytest

import agent_e2e_studio_arc
from agent_e2e_studio_arc import load_development_case


def test_development_pack_matches_arc_assertion_source():
    from ai_arc_runner import PACK_PATH
    from agent_e2e_studio_arc import RESTORED_PACK, RESTORED_PACK_DIGEST
    assert RESTORED_PACK == PACK_PATH
    assert RESTORED_PACK_DIGEST == 'sha256:' + hashlib.sha256(PACK_PATH.read_bytes()).hexdigest()


def test_arc_stage_exposes_the_runner_contract():
    from types import SimpleNamespace
    from agent_e2e_studio_arc import arc_stage
    stage = arc_stage(SimpleNamespace(target='local-docker'))
    for name in ('unavailable', 'sign_in_probe', 'prepare', 'notes', 'attachment', 'run', 'observe',
                 'handoff_observations', 'deployment_hosts', 'approvals', 'scope'):
        assert callable(getattr(stage, name, None))
    assert not hasattr(stage, 'assistant_principals'), 'ARC v2: stage 4 has no built-in assistant'


def test_synthetic_sealed_case_resolves_committed_fixture_and_frozen_expectations(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    from types import SimpleNamespace
    from agent_e2e_studio_arc import fixture_variant, select_arc_case
    cipher = Fernet(Fernet.generate_key())
    original = {'caseId': 'synthetic-case', 'target': 'local-docker', 'fixtures': 'town-B',
                'goals': {'studio': 'Create the requested map for this synthetic customer.'}}
    sealed = cipher.encrypt(json.dumps(original).encode())
    context = SimpleNamespace(case=json.loads(cipher.decrypt(sealed)), variant='H', target='local-docker',
                              candidate={'development': False})
    fixture, case = select_arc_case(context, context.case['goals']['studio'] + '\nPrevious model handoff')
    # ARC v2: the sealed goal plus the customer's closing status checklist (identical for both drivers).
    from ai_arc_safety import with_status_request
    assert fixture == 'B' and case['prompts']['B'] == with_status_request(context.case['goals']['studio'], 'P08')
    assert 'sharing' in __import__('agent_e2e_studio_arc').fixture_requirements(context.case['fixtures'], 'P08')
    with pytest.raises(ValueError, match='not bound'):
        select_arc_case(context, 'unrelated request')
    for scenario in ('P08', 'P09', 'P24'):
        assert fixture_variant('town-B', scenario)[0] == 'B'
    context.case['fixtures'] = 'unknown-fixture'
    with pytest.raises(ValueError, match='fixture'):
        select_arc_case(context)
    context.case['fixtures'] = 'town-B'
    context.candidate['development'] = True
    with pytest.raises(ValueError, match='release candidate'):
        select_arc_case(context)
    import agent_e2e_local_expectations as expectations
    monkeypatch.setattr(expectations, 'load_expected', lambda *_args: {})
    with pytest.raises(ValueError, match='frozen Studio expectations'):
        fixture_variant('town-A', 'P08')
    bad_catalog = tmp_path / 'catalog.json'
    bad_catalog.write_text('{}')
    with pytest.raises(ValueError, match='catalog digest'):
        fixture_variant('town-A', 'P08', manifest_path=bad_catalog)


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


def test_no_studio_model_provider_is_configured():
    """Founder 2026-11-01: ARC v2 has no Studio built-in assistant, no Sonnet and no Bedrock."""
    assert not hasattr(agent_e2e_studio_arc, 'studio_provider_environment')
    assert not hasattr(agent_e2e_studio_arc, 'MODEL')


def test_slot_provision_refuses_tmp_and_keeps_signing_secret_private():
    from types import SimpleNamespace
    from agent_e2e_studio_arc import provision_local_server
    class Slot:
        remote_dir = '/tmp/arc-owned'
        project = 'arc-12345678'
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
    assert result == {'studioAiProxy': 'disabled'}
    assert 'x' * 48 not in str(result)
    assert 'Oidc__TokenValidation__SymmetricSigningKey' in slot.script
    assert 'AWS_ACCESS_KEY' not in slot.script
    assert "StudioAiProxy__Enabled: 'false'" in slot.script
    assert 'AWS_REGION' not in slot.script and 'bedrock' not in slot.script.lower()
    with pytest.raises(TypeError):
        provision_local_server(slot, 'x' * 48, enable_assistant=True)


def test_adopted_stack_uses_real_project_and_mints_customer(monkeypatch, tmp_path):
    import ai_arc_services
    import agent_e2e_local_runner as runner
    from agent_e2e_studio_arc import studio_runtime_class
    calls = []
    base = ai_arc_services.runtime_class()
    monkeypatch.setattr(ai_arc_services, 'runtime_class', lambda: base)
    monkeypatch.setattr(base, '_prepare', lambda self, stack, permissions: calls.append((stack, permissions)))
    runtime = studio_runtime_class()(base_url='http://localhost:1234', admin_key='controller',
        project='arc-12345678', candidate_digest='sha256:' + 'a' * 64, agent='codex-cli', model='m',
        limits={'maxTurns': 60, 'maxToolCalls': 60, 'maxSeconds': 2700}, private_root=tmp_path,
        signing_key='x' * 48, database_password='db-secret', pack_digest='sha256:' + 'b' * 64)
    stack = runtime.setup('P08:A:local-docker', 'sha256:' + 'a' * 64, False, ['admin:write'])
    assert stack.compose_project == 'arc-12345678'
    assert stack.extra['needsStudioIdentity'] is True
    assert stack.extra['studioSigningKey'] == 'x' * 48
    import base64
    readers = stack.extra['studioReaderSubjects']
    assert len(set(readers.values())) == 1
    tokens = [value for value in runtime._secrets if isinstance(value, str) and value.startswith('Bearer ')]
    claims = [json.loads(base64.urlsafe_b64decode((payload := token.split('.')[1]) + '=' * (-len(payload) % 4)))
              for token in tokens]
    assert {row['sub'] for row in claims} == set(readers.values())
    assert all(row['scope'] == 'honua.mcp.read' for row in claims)
    assert {row['sub']: row['roles'] for row in claims} == {readers['colleague']: ['operator']}
    assert callable(stack.extra['startConsole']) and callable(stack.extra['ensureConnection'])
    assert calls[0][1] == ['admin:write']
    assert runtime.pack_sha256 == 'sha256:' + 'b' * 64
    assert not hasattr(runtime, 'register_assistant_actors') and not hasattr(runtime, 'assistant_actors')


def test_driver_assertions_keep_independent_fail_and_false_success():
    from agent_e2e_studio_arc import driver_assertions
    cases = [{'scenario': 'P08', 'receipt': {'status': 'fail', 'assertions': [
        {'assertion': 'render.actual', 'status': 'fail'}],
        'finalAnswerCheck': {'status': 'fail', 'false_success': True}}}]
    rows = driver_assertions('arm-browser', cases)
    assert rows[0]['assertion'] == 'arm-browser:P08:render.actual'
    assert rows[0]['status'] == 'fail'
    assert rows[1]['assertion'] == 'arm-browser:P08:finalAnswerCheck'
    # ARC v2: the corpus verdict is kept for reference; the arc re-scores the row from the case's own answer.
    assert rows[1]['corpusAnswerCheck'] == {'status': 'fail', 'false_success': True} and 'answer' in rows[1]
    for driver in ('unapproved', 'console-assistant'):
        with pytest.raises(ValueError):
            driver_assertions(driver, cases)


@pytest.mark.parametrize('legacy_only', [False, True])
@pytest.mark.parametrize('sealed', [False, True])
def test_arc_runs_only_the_arm_in_studio_and_keeps_its_failed_score(monkeypatch, tmp_path, sealed, legacy_only):
    """ARC v2 (founder 2026-11-01): stage 4 runs the CLI arm itself in Studio in Console, once; no built-in
    assistant case, no model provider provisioning, no assistant model, region or usage in the stage row."""
    from types import SimpleNamespace
    import agent_e2e_local_runner as runner
    import agent_e2e_studio_arc as studio
    data = {'scenarios': [{'id': 'P08', 'prompts': {'A': 'map', 'B': 'map B'}, 'fixtureIds': ['synthetic-town'],
                           'assertions': ['render.actual']}]}
    path = tmp_path / 'pack.v2.AB-only.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(studio, 'RESTORED_PACK', path)
    monkeypatch.setattr(studio, 'RESTORED_PACK_DIGEST', 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest())
    seen, provisioned = [], []

    class Runtime:
        def __init__(self, *, agent, project, **kwargs):
            self.agent = agent
            self.workdirs = []
            self.answers = {project: 'saved'}
            self._secrets = []
            self.customer_subjects = {agent + '-customer'}
            self.customer_keys = {agent + '-key'}

        def cancel(self): return []

        def probe(self): return []
    monkeypatch.setattr(studio, 'studio_runtime_class', lambda: Runtime)
    monkeypatch.setattr(studio, 'provision_local_server', lambda *a, **k: provisioned.append((a, k)))
    monkeypatch.setattr(runner, 'load_scenario', lambda name: object())

    def execute_case(**kwargs):
        if sealed:
            assert kwargs['variant'] == 'B'
            from ai_arc_safety import with_status_request
            assert kwargs['case']['prompts']['B'] == with_status_request('synthetic held-out request', 'P08')
        seen.append(kwargs['runtime'].agent)
        # legacy_only: live state passed and only the corpus prose answer check failed (Sol 1203 item 9).
        return {'status': 'fail', 'assertions': [{'assertion': 'render.actual',
                'status': 'pass' if legacy_only else 'fail'}], 'finalAnswerCheck': {'status': 'fail',
                'false_success': True}, 'sessions': []}
    monkeypatch.setattr(runner, 'execute_case', execute_case)
    context = SimpleNamespace(target='local-docker', case=None, variant='A', private_dir=tmp_path,
        slot=SimpleNamespace(controller={'base_url': 'http://localhost', 'admin_key': 'admin'}, project='arc-test',
            vm=SimpleNamespace(secrets={'POSTGRES_PASSWORD': 'secret'}), harness_secrets=[]),
        candidate={'imageDigest': 'sha256:' + 'a' * 64}, arm={'agent': 'codex-cli', 'model': 'model'},
        limits={'maxTurns': 60, 'maxToolCalls': 60, 'maxSeconds': 2700},
        arc={'targets': {'local-docker': {'goals': {'studio': {'A': 'ignored'}}}}}, gate=None, live=[],
        remaining=lambda: 100)
    stage = studio.arc_stage(context)
    stage.signing = 'x' * 48
    if sealed:
        context.variant = 'H'
        context.case = {'target': 'local-docker', 'fixtures': 'town-B',
                        'goals': {'studio': 'synthetic held-out request'}}
    request = 'synthetic held-out request' if sealed else 'ignored'
    result = stage.run(context, SimpleNamespace(), request)
    scores = stage.observe(context, result)
    assert seen == ['codex-cli'] and provisioned == []
    assert not any(k.startswith('assistant') for k in result)
    assert (result['failure'] is None) is legacy_only, 'only live-state failure is a model failure in ARC v2'
    assert any(row.get('corpusAnswerCheck', {}).get('false_success') for row in scores)
    assert [row['assertion'] for row in scores if 'answer' in row] == ['arm-browser:P08:finalAnswerCheck']
    assert all(row['assertion'].startswith('arm-browser:') for row in scores)
    assert {r['assertion'] for r in scores if r['assertion'].endswith(':goal.services-lineage')
            and r['status'] == 'fail'} == {'arm-browser:goal.services-lineage'}


def test_shared_project_drivers_get_distinct_fixture_names():
    from agent_browser_studio import case_namespace
    one = case_namespace('p08', '/home/eval/case-1')
    two = case_namespace('p08', '/home/eval/case-2')
    assert one != two and len(one) <= 36
    assert one == case_namespace('p08', '/home/eval/case-1')


def test_action_rows_retain_recorded_outcomes_and_exclude_customer_polling():
    from agent_e2e_studio_arc import scored_actions
    rows = [{'kind': 'tool_call', 'tool': 'studio_ui_save', 'arguments': {}},
            {'kind': 'tool_call', 'tool': 'honua_publish_service', 'arguments': {}}]
    result = scored_actions('arm-browser', rows, [{'status': 'pass'}, {'status': 'fail'}])
    assert result == [{'kind': 'tool_call', 'tool': 'honua_publish_service', 'arguments': {}, 'status': 'fail'}]
    assert scored_actions('arm-browser', [{'tool': 'studio_product_action'}],
                          [{'status': 'pass'}])[0]['tool'] == 'studio_product_action'
    with pytest.raises(ValueError, match='driver'):
        scored_actions('console-assistant', rows, [{'status': 'pass'}, {'status': 'pass'}])
    with pytest.raises(ValueError, match='action evidence'):
        scored_actions('arm-browser', rows, [{'status': 'pass'}])


def test_arc_closes_browser_at_gate_and_only_once():
    from agent_e2e_studio_arc import close_browser_on_gate
    class Browser:
        _poisoned = False
        def __init__(self): self.calls = 0
        def stop(self): self.calls += 1
    browser = Browser()
    guard = close_browser_on_gate(browser)
    guard.close_gate()
    browser.stop()
    guard.close()
    assert browser.calls == 1


def test_closed_stage_blocks_browser_and_network_before_dispatch():
    from types import SimpleNamespace
    from agent_browser_studio import BrowserUnavailable, PlaywrightPage, StudioBrowserServer
    gate = SimpleNamespace(closed=True)
    browser = StudioBrowserServer('http://localhost:1234', None, '.', stage_gate=gate)
    with pytest.raises(BrowserUnavailable, match='stage has ended'):
        browser.perform('studio_click', {})
    class Route:
        request = SimpleNamespace(url='http://localhost:1234/any')
        aborted = None
        def abort(self, reason): self.aborted = reason
    route = Route()
    page = PlaywrightPage.__new__(PlaywrightPage)
    page._stage_gate = gate
    page._gate(route)
    assert route.aborted == 'blockedbyclient'


def test_admission_initializes_and_resumes_only_completed_operations():
    from agent_e2e_studio_arc import initialize_admission
    class Recorder:
        def __init__(self, status): self.status, self.calls = status, []
        def json(self, step, method, path, body):
            self.calls.append((step, method, path, body))
            return {'observation': {'httpStatus': 200, 'body': {'data': {'status': self.status}}}}
    recorder = Recorder('Completed')
    initialize_admission(recorder)
    assert [x[2] for x in recorder.calls] == ['/api/v1/operations/admin.ai.admission.initialize/submit',
                                              '/api/v1/operations/admin.ai.admission.resume/submit']
    assert 'policy' in recorder.calls[0][3]['parameters']
    recorder = Recorder('RequiresApproval')
    with pytest.raises(ValueError, match='admission'):
        initialize_admission(recorder)
    assert len(recorder.calls) == 1


def test_studio_source_reads_use_adopted_database_identity(monkeypatch):
    from types import SimpleNamespace
    import subprocess
    from agent_e2e_studio_arc import studio_runtime_class
    runtime_type = studio_runtime_class()
    runtime = runtime_type.__new__(runtime_type)
    runtime.database_host = 'postgres'
    runtime.database_name = 'customer_database'
    runtime.database_user = 'customer_owner'
    runtime._ensure_reader = lambda stack: None
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='db-container\n' if command[1] == 'ps' else b'[]')
    monkeypatch.setattr(subprocess, 'run', run)
    stack = SimpleNamespace(compose_project='arc-customer')
    assert runtime.psql(stack, 'SELECT 1', read_only=True) == (0, b'[]')
    assert 'label=com.docker.compose.service=postgres' in calls[0]
    assert 'label=com.docker.compose.project=arc-customer' in calls[0]
    assert calls[1][calls[1].index('-U')+1] == 'corpus_reader'
    assert calls[1][calls[1].index('-d')+1] == 'customer_database'
    assert 'default_transaction_read_only=on' in ' '.join(calls[1])
    runtime.psql(stack, 'SELECT 1', read_only=False)
    assert calls[-1][calls[-1].index('-U')+1] == 'customer_owner'


def test_studio_arc_contract_is_in_runner_digest_and_does_not_replace_p08():
    import ai_arc_runner as arc
    from agent_e2e_local_expectations import load_roles, load_projections
    assert 'certification/ai-arc/studio-arc-expectations.json' in arc.RUNNER_FILES
    assert any(r.role.startswith('seeded:') for r in load_roles('P08'))
    assert not any(r.role.startswith('seeded:') for r in load_roles('P08', 'studio-arc'))
    for variant in ('A', 'B'):
        assert 'studioArcContent' in load_projections('P08', variant, 'studio-arc')['source-content.unchanged']['spec']


def test_slot_provision_keeps_the_packages_compose_files():
    """Studio DEV shakeout 11-02: "-f compose.yaml -f studio-customer.yaml" dropped the package's executor overlay
    (AllowedHosts admitting Console's calls to honua) and image override; the slot's COMPOSE_FILE is kept."""
    from types import SimpleNamespace
    from agent_e2e_studio_arc import provision_local_server

    class Slot:
        remote_dir = '/home/eval/arc-owned'
        project = 'arc-12345678'

        def compose(self, *args):
            return 'docker compose ' + ' '.join(args)

        def ssh(self, script, timeout):
            self.script = script
            return SimpleNamespace(returncode=0)
    slot = Slot()
    provision_local_server(slot, 'x' * 48)
    assert 'sed -n "s/^COMPOSE_FILE=//p" .env' in slot.script
    assert 'export COMPOSE_FILE="$files:studio-customer.yaml"' in slot.script
    assert 'docker compose -f' not in slot.script and 'up -d --no-deps --wait --wait-timeout 240 honua' in slot.script
