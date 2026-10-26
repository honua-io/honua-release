"""Narrow, replayable Server IDs under a source-frozen native relationship.

Random proposal, audit, version and job IDs cannot be frozen before submission.
Only each finite profile's explicit assertion fields may refer to them. A trusted controller
collects paired immutable ScenarioHttp originals (GET reads and the finite P17
controller-issued denial POST); the gate replays those
same native relationships. This module never sends requests, accepts model JSON
as an original, approves an operation, or modifies frozen expectations.
"""
from __future__ import annotations

import copy
import re
from urllib.parse import urlsplit

from agent_e2e_pack import PackError, canonical_digest, digest_bytes
from agent_e2e_semantics import validate_response
from agent_e2e_studio_observer import OPERATION, guid, instant, route_path

PROFILE = 'honua.studio-publication-bindings/v1'
IDENTITY = ('campaignId', 'caseId', 'attempt', 'campaignMode', 'componentBindingDigest')
RELATIONSHIP = {'itemId', 'versionId', 'contentHash', 'route', 'visibility', 'requesterPrincipalId'}
ALLOWED = {('approval.pending', 'proposalId'): 'proposalId',
           ('approval.separate', 'proposalId'): 'proposalId',
           ('approval.separate', 'auditId'): 'approvalAuditId'}
STAGES = {'proposal': {'submissionHandle', 'proposal'},
          'approved': {'submissionHandle', 'proposal', 'approvedHandle', 'publicationRequest', 'savedVersion'}}


def require(condition, message):
    if not condition:
        raise PackError(message)


def exact(value, fields, message):
    require(isinstance(value, dict) and set(value) == fields, message)


def native_id(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value), 'invalid native binding identity')
    return value


def contract(value):
    if isinstance(value, dict) and value.get('profile') == 'honua.studio-denial-bindings/v1':
        from agent_e2e_studio_denial_observer import contract as denial_contract
        return denial_contract(value)
    if isinstance(value, dict) and value.get('profile') == 'honua.worker-custody-bindings/v1':
        from agent_e2e_native_custody_bindings import contract as custody_contract
        return custody_contract(value)
    if isinstance(value, dict) and value.get('profile') == 'honua.managed-job-bindings/v1':
        from agent_e2e_native_job_bindings import contract as job_contract
        return job_contract(value)
    if isinstance(value, dict) and value.get('profile') == 'honua.studio-authoring-bindings/v1':
        from agent_e2e_native_authoring_bindings import contract as authoring_contract
        return authoring_contract(value)
    exact(value, {'profile', 'fields', 'observers', 'relationships'}, 'invalid frozen native binding contract')
    require(value['profile'] == PROFILE and isinstance(value['fields'], list) and value['fields'] and
            len(value['fields']) == len(set(value['fields'])) and
            set(value['fields']) <= {'proposalId', 'approvalAuditId'}, 'unreviewed native binding field/profile')
    relations = value['relationships']
    exact(relations, RELATIONSHIP, 'native binding relationships must be exact and frozen')
    guid(relations['itemId']); guid(relations['versionId'])
    route_path(relations['route'])
    require(isinstance(relations['contentHash'], str) and re.fullmatch(r'[0-9a-f]{64}', relations['contentHash']) and
            relations['visibility'] in ('public', 'personal', 'team', 'organization') and
            isinstance(relations['requesterPrincipalId'], str) and relations['requesterPrincipalId'],
            'invalid frozen native Studio relationship')
    observers = value['observers']
    require(isinstance(observers, dict) and set(observers) in ({'1'}, {'1', '2'}), 'native observers must bind exact attempt numbers')
    for observer in observers.values():
        exact(observer, {'endpoint', 'attemptId', 'credentialKind'}, 'incomplete frozen independent observer')
        origin = urlsplit(observer['endpoint'])
        origin.port  # Reject malformed explicit ports before any execution.
        require(origin.hostname and not origin.username and not origin.password and not origin.query and
                not origin.fragment and origin.path == '' and
                all(33 <= ord(char) <= 126 for char in observer['endpoint']) and
                not any(char in observer['endpoint'] for char in ('?', '#', '\\')) and
                (origin.scheme == 'https' or origin.scheme == 'http' and origin.hostname in ('127.0.0.1', 'localhost', '::1')) and
                observer['credentialKind'] in ('bearer', 'api-key') and
                isinstance(observer['attemptId'], str) and re.fullmatch(r'[A-Za-z0-9-]{1,80}', observer['attemptId']),
                'unbound native observer origin/identity')
    require(len({row['attemptId'] for row in observers.values()}) == len(observers), 'native retry observer identity is reused')


