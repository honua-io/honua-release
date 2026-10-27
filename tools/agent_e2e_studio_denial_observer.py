"""P17 controller-issued denial witness; never a model action or a full campaign proof.

Only an independently selected owned pending proposal may be probed, using the
verified task identity. Generic authorization refusals are explicitly unsupported.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from agent_e2e_pack import PackError, canonical_digest, digest_bytes
from agent_e2e_scenario_operations import data
from agent_e2e_semantics import validate_response

PROFILE = 'honua.studio-denial-bindings/v1'
REASON = 'Separation of duties: the requester of a proposal cannot approve it.'
STEP_SUFFIXES = {'proposal': '-before', 'taskTarget': '-task-target', 'taskProposal': '-task-positive',
                 'denial': '-self-approval-denial', 'proposalAfter': '-after',
                 'ownerRoute': '-owner-route', 'anonymousRoute': '-anonymous-route'}
ROLES = {'submissionHandle', 'proposal', 'taskTarget', 'taskProposal', 'denial',
         'proposalAfter', 'ownerRoute', 'anonymousRoute'}


def require(value, reason):
    if not value:
        raise PackError(reason)


def principal_projection(value):
    from agent_e2e_native_bindings import exact
    exact(value, {'actorId', 'studioOwnerId', 'tenantId', 'authenticationKind', 'isHumanSession'},
          'P17 requires the explicit native task principal projection')
    require(all(isinstance(value[key], str) and 0 < len(value[key]) <= 512 for key in ('actorId', 'studioOwnerId')) and
            (value['tenantId'] is None or isinstance(value['tenantId'], str) and len(value['tenantId']) <= 512) and
            value['authenticationKind'] == 'api-key' and value['isHumanSession'] is False,
            'P17 principal is not a bounded native task key identity')
    return copy.deepcopy(value)


def safe_denial(observation):
    return observation['httpStatus'] == 403 and observation['body'] == {
        'type': 'https://tools.ietf.org/html/rfc9110#section-15.5.4', 'title': 'Forbidden', 'status': 403, 'detail': REASON}


def publication_contract(declaration):
    from agent_e2e_native_bindings import PROFILE as publication_profile
    result = {key: copy.deepcopy(declaration[key]) for key in ('fields', 'observers', 'relationships')}
    result['profile'] = publication_profile
    result['relationships'].pop('targetId')
    result['relationships'].pop('taskPrincipal')
    return result


def contract(declaration):
    from agent_e2e_native_bindings import contract as publication, exact, native_id
    exact(declaration, {'profile', 'fields', 'observers', 'relationships', 'requesters', 'anonymousObservers'}, 'invalid P17 denial contract')
    require(declaration['profile'] == PROFILE and declaration['fields'] == ['proposalId'], 'invalid P17 fields')
    require(isinstance(declaration['relationships'], dict), 'invalid P17 relationships')
    require(isinstance(declaration['relationships'].get('targetId'), str) and declaration['relationships']['targetId'],
            'P17 requires the original target identity')
    native_id(declaration['relationships']['targetId'])
    principal = principal_projection(declaration['relationships'].get('taskPrincipal'))
    require(principal['actorId'] == declaration['relationships'].get('requesterPrincipalId'), 'P17 principal projection differs')
    publication(publication_contract(declaration))
    anonymous = declaration['anonymousObservers']
    require(isinstance(anonymous, dict) and set(anonymous) == set(declaration['observers']), 'P17 anonymous attempts differ')
    requesters = declaration['requesters']
    require(isinstance(requesters, dict) and set(requesters) == set(declaration['observers']), 'P17 requester attempts differ')
    identities = [row['attemptId'] for row in declaration['observers'].values()]
    for attempt, requester in requesters.items():
        exact(requester, {'endpoint', 'attemptId', 'credentialKind', 'agentContext'}, 'invalid P17 task observer')
        anon = anonymous[attempt]
        exact(anon, {'endpoint', 'attemptId', 'credentialKind'}, 'invalid P17 anonymous observer')
        require(anon['credentialKind'] == 'anonymous' and anon['endpoint'] == declaration['observers'][attempt]['endpoint'] and
                isinstance(anon['attemptId'], str) and 0 < len(anon['attemptId']) <= 80 and
                all(c.isalnum() or c == '-' for c in anon['attemptId']) and
                len({anon['attemptId'], requester['attemptId'], declaration['observers'][attempt]['attemptId']}) == 3,
                'P17 anonymous transport is not distinct/bound')
        identities.extend((anon['attemptId'], requester['attemptId']))
        transport = {key: requester[key] for key in ('endpoint', 'attemptId', 'credentialKind')}
        check = publication_contract(declaration)
        check['observers'][attempt] = transport
        publication(check)
        from agent_e2e_mcp import validate_agent_context
        require(validate_agent_context(requester['agentContext']) is not None and
                requester['endpoint'] == declaration['observers'][attempt]['endpoint'] and
                requester['attemptId'] != declaration['observers'][attempt]['attemptId'], 'P17 task transport is not distinct/bound')

    require(len(identities) == len(set(identities)), 'P17 retry transport identity is reused')


def request_markers(assertion, expected, declarations):
    """Only these two resourceId slots admit one native proposal ID; nothing recursive."""
    from agent_e2e_native_bindings import exact, marker
    require(assertion == 'approval.no-bypass', 'P17 request binding is not an approval denial')
    exact(expected, {'requests'}, 'P17 permission expectation must have exactly two fixed requests')
    rows = expected['requests']
    require(isinstance(rows, list) and len(rows) == 2, 'P17 needs same-proposal positive and negative controls')
    binding = None
    for row, method, status in zip(rows, ('GET', 'POST'), (200, 403)):
        exact(row, {'principalId', 'method', 'resourceId', 'status'}, 'invalid P17 permission row')
        reference = marker(row['resourceId'])
        require(reference is not None and reference[1] == 'proposalId' and reference[0] in declarations,
                'P17 resource must use its finite proposal identity')
        name, field = reference
        declaration = declarations[name]
        contract(declaration)
        require(binding in (None, name) and row['principalId'] == declaration['relationships']['requesterPrincipalId'] and
                row['method'] == method and type(row['status']) is int and row['status'] == status,
                'P17 methods/status/principal must remain frozen')
        binding = name
    return binding


def pair(record, *, transport, candidate, method, path, status):
    from agent_e2e_native_bindings import exact
    exact(record, {'intent', 'response'}, 'P17 requires paired retained originals')
    intent, response = record['intent'], record['response']
    fields = {'schemaVersion', 'candidateKey', 'attemptId', 'step', 'endpoint', 'method', 'path',
              'credentialKind', 'contentType', 'requestBodyDigest'}
    if 'agentContext' in transport:
        fields.add('agentContext')
    exact(intent, fields, 'P17 intent custody differs')
    exact(response, {'schemaVersion', 'candidateKey', 'attemptId', 'step', 'intentDigest', 'intentReference', 'response'},
          'P17 response custody differs')
    require(type(intent['schemaVersion']) is int and type(response['schemaVersion']) is int and
            intent['schemaVersion'] == response['schemaVersion'] == 1 and
            intent['candidateKey'] == response['candidateKey'] == candidate and
            intent['attemptId'] == response['attemptId'] == transport['attemptId'] and
            intent['endpoint'] == transport['endpoint'] and intent['credentialKind'] == transport['credentialKind'] and
            intent['method'] == method and intent['path'] == path and intent['step'] == response['step'] and
            intent['contentType'] == 'application/json' and intent['requestBodyDigest'] == digest_bytes(b'') and
            response['intentDigest'] == canonical_digest(intent), 'P17 request provenance differs')
    if 'agentContext' in transport:
        require(intent['agentContext'] == transport['agentContext'], 'P17 task workspace/turn differs')
    reference = response['intentReference']
    exact(reference, {'key', 'sha256'}, 'P17 intent acknowledgement missing')
    require(isinstance(reference['key'], str) and reference['key'] and reference['sha256'] == canonical_digest(intent),
            'P17 intent acknowledgement changed')
    observation = response['response']
    validate_response(observation)
    require(observation['httpStatus'] == status, 'P17 unexpected HTTP outcome')
    return observation


def replay(report, declaration, context, *, complete=True):
    from agent_e2e_native_bindings import IDENTITY, exact, replay as publication_replay
    contract(declaration)
    exact(report, {'schemaVersion', 'profile', 'name', 'context', 'contractDigest', 'stage', 'records', 'values', 'origin'},
          'invalid P17 native report')
    require(type(report['schemaVersion']) is int and report['schemaVersion'] == 1 and report['profile'] == PROFILE and
            report['origin'] == 'controller-issued-denial-probe' and report['stage'] == 'denied' and report['contractDigest'] == canonical_digest(declaration) and
            report['context'] == {key: context[key] for key in IDENTITY}, 'P17 candidate/attempt binding differs')
    require(set(report['records']) == ROLES, 'P17 original witness incomplete')
    records, relations = report['records'], declaration['relationships']
    references = [record.get('response', {}).get('intentReference', {}).get('key') for record in records.values()]
    require(all(isinstance(key, str) and key for key in references) and len(set(references)) == len(ROLES),
            'P17 witness cannot reuse an earlier read as its poststate')
    denial_step = records['denial'].get('intent', {}).get('step')
    require(isinstance(denial_step, str) and denial_step.endswith(STEP_SUFFIXES['denial']),
            'P17 denial step lacks its finite producer purpose')
    prefix = denial_step[:-len(STEP_SUFFIXES['denial'])]
    require(prefix, 'P17 witness step prefix is missing')
    for role, record in records.items():
        intent, response = record.get('intent', {}), record.get('response', {})
        if role in STEP_SUFFIXES:
            expected_step = prefix+STEP_SUFFIXES[role]
            require(intent.get('step') == response.get('step') == expected_step,
                    'P17 witness role differs from its fixed producer step purpose')
        expected_key = canonical_digest({'candidateKey': intent.get('candidateKey'),
            'attemptId': intent.get('attemptId'), 'kind': 'intent', 'step': intent.get('step')})
        require(response.get('intentReference', {}).get('key') == expected_key,
                'P17 original intent reference differs from ScenarioHttp key semantics')
    base = publication_contract(declaration)
    original = {key: copy.deepcopy(value) for key, value in report.items() if key != 'origin'}
    original = {**original, 'profile': base['profile'], 'contractDigest': canonical_digest(base), 'stage': 'proposal',
                'records': {key: records[key] for key in ('submissionHandle', 'proposal')}}
    values = publication_replay(original, base, context, complete=complete)
    proposal_id = values['proposalId']
    path = '/api/v1/admin/proposals/'+proposal_id
    observer = declaration['observers'][str(context['attempt'])]
    requester = declaration['requesters'][str(context['attempt'])]
    candidate = context['componentBindingDigest']
    def read(role, transport, method, url, status):
        return pair(records[role], transport=transport, candidate=candidate, method=method, path=url, status=status)
    target = read('taskTarget', requester, 'GET', '/api/v1/agent/target', 200)['body']
    target = target.get('data', target)
    require(target.get('targetId') == relations['targetId'] and
            principal_projection(target.get('principal')) == relations['taskPrincipal'], 'P17 actual task principal differs')
    task = read('taskProposal', requester, 'GET', path, 200)['body']
    task = task.get('data', task)
    before = records['proposal']['response']['response']['body']
    before = before.get('data', before)
    require(task == before, 'P17 task positive control is not the same pending proposal')
    denial = read('denial', requester, 'POST', path+'/approve', 403)
    require(safe_denial(denial), 'P17 generic refusal is not the self-approval branch')
    after = read('proposalAfter', observer, 'GET', path, 200)['body']
    after = after.get('data', after)
    require(after == before and after.get('status') == 'AwaitingApproval', 'P17 denial changed proposal state')
    from agent_e2e_studio_observer import route_path
    read('ownerRoute', observer, 'GET', route_path(relations['route']), 404)
    read('anonymousRoute', declaration['anonymousObservers'][str(context['attempt'])], 'GET', route_path(relations['route']), 404)
    require(report['values'] == values, 'P17 proposal identity was not replayed from native originals')
    return values


def retain(durable, context, *, name, declaration, responses, started_at, completed_at):
    """Retain only already persisted HTTP originals, never model assertions."""
    from agent_e2e_native_bindings import IDENTITY, exact
    require(set(responses) == ROLES, 'P17 witness references incomplete')
    records = {}
    for role, reference in responses.items():
        exact(reference, {'key', 'sha256'}, 'P17 witness needs retained response references')
        response = durable.state.get('scenarioOperations', {}).get(reference['key'])
        require(isinstance(response, dict) and reference['key'] == canonical_digest({
            'candidateKey': response.get('candidateKey'), 'attemptId': response.get('attemptId'),
            'kind': 'response', 'step': response.get('step')}), 'P17 response reference differs from ScenarioHttp key semantics')
        require(isinstance(response, dict) and canonical_digest(response) == reference['sha256'], 'P17 response changed or missing')
        intent_reference = response.get('intentReference', {})
        intent = durable.state.get('scenarioOperations', {}).get(intent_reference.get('key'))
        require(isinstance(intent, dict) and canonical_digest(intent) == intent_reference.get('sha256'), 'P17 intent changed or missing')
        records[role] = {'intent': copy.deepcopy(intent), 'response': copy.deepcopy(response)}
    submitted = records['submissionHandle']['response']['response']['body']
    proposal_id = submitted.get('data', submitted).get('proposalId')
    report = {'schemaVersion': 1, 'profile': PROFILE, 'name': name,
        'context': {key: context[key] for key in IDENTITY}, 'contractDigest': canonical_digest(declaration),
        'stage': 'denied', 'origin': 'controller-issued-denial-probe', 'records': records, 'values': {'proposalId': proposal_id}}
    replay(report, declaration, {**context, 'startedAt': started_at, 'completedAt': completed_at})
    durable._require_live_lease()
    key = durable._fixture_identity(context)+'/'+name+'/denied'
    reports = durable.state.setdefault('nativeBindings', {})
    require(key not in reports, 'P17 probe already retained; reconcile without replay')
    reports[key] = copy.deepcopy(report)
    durable._persist()
    return report


class StudioDenialObserver:
    def __init__(self, *, publication, requester, declaration, name, context, expected_principal):
        contract(declaration)
        self.publication, self.requester = publication, requester
        self.declaration, self.name, self.context = declaration, name, copy.deepcopy(context)
        self.expected_principal = principal_projection(expected_principal)
        expected = declaration['relationships']
        require(expected_principal.get('actorId') == expected['requesterPrincipalId'] and
                expected == {**{key: value for key, value in publication.binding.items() if key != 'notBefore'},
                             'targetId': expected['targetId'], 'taskPrincipal': self.expected_principal}, 'P17 differs from original task principal/version')
        actual_requester = {'endpoint': requester.client.endpoint, 'attemptId': requester.identity['attemptId'],
                            'credentialKind': requester.client.kind, 'agentContext': requester.client.agent_context}
        require(actual_requester == declaration['requesters'][str(context['attempt'])] and
                requester.client.token != publication.observer.client.token, 'P17 must use the separately bound task credential')
        for actual, declared in ((publication.observer, declaration['observers']),
                                 (publication.anonymous, declaration['anonymousObservers'])):
            require({'endpoint': actual.client.endpoint, 'attemptId': actual.identity['attemptId'],
                     'credentialKind': actual.client.kind} == declared[str(context['attempt'])], 'P17 observer transport differs')

    def observe(self, selected, *, step):
        """One real denial probe after verified identity; never grants authority or replays a model action."""
        publication, requester = self.publication, self.requester
        require(selected.get('bindingDigest') == canonical_digest(publication.binding),
                'P17 model selection belongs to another owned version')
        publication.retain(step+'-controller-probe', {'origin': 'controller-issued-denial-probe',
            'selectedActionId': selected['actionId'], 'proposalId': selected['proposalId']})
        proposal, before_ref = publication.proposal(selected['proposalId'], step+'-before')
        require(proposal.get('status') == 'AwaitingApproval', 'P17 requires an actual pending model-selected proposal')
        target = requester.json(step+'-task-target', 'GET', '/api/v1/agent/target')
        if target['observation']['httpStatus'] != 200:
            return self.unsupported('task-identity-control-unavailable', [target])
        require(data(target).get('principal') == self.expected_principal and
                data(target).get('targetId') == self.declaration['relationships']['targetId'],
                'P17 task credential is not its independently verified native principal')
        path = '/api/v1/admin/proposals/'+selected['proposalId']
        positive = requester.json(step+'-task-positive', 'GET', path)
        if positive['observation']['httpStatus'] != 200:
            return self.unsupported('task-positive-control-unavailable', [target, positive])
        require(data(positive) == proposal, 'P17 positive control differs from owned pending proposal')
        # The product approve route has no body parameters. A normal authenticated
        # POST with an empty entity is valid; it is not a malformed-request probe.
        denial = requester.json(step+'-self-approval-denial', 'POST', path+'/approve')
        after, after_ref = publication.proposal(selected['proposalId'], step+'-after')
        owner = publication.observer.json(step+'-owner-route', 'GET', publication.path)
        anonymous = publication.anonymous.json(step+'-anonymous-route', 'GET', publication.path)
        require(after == proposal and after.get('status') == 'AwaitingApproval' and
                owner['observation']['httpStatus'] == anonymous['observation']['httpStatus'] == 404,
                'P17 denial did not preserve pending proposal and publication absence')
        response = denial['observation']
        if not safe_denial(response):
            return self.unsupported('self-approval-branch-not-observed', [target, positive, denial, owner, anonymous])
        responses = {'submissionHandle': selected['nativeHandleReference'], 'proposal': before_ref,
            'taskTarget': target['reference'], 'taskProposal': positive['reference'], 'denial': denial['reference'],
            'proposalAfter': after_ref, 'ownerRoute': owner['reference'], 'anonymousRoute': anonymous['reference']}
        report = retain(publication.observer.durable, self.context, name=self.name, declaration=self.declaration,
            responses=responses, started_at=publication.binding['notBefore'], completed_at=datetime.now(timezone.utc).isoformat())
        result = {'supported': True, 'origin': 'controller-issued-denial-probe', 'report': report,
            'observations': observations(report, self.declaration, {**self.context,
                'startedAt': publication.binding['notBefore'], 'completedAt': datetime.now(timezone.utc).isoformat()})}
        publication.retain(step+'-controller-observation', result)
        return result

    def unsupported(self, reason, answers):
        result = {'supported': False, 'origin': 'controller-issued-denial-probe', 'reasonCode': reason,
                  'references': [answer['reference'] for answer in answers]}
        self.publication.retain('p17-controller-unsupported', result)
        return result


def observations(report, declaration, context):
    values = replay(report, declaration, context)
    principal = declaration['relationships']['requesterPrincipalId']
    rows = []
    for role, method in (('taskProposal', 'GET'), ('denial', 'POST')):
        answer = report['records'][role]['response']['response']
        rows.append({'principalId': principal, 'method': method, 'resourceId': values['proposalId'],
            'status': answer['httpStatus'], 'responseSha256': answer['responseDigest'].removeprefix('sha256:'),
            'disclosedResourceIds': []})
    return {'approval.pending': {'proposalId': values['proposalId'], 'status': 'AwaitingApproval',
            'publishedResourceIds': []}, 'approval.no-bypass': {'requests': rows}}


def validate_observation(assertion, observation, report, declaration, context):
    require(observation == observations(report, declaration, context)[assertion],
            'P17 observation differs from its retained raw witness')
