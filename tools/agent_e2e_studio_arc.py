"""Studio stage helpers for the arc's runner-owned customer and Console."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent_e2e_studio_customer_session import MODEL

CORPUS_MANIFEST = Path(__file__).resolve().parents[1] / 'certification/agent-e2e/corpus/manifest.v1.json'
CORPUS_MANIFEST_SHA256 = '166fad2a58cdfb5560882181a1c0afe04b81442401a1b465d28d6dc9710f846e'


def fixture_variant(fixture_id, scenario_id, *, manifest_path=CORPUS_MANIFEST):
    """Resolve a committed corpus fixture ID to its frozen local observer variant."""
    from agent_e2e_local_expectations import load_expected, load_projections
    if scenario_id not in ('P08', 'P09', 'P24') or not isinstance(fixture_id, str):
        raise ValueError('unsupported Studio fixture or scenario')
    raw = Path(manifest_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != CORPUS_MANIFEST_SHA256:
        raise ValueError('Studio fixture catalog digest differs')
    manifest = json.loads(raw)
    matches = [(row.get('id'), variant) for row in manifest.get('fixtures', []) if row.get('committed') is not False
               for variant, spec in (row.get('variants') or {}).items()
               if variant in ('A', 'B') and isinstance(spec, dict) and spec.get('path') == fixture_id]
    if len(matches) != 1 or matches[0][0] != 'synthetic-town':
        raise ValueError('fixture has no unique committed Studio expectations')
    variant = matches[0][1]
    case = load_development_case(RESTORED_PACK, RESTORED_PACK_DIGEST, scenario_id, variant)
    if matches[0][0] not in case.get('fixtureIds', []):
        raise ValueError('fixture is not declared for this Studio scenario')
    expected, projections = load_expected(scenario_id, variant), load_projections(scenario_id, variant)
    if not set(case['assertions']) <= set(expected) or not set(case['assertions']) <= set(projections):
        raise ValueError('fixture lacks frozen Studio expectations or projections')
    return variant, case


def select_arc_case(context, prompt=None):
    if context.case is None:
        if context.variant not in ('A', 'B'):
            raise ValueError('invalid development variant')
        return fixture_variant('town-' + context.variant, 'P08')
    if context.variant != 'H' or context.candidate.get('development'):
        raise ValueError('held-out Studio requires a release candidate')
    if not isinstance(context.case, dict) or context.case.get('target') != context.target:
        raise ValueError('held-out Studio case target differs')
    goal = (context.case.get('goals') or {}).get('studio')
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError('held-out Studio goal is unavailable')
    variant, case = fixture_variant(context.case.get('fixtures'), 'P08')
    if prompt is not None:
        if not isinstance(prompt, str) or goal not in prompt:
            raise ValueError('held-out Studio prompt is not bound to the sealed goal')
        case = {**case, 'prompts': {**case['prompts'], variant: prompt}}
    return variant, case


def load_development_case(path, digest, scenario, variant):
    """Load only an explicitly pinned A/B development pack, never held-out prompts."""
    path = Path(path)
    if path.name != 'pack.v2.AB-only.json' or variant not in ('A', 'B'):
        raise ValueError('invalid development pack or variant')
    raw = path.read_bytes()
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('development pack digest mismatch')
    pack = json.loads(raw)
    matches = [row for row in pack.get('scenarios', []) if row.get('id') == scenario]
    if len(matches) != 1:
        raise ValueError('missing or duplicate scenario')
    case = matches[0]
    prompts = case.get('prompts')
    if (not isinstance(prompts, dict) or set(prompts) - {'A', 'B'}
            or not isinstance(prompts.get(variant), str) or not prompts[variant]
            or not isinstance(case.get('assertions'), list) or not case['assertions']):
        raise ValueError('invalid development variant or assertions')
    return case


def studio_provider_environment():
    """Customer's explicit Bedrock provider and disclosure approval; no credentials."""
    prefix = 'StudioAiProxy__'
    return {prefix + key: value for key, value in {
        'Enabled': 'true', 'DefaultProvider': 'bedrock',
        'Providers__bedrock__Kind': 'bedrock', 'Providers__bedrock__Model': MODEL,
        'Providers__bedrock__Region': 'us-east-1', 'Providers__bedrock__SupportsTools': 'true',
        'Disclosure__WorkloadDataClassification': 'unclassified',
        'Disclosure__Approvals__0__Provider': 'bedrock', 'Disclosure__Approvals__0__Kind': 'bedrock',
        'Disclosure__Approvals__0__Destination': 'aws-bedrock:us-east-1',
        'Disclosure__Approvals__0__Model': MODEL, 'Disclosure__Approvals__0__UseCase': 'studio-chat',
        'Disclosure__Approvals__0__DataClassification': 'unclassified',
    }.items()}