def marker(value):
    if isinstance(value, dict) and '$native' in value:
        exact(value, {'$native'}, 'native reference cannot carry fallback/literal fields')
        reference = value['$native']
        require(isinstance(reference, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,40}\.(?:proposalId|approvalAuditId|savedVersionId|jobId|taskWorkerId)', reference),
                'invalid native reference')
        return reference.split('.')
    return None


def contains_marker(value):
    if isinstance(value, dict):
        return '$native' in value or any(contains_marker(item) for item in value.values())
    return isinstance(value, list) and any(contains_marker(item) for item in value)


def validate_case(value):
    require(isinstance(value, dict) and set(value) in ({'assertions', 'cleanup'}, {'assertions', 'cleanup', 'nativeBindings'}),
            'invalid frozen case expectation fields')
    contracts = value.get('nativeBindings', {})
    require(isinstance(contracts, dict) and len(contracts) <= 2 and all(isinstance(row, dict) for row in contracts.values()),
            'only reviewed finite native binding pairs are supported per case')
    profiles = [row.get('profile') for row in contracts.values()]
    require(len(profiles) == len(set(profiles)) and (len(profiles) <= 1 or frozenset(profiles) in {
        frozenset({'honua.studio-publication-bindings/v1', 'honua.worker-custody-bindings/v1'}),
        frozenset({'honua.studio-denial-bindings/v1', 'honua.worker-custody-bindings/v1'}),
        frozenset({'honua.studio-authoring-bindings/v1', 'honua.managed-job-bindings/v1'})}),
        'native bindings must be distinct reviewed publication/custody or authoring/job profiles')
    if 'honua.studio-denial-bindings/v1' in profiles:
        require('honua.worker-custody-bindings/v1' in profiles, 'P17 requires mandatory native worker custody')
        denial = next(row for row in contracts.values() if row['profile'] == 'honua.studio-denial-bindings/v1')
        custody = next(row for row in contracts.values() if row['profile'] == 'honua.worker-custody-bindings/v1')
        contract(denial)
        contract(custody)
        require(set(value['assertions']) == {'approval.pending', 'approval.no-bypass',
                'custody.bootstrap-revoked', 'custody.worker-replaced'}, 'P17 requires all four frozen assertions')
        from agent_e2e_studio_denial_observer import request_markers
        request_markers('approval.no-bypass', value['assertions']['approval.no-bypass'], contracts)
        require(denial['relationships']['requesterPrincipalId'] == custody['relationships']['taskPrincipalId'],
                'P17 denial and custody must bind the same actual task principal')
    used = {name: set() for name in contracts}
    for name, declaration in contracts.items():
        require(isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,40}', name), 'invalid frozen native binding name')
        contract(declaration)
    require(isinstance(value['assertions'], dict) and not contains_marker(value['cleanup']),
            'cleanup cannot resolve dynamic native identities')
    for assertion, expected in value['assertions'].items():
        require(isinstance(expected, dict), 'assertion expectation must be an object')
        for field, item in expected.items():
            if assertion == 'approval.no-bypass' and field == 'requests' and contains_marker(item):
                from agent_e2e_studio_denial_observer import request_markers
                name = request_markers(assertion, expected, contracts)
                used[name].add('proposalId')
                continue
            reference = marker(item)
            if reference is not None:
                name, native_field = reference
                require(name in contracts and allowed(assertion, field, contracts[name]) == native_field and
                        native_field in contracts[name]['fields'], 'native reference is outside frozen field/relationship allowlist')
                used[name].add(native_field)
            else:
                require(not contains_marker(item), 'nested/dynamic expression native references are unsupported')
    require(all(used[name] == set(declaration['fields']) for name, declaration in contracts.items()),
            'frozen native fields must exactly match assertion references')


def allowed(assertion, field, declaration):
    if declaration.get('profile') == 'honua.studio-denial-bindings/v1':
        return 'proposalId' if (assertion, field) == ('approval.pending', 'proposalId') else None
    if declaration.get('profile') == 'honua.worker-custody-bindings/v1':
        from agent_e2e_native_custody_bindings import ALLOWED as custody_allowed
        return custody_allowed.get((assertion, field))
    if declaration.get('profile') == 'honua.managed-job-bindings/v1':
        from agent_e2e_native_job_bindings import ALLOWED as job_allowed
        return job_allowed.get((assertion, field))
    if declaration.get('profile') == 'honua.studio-authoring-bindings/v1':
        from agent_e2e_native_authoring_bindings import ALLOWED as authoring_allowed
        return authoring_allowed.get((assertion, field))
    return ALLOWED.get((assertion, field))


