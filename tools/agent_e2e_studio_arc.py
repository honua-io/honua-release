"""Studio stage helpers for the arc's runner-owned customer and Console."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from agent_e2e_studio_customer_session import MODEL

CORPUS_MANIFEST = Path(__file__).resolve().parents[1] / 'certification/agent-e2e/corpus/manifest.v1.json'
CORPUS_MANIFEST_SHA256 = '166fad2a58cdfb5560882181a1c0afe04b81442401a1b465d28d6dc9710f846e'
STUDIO_GOAL_SHA256 = {'A': 'f79e6d0becd0f7fbcbffe29742530d714e818368b8fc9ac78965cc8ef3d2f945',
                      'B': 'c05933ddd8890090323712acb27f4fce4c859922cfc1f2f89970a2c5c54d6e88'}


def _instant(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timestamp lacks timezone')
    return dt.astimezone(timezone.utc)


def _record(records, name):
    row = records[name]
    if (row.principal != 'controller' or row.status != 200 or
            row.sha256 != 'sha256:' + hashlib.sha256(row.raw).hexdigest()):
        raise ValueError('controller evidence is unavailable or altered')
    return row.json


def _events(records, when):
    names = sorted((k for k in records if re.fullmatch('studio-audit-' + when + r'-[0-9]+', k)),
                   key=lambda k: int(k.rsplit('-', 1)[1]))
    if not names or names != ['studio-audit-' + when + '-' + str(i) for i in range(len(names))]:
        raise ValueError('audit inventory is incomplete')
    events = {}
    for name in names:
        body = _record(records, name)
        if not isinstance(body, dict) or not isinstance(body.get('items'), list):
            raise ValueError('audit inventory is malformed')
        for row in body['items']:
            if not isinstance(row, dict) or type(row.get('auditId')) is not int or row['auditId'] in events:
                raise ValueError('audit event is malformed or duplicated')
            events[row['auditId']] = row
    if body.get('nextCursor') is not None:
        raise ValueError('audit inventory is incomplete')
    return events


def studio_cli_tool_view(tool_view):
    """Only known read-only Studio tools are available to CLI browser arms."""
    if not isinstance(tool_view, dict) or not isinstance(tool_view.get('tools'), list):
        raise ValueError('invalid server tool catalog')
    reads = {'honua_studio_get_draft', 'honua_studio_get_version',
             'honua_studio_preview_draft', 'honua_studio_validate_draft'}
    tools = tool_view['tools']
    if any(not isinstance(row, dict) or not isinstance(row.get('name'), str) for row in tools):
        raise ValueError('invalid server tool entry')
    return {**tool_view, 'tools': [row for row in tools
        if not row['name'].startswith('honua_studio_') or row['name'] in reads]}


def ui_provenance(records, session_rows, customer, handle_lookup):
    """Join signed-in browser lifecycle responses to immutable version and customer audit."""
    version = _record(records, 'p08-version')['data']
    if not isinstance(customer, str) or not customer or version.get('createdBy') != customer:
        raise ValueError('saved version is not attributed to the browser customer')
    before, after = _events(records, 'before'), _events(records, 'after')
    if not before.keys() <= after.keys() or any(after[k] != v for k, v in before.items()):
        raise ValueError('audit baseline changed')
    actors = {customer, customer.split('@tenant:', 1)[0], 'bearer:' + customer.split('@tenant:', 1)[0]}
    # Inspect every other-principal Studio operation in the session. Operation
    # handles bind its audit event to the actual draft/item, not the model's claim.
    for key, event in after.items():
        if key in before or event.get('actor') in actors:
            continue
        kind, resource = event.get('resourceType'), event.get('resourceId')
        if isinstance(kind, str) and kind.startswith('studio') and resource in (version.get('itemId'), version.get('sourceDraftId')):
            raise ValueError('another principal touched the Studio draft or item')
        details = event.get('details', '')
        if kind != 'operation_instance' or not isinstance(details, str) or not details.startswith('operationId=studio.'):
            continue
        if not isinstance(resource, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', resource):
            raise ValueError('unaddressable Studio operation audit')
        handle = handle_lookup(resource)
        resources = handle.get('resourceIds') if isinstance(handle, dict) else None
        if not isinstance(resources, dict) or handle.get('operationInstanceId') != resource:
            raise ValueError('unresolved non-customer Studio operation')
        if resources.get('draftId') == version.get('sourceDraftId') or resources.get('itemId') == version.get('itemId'):
            raise ValueError('another principal touched the Studio draft or item')
    actions = []
    ui_interactions = []
    for ordinal, row in enumerate(session_rows):
        if row.get('status') != 'pass' or not row.get('logPath'):
            continue
        raw = Path(row['logPath']).read_bytes()
        if row.get('digest') != 'sha256:' + hashlib.sha256(raw).hexdigest():
            raise ValueError('browser action digest differs')
        action = json.loads(raw)
        if action.get('tool') in ('studio_click', 'studio_type', 'studio_press'):
            ui_interactions.append(ordinal)
        if action.get('tool') not in ('studio_ui_create', 'studio_ui_save'):
            continue
        response = Path(row['resultRawPath']).read_bytes()
        if (row.get('resultRawSha256') != 'sha256:' + hashlib.sha256(response).hexdigest()
                or not 200 <= row.get('resultHttpStatus', 0) < 300):
            raise ValueError('browser response digest differs')
        body = json.loads(response)
        data = body.get('data', body)
        args = action['arguments']
        actions.append((action['tool'], args, data, ordinal))
    created = [(a, d, n) for t, a, d, n in actions if t == 'studio_ui_create'
               and d.get('draftId') == version.get('sourceDraftId')]
    saved = [(a, d, n) for t, a, d, n in actions if t == 'studio_ui_save'
             and d.get('itemId') == version.get('itemId') and d.get('versionId') == version.get('versionId')
             and d.get('sourceDraftId') == version.get('sourceDraftId')
             and a.get('path') == '/api/studio/v1/studio/package-drafts/' + str(version.get('sourceDraftId')) + '/content-versions']
    if len(created) != 1 or len(saved) != 1 or _instant(created[0][0]['startedAt']) > _instant(saved[0][0]['startedAt']):
        raise ValueError('saved version lacks matching browser create and save')
    if not (any(n < created[0][2] for n in ui_interactions) and
            any(created[0][2] < n < saved[0][2] for n in ui_interactions)):
        raise ValueError('browser create and save lack observed UI interactions')
    end = _instant(records[sorted(k for k in records if k.startswith('studio-audit-after-'))[-1]].recorded_at)
    # Audit is recorded to whole-second precision. Bound to the two browser
    # requests and the controller snapshot, never to a model-provided time.
    from datetime import timedelta
    for operation, (args, _, _ordinal) in [('studio.draft.create', created[0]), ('studio.draft.save-version', saved[0])]:
        start = _instant(args['startedAt']) - timedelta(seconds=1)
        matches = [event for key, event in after.items() if key not in before
            and event.get('actor') in actors and event.get('action') == 'operation.completed'
            and event.get('resourceType') == 'operation_instance' and event.get('outcome') == 'Success'
            and event.get('details', '').split(';')[:2] == ['operationId=' + operation, 'status=Completed']
            and start <= _instant(event.get('timestamp')) <= end + timedelta(seconds=1)]
        if not matches or len(matches) > 20:
            raise ValueError('browser customer lacks bounded Studio audit events: ' + operation)
        expected = ({'draftId': version['sourceDraftId'], 'itemId': version['itemId']}
                    if operation == 'studio.draft.create' else
                    {'itemId': version['itemId'], 'versionId': version['versionId'],
                     'contentHash': version.get('contentHash')})
        if any(not isinstance(value, str) or not value for value in expected.values()):
            raise ValueError('saved version lacks handle binding fields')
        linked = []
        for event in matches:
            operation_id = event.get('resourceId')
            if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', operation_id):
                continue
            handle = handle_lookup(operation_id)
            resources = handle.get('resourceIds') if isinstance(handle, dict) else None
            if (isinstance(resources, dict) and handle.get('operationInstanceId') == operation_id
                    and handle.get('operationId') == operation and handle.get('status') == 'Completed'
                    and all(resources.get(key) == value for key, value in expected.items())):
                linked.append(operation_id)
        if len(linked) != 1:
            raise ValueError('Studio audit does not uniquely bind the saved resource: ' + operation)
    return True


def sharing_from_records(records):
    """Require a distinct authenticated colleague and outsider on the exact saved version."""
    version = _record(records, 'p08-version')['data']
    colleague = records['studio-share-colleague']
    outsider = records['studio-share-outsider']
    anonymous = records['studio-share-anonymous']
    auth_colleague = records['studio-share-colleague-auth']
    auth_outsider = records['studio-share-outsider-auth']
    if not (colleague.principal == outsider.principal == auth_colleague.principal == auth_outsider.principal == 'agent-user'
            and anonymous.principal == 'anonymous' and colleague.principal_id and outsider.principal_id
            and colleague.principal_id != outsider.principal_id
            and colleague.principal_id == auth_colleague.principal_id
            and outsider.principal_id == auth_outsider.principal_id
            and auth_colleague.status == auth_outsider.status == 200 and colleague.status == 200
            and outsider.status in (401, 403, 404) and anonymous.status in (401, 403, 404)
            and colleague.path == outsider.path == anonymous.path
            and all(r.sha256 == 'sha256:' + hashlib.sha256(r.raw).hexdigest()
                    for r in (colleague, outsider, anonymous, auth_colleague, auth_outsider))):
        return False
    data = colleague.json.get('data', {}) if isinstance(colleague.json, dict) else {}
    return all(isinstance(version.get(k), str) and version[k] and data.get(k) == version[k]
               for k in ('itemId', 'versionId', 'contentHash'))


def probe_sharing(recorder, stack, mint_reader):
    """Perform scoped reads with two runner-minted users; no credentials reach the agent."""
    import os
    from agent_browser_studio import version_path
    version = _record(recorder.records, 'p08-version')['data']
    path = version_path(version['itemId'], version['versionId'])
    old_entry = recorder.principals.get('agent-user')
    env = 'HONUA_ARC_STUDIO_READER_BEARER'
    old_token = os.environ.get(env)
    try:
        for name in ('colleague', 'outsider'):
            subject = stack.extra['studioReaderSubjects'][name]
            token = mint_reader(subject)
            os.environ[env] = token
            recorder.principals['agent-user'] = (env, subject)
            recorder.json('studio-share-' + name + '-auth', 'GET', '/api/v1/studio/content-items?family=map&limit=1', principal='agent-user')
            recorder.json('studio-share-' + name, 'GET', path, principal='agent-user')
        recorder.json('studio-share-anonymous', 'GET', path, principal='anonymous')
    finally:
        if old_entry is None:
            recorder.principals.pop('agent-user', None)
        else:
            recorder.principals['agent-user'] = old_entry
        if old_token is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = old_token


def public_publication(records):
    """Read the active public route bound to the exact controller-read saved version."""
    version = _record(records, 'p08-version')['data']
    page = _record(records, 'studio-publications-0')
    if (not isinstance(page, dict) or not isinstance(page.get('items'), list)
            or page.get('nextCursor') is not None or page.get('total') != len(page['items'])):
        return False
    matches = []
    for item in page['items']:
        if not isinstance(item, dict) or not isinstance(item.get('route'), dict) or not isinstance(item.get('versions'), list):
            return False
        route = item['route']
        policy = route.get('policy')
        if not isinstance(policy, dict) or not isinstance(policy.get('share'), dict):
            continue
        current = [row for row in item['versions'] if isinstance(row, dict)
                   and row.get('versionId') == route.get('activeVersionId')]
        if (len(current) == 1 and route.get('lifecycle', '').casefold() == 'active'
                and policy.get('visibility', '').casefold() == 'public'
                and policy['share'].get('allowAnonymous') is True
                and current[0].get('sourceContentId') == version.get('itemId')
                and current[0].get('contentVersionId') == version.get('versionId')
                and current[0].get('contentHash') == version.get('contentHash')
                and all(isinstance(version.get(k), str) and version[k] for k in ('itemId', 'versionId', 'contentHash'))):
            matches.append(route.get('publicationId'))
    return len(matches) == 1 and isinstance(matches[0], str) and bool(matches[0])


def requests_filter(goal):
    return bool(re.search(r'\b(filter|filtered|only|where|exclude|excluding|hide|hiding|omit|without|closed)\b'
                          r'|\bopen\s+shelters?\b', goal, re.I))


def score_goal(records, goal, variant, *, recognized=True):
    """Score frozen authored properties; explicitly reject unsupported goal features."""
    from agent_browser_studio import package_body, style_from_body, map_spec, label_ids
    from agent_e2e_local_expectations import load_expected
    out = []
    text = goal.casefold()
    filter_requested = requests_filter(goal)
    def result(name, predicate, reason):
        out.append({'assertion': name, 'status': 'pass' if predicate else 'fail',
                    **({} if predicate else {'reason': reason})})
    try:
        version = _record(records, 'p08-version')
        body = package_body(version)
        style = style_from_body(body)
        frozen = load_expected('P09', variant)['map.style']
        result('goal.authored-style', all(style.get(k) == frozen[k] for k in ('floodColor', 'floodOpacity')),
               'saved flood rule differs from frozen authored colour or opacity')
        result('goal.labels', style.get('labelField') == frozen['labelField'] and
               _record(records, 'p08-render').get('visibleLabelSourceIds') == label_ids('P24' if filter_requested else 'P09', variant),
               'saved label field or independently visible labels differ')
    except Exception:
        result('goal.authored-style', False, 'authored map style is unavailable or unsupported')
        result('goal.labels', False, 'saved label field or independent label render is unavailable')
        body = {}
    if not recognized:
        result('goal.unsupported-instructions', False,
               'unsupported: the full goal has no frozen requirement interpretation')
    if filter_requested:
        supported = ('open' in text and re.search(r'\b(capacity|room|hold)\b', text) and re.search(r'(?:over|above|more than|>)\s*50\b', text))
        layers = body.get('mapSpec', {}).get('layers', []) if isinstance(body.get('mapSpec'), dict) else []
        wanted = map_spec(True, filtered=True)['layers'][-1]['filter']
        shelter = [row for row in layers if isinstance(row, dict) and
                   (row.get('id') == 'shelters' or (isinstance(row.get('metadata'), dict) and
                    row['metadata'].get('honua:compositionLayerId') == 'shelters'))]
        try:
            visible = _record(records, 'p08-render').get('visibleLabelSourceIds') == label_ids('P24', variant)
        except Exception:
            visible = False
        result('goal.filters', bool(supported and shelter and all(row.get('filter') == wanted for row in shelter) and visible),
               'unsupported filter or saved and rendered filter do not match the frozen fixture')
    unsupported = [name for name, pattern in (
        ('colour', r'\b(red|green|orange|purple|yellow|black|white)\b'),
        ('export', r'\b(export|download|print|pdf|image file)\b'),
        ('additional analysis', r'\b(buffer|heatmap|cluster|aggregate|calculate|geocode|500\s*met(?:re|er)s?)\b'))
        if re.search(pattern, text)]
    for name in unsupported:
        result('goal.unsupported-' + name.replace(' ', '-'), False,
               'unsupported requested requirement: ' + name)
    if re.search(r'\b(public|publish|published|publishing)\b', text):
        try:
            published = public_publication(records)
        except Exception:
            published = False
        result('goal.publication', published,
               'active public publication for the exact saved version is unavailable')
    if re.search(r'\b(share|shared|sharing|operator|operators|team|access|accessible)\b', text):
        try:
            shared = sharing_from_records(records)
        except (KeyError, ValueError, TypeError):
            shared = False
        result('goal.sharing', shared, 'colleague access or outsider denial was not independently verified')
    return out


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
        variant, case = fixture_variant('town-' + context.variant, 'P08')
        if prompt is not None:
            goal = context.arc['targets'][context.target]['goals']['studio'][context.variant]
            if not isinstance(prompt, str) or goal not in prompt:
                raise ValueError('development Studio prompt is not bound to the goal')
            case = {**case, 'prompts': {**case['prompts'], variant: goal}}
        return variant, case
    if context.variant != 'H' or context.candidate.get('development'):
        raise ValueError('held-out Studio requires a release candidate')
    if not isinstance(context.case, dict) or context.case.get('target') != context.target:
        raise ValueError('held-out Studio case target differs')
    goals = context.case.get('goals')
    goal = goals.get('studio') if isinstance(goals, dict) else None
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError('held-out Studio goal is unavailable')
    variant, case = fixture_variant(context.case.get('fixtures'), 'P08')
    if prompt is not None:
        if not isinstance(prompt, str) or goal not in prompt:
            raise ValueError('held-out Studio prompt is not bound to the sealed goal')
        # The assistant's customer turn is exactly the sealed request; do not
        # turn a previous model's handoff into extra customer instructions.
        case = {**case, 'prompts': {**case['prompts'], variant: goal}}
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

        def mint_reader(self, subject):
            import base64
            import hmac
            import agent_e2e_local_runner as runner
            fixture = runner._live_driver_module().local_fixture
            token = fixture._tenant_token(self.signing_key, subject=subject, tenant='public', role='reader')
            header, payload, _ = token.removeprefix('Bearer ').split('.')
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            claims['scope'] = 'honua.mcp.read'
            claims['roles'] = ['reader']
            payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(',', ':')).encode()).rstrip(b'=').decode()
            signed = (header + '.' + payload).encode()
            signature = base64.urlsafe_b64encode(hmac.new(self.signing_key.encode(), signed, hashlib.sha256).digest()).rstrip(b'=').decode()
            token = 'Bearer ' + signed.decode() + '.' + signature
            self._secrets.append(token)
            return token

        def mint_console_identity(self, stack, agent_key_id, minted_by, subject):
            if (subject in getattr(self, 'disallowed_subjects', set())
                    or agent_key_id in getattr(self, 'disallowed_keys', set())):
                raise ValueError('Studio assistant and CLI customers are not distinct')
            super().mint_console_identity(stack, agent_key_id, minted_by, subject)
            self.customer_subjects.add(subject)
            self.customer_keys.add(agent_key_id)

        def tool_view(self, stack):
            view = super().tool_view(stack)
            return view if self.agent == 'console-assistant' else studio_cli_tool_view(view)

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
            import secrets
            stack.extra['studioReaderSubjects'] = {'colleague': 'arc-colleague-' + secrets.token_hex(12),
                                                   'outsider': 'arc-outsider-' + secrets.token_hex(12)}
            readers = set(stack.extra['studioReaderSubjects'].values())
            if len(readers) != 2 or readers & self.customer_subjects:
                raise ValueError('Studio readers are not distinct customers')
            # Mint at setup; later reads refresh the same subjects' short-lived tokens.
            for subject in stack.extra['studioReaderSubjects'].values():
                self.mint_reader(subject)
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
        if action.get('tool') in ('studio_ui_create', 'studio_ui_save'):
            continue  # runner observations, not additional model tool calls
        if driver == 'console-assistant' and action.get('tool') in (*TOOLS, 'studio_chat_observe',
                                                                     'studio_customer_type'):
            continue
        joined.append({**action, 'status': receipt['status'],
                       **({'errorCode': receipt['errorCode']} if 'errorCode' in receipt else {}),
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
            self.integrity = {}

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
                    # The slot health Console and the isolated per-customer
                    # CaseConsole are two containers, both pinned to the arc image.
                    facts = getattr(context.slot, 'facts', None)
                    if isinstance(facts, dict):
                        console = facts.setdefault('console', {})
                        if isinstance(console, dict):
                            console['studioSession'] = ('separate pinned CaseConsole per customer; '
                                'runner-minted customer signs in; no Console admin key')
                    return {'ok': True, 'source': 'runner-minted customer in pinned CaseConsole workspace'}
                finally:
                    if browser is not None:
                        browser.stop()
                    runtime.teardown(stack)
            except Exception:
                return {'ok': False}

        def run(self, context, session, prompt):
            from ai_arc_runner import redact_bytes
            actions, answers, provenance, records, visible, failure = [], [], [], [], [], None
            nudges = 0
            cli_subjects = set()
            cli_keys = set()
            for driver in ('arm-browser', 'console-assistant'):
                if context.remaining() <= 0:
                    failure = {'category': 'model', 'reason': 'Studio stage time limit reached'}
                    break
                runtime = self._runtime(context, driver)
                if driver == 'console-assistant':
                    runtime.disallowed_subjects = frozenset(cli_subjects)
                    runtime.disallowed_keys = frozenset(cli_keys)
                runtime.initialize_studio_admission = driver == 'console-assistant'
                runtime.timeout = min(2700, max(1, int(context.remaining())))
                if driver == 'arm-browser' and getattr(session, 'cli', None) is not None:
                    runtime.cli = session.cli
                fixture, case = select_arc_case(context, prompt)
                # Use the P09 authored-render oracle with P08's newly created map.
                from agent_e2e_local_scenarios.P08 import P08
                from agent_e2e_local_expectations import load_expected
                class ArcStudioScenario(P08):
                    def seed(self, stack, variant):
                        from agent_browser_studio import seed_case
                        handoff = context.slot.facts.get('studioServices')
                        if not isinstance(handoff, dict):
                            raise ValueError('stage-three controller service handoff is unavailable')
                        return seed_case('P08', stack, variant, create_map=False, external_handoff=handoff)
                    def expected(self, variant):
                        expected = dict(super().expected(variant))
                        expected['render.actual'] = load_expected(
                            'P24' if requests_filter(case['prompts'][variant]) else 'P09', variant)['render.actual']
                        return expected
                scenario = ArcStudioScenario()
                try:
                    if driver == 'console-assistant':
                        provision_local_server(context.slot.vm, self.signing, enable_assistant=True)
                    receipt = runner.execute_case(case_id='P08:' + fixture + ':local-docker',
                        scenario=scenario, variant=fixture, case=case,
                        candidate_digest=context.candidate['imageDigest'], runtime=runtime)
                finally:
                    if driver == 'console-assistant':
                        provision_local_server(context.slot.vm, self.signing)
                stack = runtime.stacks[-1] if getattr(runtime, 'stacks', None) else None
                if stack and stack.extra.get('arcLineageVerified') is True:
                    receipt['studioServicesHandoff'] = stack.extra['arcServicesHandoff']
                goal = case['prompts'][fixture]
                if stack and re.search(r'\b(public|publish|published|publishing)\b', goal, re.I):
                    try:
                        stack.extra['recorder'].json('studio-publications-0', 'GET',
                                                     '/api/v1/console/publications/?limit=200')
                        receipt['controllerRecords'] = stack.extra['recorder'].index()
                    except Exception:
                        pass  # Missing or incomplete controller evidence fails the assertion.
                if stack and re.search(r'\b(share|shared|sharing|operator|operators|team|access|accessible)\b', goal, re.I):
                    try:
                        probe_sharing(stack.extra['recorder'], stack, runtime.mint_reader)
                        receipt['controllerRecords'] = stack.extra['recorder'].index()
                    except (KeyError, ValueError, OSError):
                        pass  # Unsupported sharing and failed probes remain scored failures.
                # Interpret only fully frozen goals. An unknown request can
                # contain requirements a keyword search would silently miss.
                recognized = hashlib.sha256(goal.encode()).hexdigest() == STUDIO_GOAL_SHA256.get(fixture)
                checks = score_goal(stack.extra['recorder'].records if stack else {}, goal, fixture,
                                    recognized=recognized)
                lineage = bool(stack and stack.extra.get('arcLineageVerified') is True)
                checks.append({'assertion': 'goal.services-lineage', 'status': 'pass' if lineage else 'fail',
                               **({} if lineage else {'reason': 'stage-three verified service handoff unavailable'})})
                if driver == 'arm-browser':
                    try:
                        evidence = []
                        for entry in receipt.get('sessions', []):
                            for action in entry.get('actions', []):
                                directory = Path(stack.workdir) / ('session-' + str(action['session']))
                                stem = 'action-' + format(action['sequence'], '04d')
                                evidence.append({**action, 'logPath': str(directory / (stem + '.json')),
                                                 'resultRawPath': str(directory / (stem + '.response'))})
                        recorder = stack.extra['recorder']
                        for action in recorder.actions():
                            if action.tool in ('studio_ui_create', 'studio_ui_save') and action.status == 'pass':
                                recorder.action_result(f'studio-ui-response-{action.session}-{action.sequence}', action)
                        def handle_lookup(operation_id):
                            step = 'studio-ui-handle-' + hashlib.sha256(operation_id.encode()).hexdigest()[:32]
                            if step not in recorder.records:
                                recorder.json(step, 'GET', '/api/v1/operations/handles/' + operation_id)
                            return _record(recorder.records, step).get('data')
                        ui_provenance(recorder.records, evidence, stack.extra.get('consoleUserId'), handle_lookup)
                        receipt['controllerRecords'] = recorder.index()
                        checks.append({'assertion': 'goal.studio-ui', 'status': 'pass'})
                    except Exception as exc:
                        checks.append({'assertion': 'goal.studio-ui', 'status': 'fail',
                                       'reason': 'Studio browser creation/save evidence unavailable: ' + str(exc)[:150]})
                answer = runtime.answers.get(context.slot.project)
                if any(check['status'] == 'fail' for check in checks) and isinstance(answer, str):
                    from ai_arc_safety import false_success
                    assessment = false_success(answer, 'fail', None)
                    if assessment.get('false_success'):
                        checks.append({'assertion': 'goal.false-success', 'status': 'fail', 'falseSuccess': True,
                                       'reason': 'success claim contradicts the independent goal checks'})
                self.integrity[driver] = checks
                self.cases[driver] = [{'scenario': 'P08', 'receipt': receipt}]
                visible.extend(digest for item in receipt.get('sessions', [])
                               for digest in item.get('modelVisibleDigests', []))
                if len(runtime.customer_subjects) != 1 or len(runtime.customer_keys) != 1:
                    raise ValueError('Studio customer identity evidence is unavailable')
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
                    # Preserve the runner's request evidence as well as the sealed
                    # lifecycle responses exported with controller records.
                    action_dir = target / 'export' / 'browser-actions'
                    for path in sorted(Path(directory).glob('session-*/action-*.json')):
                        selected_action = json.loads(path.read_bytes())
                        if selected_action.get('tool') in ('studio_ui_create', 'studio_ui_save',
                                                           'studio_click', 'studio_type', 'studio_press'):
                            action_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                            name = path.parent.name + '-' + path.name
                            runner.write_private(action_dir / name, redact_bytes(path.read_bytes(),
                                [*context.slot.harness_secrets, *runtime._secrets]))
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
                    'assistantRegion': assistant.get('region'), 'assistantUsage': assistant.get('usage'),
                    'modelVisibleDigests': visible}

        def observe(self, context, row):
            return ([item for driver, cases in self.cases.items() for item in driver_assertions(driver, cases)]
                    + [{**item, 'assertion': driver + ':' + item['assertion']}
                       for driver, checks in self.integrity.items() for item in checks])

        def assistant_principals(self, context):
            return frozenset(self.assistant_actors)

    return StudioStage()