def initialize_admission(recorder):
    """Use the documented operator operations; do not bypass pending approval."""
    policy = {'windowSeconds': 60, 'maxOperationSeconds': 300, 'deploymentAttempts': 120,
              'actorAttempts': 60, 'cohortAttempts': 60, 'workflowAttempts': 60,
              'deploymentConcurrency': 8, 'actorConcurrency': 4, 'cohortConcurrency': 2,
              'workflowConcurrency': 1}
    for action, params in (('initialize', {'policy': json.dumps(policy, separators=(',', ':'))}),
                           ('resume', {})):
        response = recorder.json('studio-admission-' + action, 'POST',
            '/api/v1/operations/admin.ai.admission.' + action + '/submit', {'parameters': params})['observation']
        body = response.get('body')
        data = body.get('data') if isinstance(body, dict) else None
        if response.get('httpStatus') != 200 or not isinstance(data, dict) or data.get('status') != 'Completed':
            raise ValueError('Studio admission ' + action + ' was not completed')


def provision_local_server(slot, signing_key, *, enable_assistant=False):
    """Configure the existing local stack for a customer identity and Bedrock provider.

    The slot's published Compose stays intact; the private override is applied to
    the same server and network. No cloud credentials are read or passed here.
    """
    import re
    import secrets
    import shlex
    import yaml

    remote = Path(slot.remote_dir)
    if not remote.is_absolute() or '..' in remote.parts or remote == Path('/tmp') or Path('/tmp') in remote.parents:
        raise ValueError('invalid slot directory')
    if not isinstance(signing_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', signing_key):
        raise ValueError('invalid customer signing authority')
    env = {**(studio_provider_environment() if enable_assistant else {'StudioAiProxy__Enabled': 'false'}),
           'Oidc__Enabled': 'true',
           'Oidc__Generic__Enabled': 'true', 'Oidc__Generic__Authority': 'https://terminal-journey.invalid',
           'Oidc__Generic__ClientId': 'terminal-journey',
           'Oidc__TokenValidation__SymmetricSigningKey': signing_key,
           'Studio__EndUserAuthorization__Enabled': 'true',
           'AiExecutionAdmission__Backend': 'postgres', 'AiExecutionAdmission__DeploymentId': slot.project,
           'AWS_REGION': 'us-east-1'}
    override = yaml.safe_dump({'services': {'honua': {'image': '${HONUA_STUDIO_ACTIVE_IMAGE}',
                                                        'environment': env}}}, sort_keys=True)
    fence = 'HONUA_STUDIO_' + secrets.token_hex(12)
    script = ('set -eu\numask 077\ncd -- ' + shlex.quote(str(remote)) + '\n'
              + 'server_id=$(' + slot.compose('-f', 'compose.yaml', 'ps', '-q', 'honua') + ')\n'
              + '[ -n "$server_id" ]\n'
              + 'HONUA_STUDIO_ACTIVE_IMAGE=$(docker inspect --format "{{.Image}}" "$server_id")\n'
              + 'case "$HONUA_STUDIO_ACTIVE_IMAGE" in sha256:*) ;; *) exit 1 ;; esac\n'
              + 'export HONUA_STUDIO_ACTIVE_IMAGE\n'
              + f"cat > studio-customer.yaml <<'{fence}'\n{override}{fence}\n"
              + slot.compose('-f', 'compose.yaml', '-f', 'studio-customer.yaml', 'up', '-d', '--no-deps',
                             '--wait', '--wait-timeout', '240', 'honua') + '\n')
    if slot.ssh(script, timeout=300).returncode:
        raise RuntimeError('could not configure the local Studio server')
    return {'provider': 'bedrock', 'region': 'us-east-1', 'model': MODEL}


def studio_runtime_class():
    """Adopt the arc's actual Compose network while reusing corpus custody and CaseConsole."""
    import os
    import tempfile
    import ai_arc_services
    from agent_e2e_local_api import Stack
    base = ai_arc_services.runtime_class()

    class StudioRuntime(base):
        def __init__(self, *, signing_key, database_password, pack_digest, database_name='honua',
                     database_user='honua', database_host='postgres', **kwargs):
            super().__init__(**kwargs)
            self.signing_key = signing_key
            self.database_password = database_password
            self.database_name = database_name
            self.database_user = database_user
            self.database_host = database_host
            self.pack_sha256 = pack_digest
            self._secrets.extend((signing_key, database_password))
            self.extra_secrets.extend((signing_key, database_password))
            self.assistant_actors = set()
            self.customer_subjects = set()
            self.customer_keys = set()

        def mint_console_identity(self, stack, agent_key_id, minted_by, subject):
            if (subject in getattr(self, 'disallowed_subjects', set())
                    or agent_key_id in getattr(self, 'disallowed_keys', set())):
                raise ValueError('Studio assistant and CLI customers are not distinct')
            super().mint_console_identity(stack, agent_key_id, minted_by, subject)
            self.customer_subjects.add(subject)
            self.customer_keys.add(agent_key_id)

        def register_assistant_actors(self, actors):
            if self.agent != 'console-assistant' or not actors or any(not isinstance(a, str) or not a for a in actors):
                raise ValueError('invalid assistant audit actors')
            self.assistant_actors.update(actors)

        def register_studio_browser(self, browser):
            register = getattr(self, 'browser_register', None)
            if callable(register):
                guard = close_browser_on_gate(browser)
                register(guard)
                if self.gate is not None and self.gate.closed:
                    guard.close_gate()
                    raise RuntimeError('Studio stage closed while the browser was starting')

        def setup(self, case_id, candidate_digest, needs_approver=False, agent_permissions=('admin:write',)):
            if candidate_digest != self.arc['candidate'] or needs_approver:
                raise ValueError('Studio case does not bind the candidate or needs an unsupported approver')
            if case_id.split(':', 1)[0] not in ('P08', 'P09', 'P24'):
                raise ValueError('unsupported Studio case')
            case_dir = Path(tempfile.mkdtemp(prefix='studio-case-', dir=self.arc['private_root']))
            os.chmod(case_dir, 0o700)
            self.workdirs.append(str(case_dir))
            os.environ[self.ADMIN_ENV] = self.arc['admin_key']
            self._secrets.append(self.arc['admin_key'])
            stack = Stack(base_url=self.arc['base_url'], admin_key=self.arc['admin_key'], approver_key=None,
                          compose_project=self.arc['project'], compose_file='', workdir=str(case_dir),
                          candidate_digest=candidate_digest, extra={'needsStudioIdentity': True,
                                                                   'studioSigningKey': self.signing_key,
                                                                   'serverHost': 'honua',
                                                                   'studioGate': self.gate})
            self._prepare(stack, list(agent_permissions))
            self.stacks.append(stack)
            if getattr(self, 'initialize_studio_admission', False):
                initialize_admission(stack.extra['recorder'])
            stack.extra['startConsole'] = lambda: self.start_console(stack)
            stack.extra['ensureConnection'] = lambda: self.fixture_connection(stack)
            for value in self._secrets:
                if value not in self.extra_secrets:
                    self.extra_secrets.append(value)
            return stack

        def fixture_connection(self, stack):
            rec = stack.extra['recorder']
            body = {'name': 'studio-' + Path(stack.workdir).name, 'host': self.database_host, 'port': 5432,
                    'databaseName': self.database_name, 'username': self.database_user,
                    'password': self.database_password, 'sslRequired': False, 'sslMode': 'Disable'}
            result = rec.json('studio-fixture-connection', 'POST', '/api/v1/admin/connections/', body)['observation']
            data = result.get('body', {}).get('data', {})
            if result.get('httpStatus') != 201 or not isinstance(data.get('connectionId'), str):
                raise ValueError('could not create the Studio fixture connection')
            return data['connectionId']

        def teardown(self, stack):
            console = stack.extra.pop('_caseConsole', None)
            try:
                if console is not None:
                    console.close()
            finally:
                self._console_identities.pop(stack.compose_project, None)
                for name in (self.AGENT_ENV, self.BOOTSTRAP_ENV, 'HONUA_CORPUS_USER_BEARER'):
                    os.environ.pop(name, None)

    return StudioRuntime


def close_browser_on_gate(browser):
    """Make arc shutdown stop an active browser before it revokes credentials."""
    import threading
    lock = threading.Lock()
    original = browser.stop
    stopped = False

    def stop():
        nonlocal stopped
        with lock:
            if not stopped:
                original()
                stopped = True
                if browser._poisoned:
                    raise RuntimeError('Studio browser did not stop cleanly')
    browser.stop = stop

    class Guard:
        close_gate = staticmethod(stop)
        close = staticmethod(stop)
    return Guard()


def driver_assertions(driver, cases):
    """Retain the corpus runner's independent scores and false-success finding."""
    from ai_arc_services import case_assertions
    if driver not in ('console-assistant', 'arm-browser'):
        raise ValueError('invalid Studio driver')
    return [{**row, 'assertion': driver + ':' + row['assertion']} for row in case_assertions(cases)]


def scored_actions(driver, actions, receipts):
    """Join private requests to their runner-recorded outcomes before filtering."""
    from agent_browser_studio import TOOLS
    if len(actions) != len(receipts):
        raise ValueError('Studio action evidence is incomplete')
    joined = []
    for action, receipt in zip(actions, receipts):
        if receipt.get('status') not in ('pass', 'fail', 'unknown'):
            raise ValueError('Studio action evidence has an invalid outcome')
        if driver == 'console-assistant' and action.get('tool') in (*TOOLS, 'studio_chat_observe',
                                                                     'studio_customer_type'):
            continue
        joined.append({**action, 'status': receipt['status'],
                       **({'refusal': 'out-of-scope'} if receipt.get('refusal') == 'out-of-scope' else {})})
    return joined


from agent_e2e_local_runner import PACK as RESTORED_PACK, PACK_SHA256 as RESTORED_PACK_DIGEST


def arc_stage(ctx):
    """#1177 entry point: return a local Studio stage over the arc-owned candidate."""
    if ctx.target != 'local-docker':
        return None
    import secrets
    import time
    import ai_arc_runner as arc
    import ai_arc_services as services
    import agent_e2e_local_runner as runner
    import agent_e2e_cli_sessions  # noqa: F401 -- register the CLI session implementations

    class StudioStage(arc.StageModule):
        stage = 'studio'
        lane = 'studio customer and browser'

        def __init__(self):
            self.cases = {}
            self.signing = None
            self.runtimes = []
            self.assistant_actors = set()

        def unavailable(self, context):
            if not context.slot.vm or not context.slot.controller.get('base_url') or not context.slot.controller.get('admin_key'):
                return 'the local candidate and controller are unavailable'
            if not context.slot.vm.secrets.get('POSTGRES_PASSWORD'):
                return 'the customer database fixture authority is unavailable'
            try:
                select_arc_case(context)
            except (OSError, ValueError, KeyError):
                return 'the Studio fixture or its frozen expectations are unavailable'
            return None

        def _runtime(self, context, driver):
            agent = 'console-assistant' if driver == 'console-assistant' else context.arm['agent']
            model = MODEL if driver == 'console-assistant' else context.arm['model']
            root = context.private_dir / 'studio' / driver / 'cases'
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            runtime = studio_runtime_class()(
                base_url=context.slot.controller['base_url'], admin_key=context.slot.controller['admin_key'],
                project=context.slot.project, candidate_digest=context.candidate['imageDigest'], agent=agent,
                model=model, limits=context.limits, private_root=root, extra_secrets=context.slot.harness_secrets,
                signing_key=self.signing, database_password=context.slot.vm.secrets['POSTGRES_PASSWORD'],
                pack_digest=RESTORED_PACK_DIGEST)
            runtime.studio_customer_script = context.arc['studioAssistant']['customerScript']
            runtime.gate = context.gate
            runtime.browser_register = context.live.append
            context.live.append(services.Canceller(runtime))
            self.runtimes.append(runtime)
            return runtime

        def sign_in_probe(self, context):
            from agent_browser_studio import open_studio_browser
            from agent_e2e_studio_customer_session import prepare_conversation, rpc
            self.signing = secrets.token_urlsafe(48)
            context.slot.harness_secrets.append(self.signing)
            try:
                provision_local_server(context.slot.vm, self.signing)
                runtime = self._runtime(context, 'arm-browser')
                fixture, _ = select_arc_case(context)
                stack = runtime.setup('P08:' + fixture + ':local-docker', context.candidate['imageDigest'])
                browser = None
                try:
                    runtime.start_console(stack)
                    log = runner.ActionLog(Path(stack.workdir) / 'sign-in-probe', 1, set(), 200)
                    browser = open_studio_browser(stack, log, identity=runtime._console_identities[stack.compose_project],
                                                  scripted_customer=True)
                    prepare_conversation(invoke=lambda name, args: rpc(browser.endpoint, name, args),
                                         deadline=time.monotonic() + 30)
                    return {'ok': True, 'source': 'runner-minted customer in pinned CaseConsole workspace'}
                finally:
                    if browser is not None:
                        browser.stop()
                    runtime.teardown(stack)
            except Exception:
                return {'ok': False}

        def run(self, context, session, prompt):
            from ai_arc_runner import redact_bytes
            actions, answers, provenance, records, failure = [], [], [], [], None
            nudges = 0
            cli_subjects = set()
            cli_keys = set()
            for driver in ('arm-browser', 'console-assistant'):
                if context.remaining() <= 0:
                    failure = {'category': 'model', 'reason': 'Studio stage time limit reached'}
                    break
                if driver == 'console-assistant':
                    provision_local_server(context.slot.vm, self.signing, enable_assistant=True)
                runtime = self._runtime(context, driver)
                if driver == 'console-assistant':
                    runtime.disallowed_subjects = frozenset(cli_subjects)
                    runtime.disallowed_keys = frozenset(cli_keys)
                runtime.initialize_studio_admission = driver == 'console-assistant'
                runtime.timeout = min(2700, max(1, int(context.remaining())))
                if driver == 'arm-browser' and getattr(session, 'cli', None) is not None:
                    runtime.cli = session.cli
                fixture, case = select_arc_case(context, prompt)
                scenario = runner.load_scenario('P08')
                try:
                    receipt = runner.execute_case(case_id='P08:' + fixture + ':local-docker',
                        scenario=scenario, variant=fixture, case=case,
                        candidate_digest=context.candidate['imageDigest'], runtime=runtime)
                finally:
                    if driver == 'console-assistant':
                        provision_local_server(context.slot.vm, self.signing)
                self.cases[driver] = [{'scenario': 'P08', 'receipt': receipt}]
                if driver == 'console-assistant':
                    if cli_subjects & runtime.customer_subjects or cli_keys & runtime.customer_keys:
                        raise ValueError('Studio assistant and CLI customers are not distinct')
                    self.assistant_actors.update(runtime.assistant_actors)
                else:
                    cli_subjects.update(runtime.customer_subjects)
                    cli_keys.update(runtime.customer_keys)
                target = context.private_dir / 'studio' / driver
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                (target / 'case-receipt.json').write_bytes(redact_bytes(
                    json.dumps(receipt, sort_keys=True).encode(), context.slot.harness_secrets))
                for directory in runtime.workdirs:
                    runner.export_case_evidence(directory, target / 'export', runtime._secrets)
                    selected = services.private_actions(Path(directory))
                    outcomes = [action for entry in receipt.get('sessions', [])
                                for action in entry.get('actions', [])]
                    actions.extend(scored_actions(driver, selected, outcomes))
                answer = runtime.answers.get(context.slot.project)
                if isinstance(answer, str):
                    answers.append(answer)
                nudges += int(receipt.get('nudges') or 0)
                if driver == 'arm-browser':
                    provenance.extend(p for item in receipt.get('sessions', []) for p in item.get('provenance', []))
                    records.extend(arc.session_record(item) for item in receipt.get('sessions', []))
                if receipt.get('status') != 'pass' and failure is None:
                    failure = {'category': 'model' if receipt.get('status') == 'fail' else 'harness-error',
                               'reason': driver + ': case did not pass'}
            assistant = (self.cases.get('console-assistant') or [{}])[0].get('receipt', {}).get('studioAssistant', {})
            return {'actions': actions, 'finalAnswer': answers[-1] if answers else None, 'utterances': answers,
                    'nudges': nudges, 'failure': failure, 'nestedAnswerChecks': True,
                    'provenance': provenance, 'sessionRecords': records, 'assistantModel': assistant.get('model'),
                    'assistantRegion': assistant.get('region'), 'assistantUsage': assistant.get('usage')}

        def observe(self, context, row):
            return [item for driver, cases in self.cases.items() for item in driver_assertions(driver, cases)]

        def assistant_principals(self, context):
            return frozenset(self.assistant_actors)

    return StudioStage()