def original(pair, *, observer, candidate, path):
    exact(pair, {'intent', 'response'}, 'native original requires paired retained HTTP records')
    intent, response = pair['intent'], pair['response']
    exact(intent, {'schemaVersion', 'candidateKey', 'attemptId', 'step', 'endpoint', 'method', 'path',
                  'credentialKind', 'contentType', 'requestBodyDigest'}, 'native original intent is not ScenarioHttp custody')
    exact(response, {'schemaVersion', 'candidateKey', 'attemptId', 'step', 'intentDigest', 'intentReference', 'response'},
          'native original response is not ScenarioHttp custody')
    require(type(intent['schemaVersion']) is int and type(response['schemaVersion']) is int and
            intent['schemaVersion'] == response['schemaVersion'] == 1 and intent['candidateKey'] == response['candidateKey'] == candidate and
            intent['attemptId'] == response['attemptId'] == observer['attemptId'] and
            intent['endpoint'] == observer['endpoint'] and intent['credentialKind'] == observer['credentialKind'] and
            intent['step'] == response['step'] and intent['method'] == 'GET' and intent['path'] == path and
            intent['contentType'] == 'application/json' and intent['requestBodyDigest'] == digest_bytes(b'') and
            response['intentDigest'] == canonical_digest(intent), 'native original changed observer/request/attempt provenance')
    exact(response['intentReference'], {'key', 'sha256'}, 'native original lacks retained intent acknowledgement')
    require(isinstance(response['intentReference']['key'], str) and response['intentReference']['key'] and
            response['intentReference']['sha256'] == canonical_digest(intent), 'native retained intent acknowledgement differs')
    observation = response['response']
    validate_response(observation)
    require(observation['httpStatus'] == 200 and isinstance(observation['body'], dict), 'native ID original is not a successful authenticated read')
    value = observation['body']
    require(value.get('success', True) is not False and 'error' not in value, 'native ID original reports product failure')
    return value.get('data', value)


def replay(report, declaration, context, *, complete=True):
    """Resolve from raw originals, retaining every ordinary oracle predicate."""
    if declaration.get('profile') == 'honua.studio-denial-bindings/v1':
        from agent_e2e_studio_denial_observer import replay as denial_replay
        return denial_replay(report, declaration, context, complete=complete)
    if declaration.get('profile') == 'honua.worker-custody-bindings/v1':
        from agent_e2e_native_custody_bindings import replay as custody_replay
        return custody_replay(report, declaration, context, complete=complete)
    if declaration.get('profile') == 'honua.managed-job-bindings/v1':
        from agent_e2e_native_job_bindings import replay as job_replay
        return job_replay(report, declaration, context, complete=complete)
    if declaration.get('profile') == 'honua.studio-authoring-bindings/v1':
        from agent_e2e_native_authoring_bindings import replay as authoring_replay
        return authoring_replay(report, declaration, context, complete=complete)
    contract(declaration)
    exact(report, {'schemaVersion', 'profile', 'name', 'context', 'contractDigest', 'stage', 'records', 'values'},
          'invalid native binding report')
    require(type(report['schemaVersion']) is int and report['schemaVersion'] == 1 and report['profile'] == PROFILE and
            report['contractDigest'] == canonical_digest(declaration) and
            report['context'] == {key: context[key] for key in IDENTITY} and
            str(context['attempt']) in declaration['observers'] and report['stage'] in STAGES,
            'native binding report belongs to another frozen contract/candidate/attempt')
    require(isinstance(report['records'], dict) and set(report['records']) == STAGES[report['stage']],
            'native binding report omits required independent originals')
    observer = declaration['observers'][str(context['attempt'])]
    records, relations = report['records'], declaration['relationships']
    def read(name, path):
        value = original(records[name], observer=observer, candidate=context['componentBindingDigest'], path=path)
        require(isinstance(value, dict), 'native original lacks an object')
        return value
    submitted_path = records['submissionHandle']['intent']['path']
    require(re.fullmatch(r'/api/v1/operations/handles/[A-Za-z0-9_-]{1,200}', submitted_path), 'native original is not an operation read')
    handle = read('submissionHandle', submitted_path)
    operation = native_id(handle.get('operationInstanceId'))
    proposal_id = native_id(handle.get('proposalId'))
    require(submitted_path == '/api/v1/operations/handles/'+operation and handle.get('operationId') == OPERATION and
            handle.get('status') == 'RequiresApproval', 'native Studio submission lacks canonical approval boundary')
    native_id(handle.get('auditId')); native_id(handle.get('correlationId'))
    begin, end = instant(context['startedAt']), instant(context['completedAt'])
    require(begin <= instant(handle.get('createdAt')) <= end, 'native operation was not created during this original attempt')
    proposal = read('proposal', '/api/v1/admin/proposals/'+proposal_id)
    expected_diff = [field+'='+relations[field] for field in ('itemId', 'versionId', 'contentHash', 'route', 'visibility')]
    require(proposal.get('proposalId') == proposal_id and proposal.get('kind') == 'StudioDraftMutation' and
            proposal.get('requestedBy') == relations['requesterPrincipalId'] and proposal.get('diff') == expected_diff and
            begin <= instant(proposal.get('createdAt')) <= end, 'native proposal does not seal the frozen owned relationship')
    values = {'proposalId': proposal_id}
    if report['stage'] == 'proposal':
        require(proposal.get('status') == 'AwaitingApproval', 'native pending binding is not awaiting approval')
    else:
        require(proposal.get('status') == 'Succeeded' and isinstance(proposal.get('resolvedBy'), str) and
                proposal['resolvedBy'] and proposal['resolvedBy'] != relations['requesterPrincipalId'] and
                instant(proposal['createdAt']) <= instant(proposal.get('resolvedAt')) <= end,
                'native Studio approval is absent/self-approved/outside attempt')
        execution = native_id(proposal.get('executionOperationId'))
        approved = read('approvedHandle', '/api/v1/operations/handles/'+execution)
        require(approved.get('operationInstanceId') == execution and approved.get('operationId') == OPERATION and
                approved.get('status') == 'Completed' and approved.get('proposalId') == proposal_id and
                begin <= instant(approved.get('createdAt')) <= end, 'native approved execution is not this completed Studio proposal')
        resources = approved.get('resourceIds', {})
        publication = guid(resources.get('publicationId'))
        require(all(resources.get(field) == relations[field] for field in ('itemId', 'versionId', 'route')) and
                resources.get('activeUrl') == route_path(relations['route']), 'native approved resource relationship differs')
        base = '/api/v1/studio/content-items/'+relations['itemId']+'/versions/'+relations['versionId']
        request = read('publicationRequest', base+'/publish-requests/'+publication)
        require(request.get('requestId') == publication and request.get('status') == 'accepted' and
                all(request.get(field) == relations[field] for field in ('itemId', 'versionId')) and
                request.get('requestedBy') == relations['requesterPrincipalId'] and
                request.get('intent', {}).get('route') == relations['route'] and
                request.get('intent', {}).get('visibility') == relations['visibility'], 'native publication request is not the approved owned version')
        version = read('savedVersion', base)
        require(all(version.get(field) == relations[field] for field in ('itemId', 'versionId', 'contentHash')) and
                isinstance(version.get('envelope'), dict) and version['envelope'], 'native saved version/hash differs from frozen relationship')
        values['approvalAuditId'] = native_id(approved.get('auditId'))
    require(report['values'] == values, 'native binding values differ from replayed original IDs')
    require(not complete or set(declaration['fields']) <= set(values), 'required native ID remains unbound')
    return values


def retain(durable, context, *, name, declaration, responses, stage, started_at, completed_at):
    """Append only independently retained ScenarioHttp originals under live liability."""
    attempt_key = durable._fixture_identity(context)
    require(isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,40}', name), 'invalid native binding name')
    require(stage in STAGES and isinstance(responses, dict) and set(responses) == STAGES[stage], 'incomplete native binding source references')
    records = {}
    for role, reference in responses.items():
        exact(reference, {'key', 'sha256'}, 'native binding requires retained response references, never model payloads')
        source = durable.state.get('scenarioOperations', {}).get(reference['key'])
        require(isinstance(source, dict) and canonical_digest(source) == reference['sha256'] and 'intentReference' in source,
                'native response reference is not retained controller HTTP evidence')
        intent_ref = source['intentReference']
        intent = durable.state.get('scenarioOperations', {}).get(intent_ref.get('key'))
        require(isinstance(intent, dict) and canonical_digest(intent) == intent_ref.get('sha256'), 'native response intent is absent or changed')
        records[role] = {'intent': copy.deepcopy(intent), 'response': copy.deepcopy(source)}
    report = {'schemaVersion': 1, 'profile': PROFILE, 'name': name,
        'context': {key: context[key] for key in IDENTITY}, 'contractDigest': canonical_digest(declaration),
        'stage': stage, 'records': records, 'values': {}}
    # Derive candidate values only from already retained native GET originals;
    # replay then verifies every relationship before anything is appended.
    submission = records['submissionHandle']['response']['response']['body']
    report['values']['proposalId'] = submission.get('data', submission).get('proposalId')
    if stage == 'approved':
        approved = records['approvedHandle']['response']['response']['body']
        report['values']['approvalAuditId'] = approved.get('data', approved).get('auditId')
    replay(report, declaration, {**context, 'startedAt': started_at, 'completedAt': completed_at}, complete=False)
    reports = durable.state.setdefault('nativeBindings', {})
    key = attempt_key+'/'+name+'/'+stage
    require(key not in reports, 'native binding already retained; cannot rebind or replay')
    for prior_key, prior in reports.items():
        if prior_key.startswith(attempt_key+'/'+name+'/'):
            require(prior['contractDigest'] == report['contractDigest'] and
                    all(report['values'].get(field) == value for field, value in prior['values'].items()),
                    'native identity changed after original binding')
    reports[key] = copy.deepcopy(report)
    durable._persist()
    return copy.deepcopy(report)


def resolve(assertion, expected, declarations, reports, context):
    result = copy.deepcopy(expected)
    if assertion == 'approval.no-bypass' and contains_marker(expected.get('requests')):
        from agent_e2e_studio_denial_observer import request_markers
        name = request_markers(assertion, expected, declarations)
        require(name in reports and reports[name]['name'] == name, 'P17 native denial report missing')
        values = replay(reports[name], declarations[name], context)
        for row in result['requests']:
            row['resourceId'] = values['proposalId']
        return result
    for field, value in expected.items():
        reference = marker(value)
        if reference is None:
            continue
        name, native_field = reference
        require(name in declarations and allowed(assertion, field, declarations[name]) == native_field and name in reports and
                reports[name]['name'] == name, 'native assertion identity is unbound or unauthorized')
        result[field] = replay(reports[name], declarations[name], context)[native_field]
    return result


def export(durable, context, declarations, evidence):
    """Materialize exact original-attempt reports after settlement without new reads."""
    durable._require_live_lease()
    require(durable.store.candidate == context['componentBindingDigest'], 'native export belongs to another candidate')
    prefix = canonical_digest({key: context[key] for key in ('campaignId', 'caseId', 'attempt')})+'/'
    context = {**context, 'modelActions': [row for row in durable.state.get('journals', {}).get(prefix[:-1], [])
                                          if row.get('event') == 'action']}
    reports = {key: report for key, report in durable.state.get('nativeBindings', {}).items() if key.startswith(prefix)}
    require({report['name'] for report in reports.values()} == set(declarations),
            'native export differs from exact frozen binding names')
    result = {}
    for name, declaration in declarations.items():
        candidates = [report for report in reports.values() if report['name'] == name]
        require(candidates, 'native binding was never independently collected')
        selected = max(candidates, key=lambda report: report['stage'] == 'approved')
        replay(selected, declaration, context)
        result[name] = evidence('native-binding-'+name+'-'+selected['stage'], selected)
    return result


def validate_observation(assertion, observation, declarations, reports, context):
    """P17 raw response hashes and safe nondisclosure must survive export unchanged."""
    from agent_e2e_studio_denial_observer import PROFILE as denial_profile, validate_observation as validate_denial
    for name, declaration in declarations.items():
        if declaration.get('profile') != denial_profile or assertion not in ('approval.pending', 'approval.no-bypass'):
            continue
        require(name in reports, 'P17 witness is missing')
        custody = [(key, row) for key, row in declarations.items() if row.get('profile') == 'honua.worker-custody-bindings/v1']
        require(len(custody) == 1 and custody[0][0] in reports, 'P17 original native custody is missing')
        key, contract = custody[0]
        replay(reports[key], contract, context)
        principal = reports[key]['proof']['taskPrincipalRead']['response']['response']['body']
        principal = principal.get('data', principal)
        require(principal.get('principal') == declaration['relationships']['taskPrincipal'], 'P17 full custody principal differs')
        validate_denial(assertion, observation, reports[name], declaration, context)
