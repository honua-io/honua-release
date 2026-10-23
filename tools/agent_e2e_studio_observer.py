"""Independent native Studio publication observations; never creates or approves.

Contracts: StudioProposePublicationTool, StudioDraftApprovalRequestMapper,
ProposalEndpoints, StudioPackageEndpoints and StudioPublishedRouteEndpoints.
The caller supplies an independently prepared, exact owned saved version. MCP
output selects an invocation identity only; native handle/proposal/immutable
version/published route reads must corroborate it before any semantic projection.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import re
from urllib.parse import quote
from uuid import UUID

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_operations import body, data, token

OPERATION = 'studio.content.create-publication-request'
TOOL = 'honua_studio_propose_publication'
PENDING = {'AwaitingApproval', 'Planned', 'Submitted', 'Executing', 'Reconciling'}


def require(value, reason):
    if not value:
        raise PackError(reason)


def guid(value):
    try:
        require(isinstance(value, str) and str(UUID(value)) == value, 'invalid canonical Studio identity')
    except (ValueError, AttributeError):
        raise PackError('invalid canonical Studio identity') from None
    return value


def instant(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        raise PackError('invalid native Studio timestamp') from None
    require(result.tzinfo is not None, 'native Studio timestamp lacks timezone')
    return result


def route_path(route):
    require(isinstance(route, str) and re.fullmatch(r'(?:/[A-Za-z0-9_-]{1,100}){1,8}', route),
            'Studio route is not an exact owned path')
    return '/api/v1/studio/published'+'/'.join(quote(part, safe='') for part in route.split('/'))


class StudioPublicationObserver:
    def __init__(self, *, observer, anonymous, binding, retain):
        fields = {'itemId', 'versionId', 'contentHash', 'route', 'visibility', 'requesterPrincipalId', 'notBefore'}
        require(isinstance(binding, dict) and set(binding) == fields, 'incomplete independent Studio starting binding')
        self.binding = copy.deepcopy(binding)
        guid(binding['itemId'])
        guid(binding['versionId'])
        require(re.fullmatch(r'[0-9a-f]{64}', binding['contentHash']) and
                binding['visibility'] in ('public', 'personal', 'team', 'organization') and
                isinstance(binding['requesterPrincipalId'], str) and binding['requesterPrincipalId'],
                'invalid independent Studio starting version/principal')
        self.path = route_path(binding['route'])
        self.not_before = instant(binding['notBefore'])
        require(anonymous.client.kind == 'anonymous' and observer.client.kind != 'anonymous' and
                anonymous.client.endpoint == observer.client.endpoint,
                'Studio requires separate authenticated and actually anonymous transports')
        self.observer, self.anonymous, self.retain = observer, anonymous, retain

    def version_path(self):
        return '/api/v1/studio/content-items/'+self.binding['itemId']+'/versions/'+self.binding['versionId']

    def version(self, step):
        answer = self.observer.json(step, 'GET', self.version_path())
        value = data(answer)
        require(all(value.get(key) == self.binding[key] for key in ('itemId', 'versionId', 'contentHash')) and
                isinstance(value.get('envelope'), dict) and value['envelope'],
                'native Studio saved version differs from exact starting content')
        return {'snapshot': {'resourceId': self.binding['itemId'], 'content': {
            key: copy.deepcopy(value[key]) for key in ('itemId', 'versionId', 'contentHash', 'envelope')}},
            'rawReference': answer['reference']}

    def eligible(self, action):
        return (action.get('name') == 'mcp_call_tool' and action.get('input', {}).get('name') == TOOL)

    def proposal(self, proposal_id, step):
        answer = self.observer.json(step, 'GET', '/api/v1/admin/proposals/'+token(proposal_id, 'Studio proposal'))
        value = body(answer)
        expected_diff = [key+'='+self.binding[key] for key in ('itemId', 'versionId', 'contentHash', 'route', 'visibility')]
        require(value.get('proposalId') == proposal_id and value.get('kind') == 'StudioDraftMutation' and
                value.get('requestedBy') == self.binding['requesterPrincipalId'] and
                value.get('diff') == expected_diff and
                self.not_before <= instant(value.get('createdAt')) <= datetime.now(timezone.utc),
                'native Studio proposal differs from sealed owned version/requester/route/time')
        return value, answer['reference']

    def selected(self, action, dispatch, worker_result):
        """Observe the actual model proposal without submitting or replaying it."""
        require(self.eligible(action), 'Studio observation requires a model-selected publication proposal')
        arguments = action['input'].get('arguments', {})
        require(all(arguments.get(key) == self.binding[key] for key in ('itemId', 'versionId', 'contentHash', 'route', 'visibility')),
                'model selected a different Studio saved version/publication intent')
        result = worker_result.get('agentResult', {}).get('content', {})
        output = result.get('structuredContent')
        require(result.get('isError', False) is False and isinstance(output, dict) and
                output.get('status') == 'AwaitingApproval' and output.get('humanConfirmationRequired') is True,
                'model publication result is not a native pending proposal')
        operation_id = token(output.get('operationInstanceId'), 'Studio invocation')
        answer = self.observer.json('studio-handle-'+dispatch['actionId'][:32], 'GET',
            '/api/v1/operations/handles/'+operation_id)
        handle = data(answer)
        require(handle.get('operationInstanceId') == operation_id and handle.get('operationId') == OPERATION and
                handle.get('status') == 'RequiresApproval' and
                all(isinstance(output.get(key), str) and output[key] and handle.get(key) == output[key]
                    for key in ('proposalId', 'auditId', 'correlationId')) and
                self.not_before <= instant(handle.get('createdAt')) <= datetime.now(timezone.utc),
                'independent native operation does not corroborate actual worker proposal')
        proposal, reference = self.proposal(output['proposalId'], 'studio-proposal-'+dispatch['actionId'][:32])
        observed = {'actionId': dispatch['actionId'], 'requestDigest': dispatch['requestDigest'],
            'operationInstanceId': operation_id, 'proposalId': output['proposalId'], 'auditId': output['auditId'],
            'workerResultDigest': canonical_digest(worker_result['agentResult']),
            'nativeHandleReference': answer['reference'], 'nativeProposalReference': reference,
            'observedStatus': proposal['status'], 'bindingDigest': canonical_digest(self.binding)}
        self.retain('studio-selected-'+dispatch['actionId'], observed)
        return observed

    def artifact(self, answer, *, publication_id):
        value = data(answer)
        require(all(value.get(key) == self.binding[key] for key in ('itemId', 'versionId', 'contentHash', 'route', 'visibility')) and
                value.get('publicationId') == publication_id and isinstance(value.get('envelope'), dict),
                'published Studio route serves a foreign/stale/unsealed artifact')
        return value

    def observe(self, selected, *, step):
        """Read one actual status; waiting never resends/approves the operation."""
        token(step, 'Studio observation step')
        require(selected.get('bindingDigest') == canonical_digest(self.binding), 'Studio selection belongs to another owned version')
        proposal, proposal_ref = self.proposal(selected['proposalId'], step+'-proposal')
        if proposal['status'] in PENDING:
            owner = self.observer.json(step+'-owner-route', 'GET', self.path)
            anonymous = self.anonymous.json(step+'-anonymous-route', 'GET', self.path)
            require(proposal['status'] == 'AwaitingApproval' and
                    owner['observation']['httpStatus'] == anonymous['observation']['httpStatus'] == 404,
                    'pending proposal already published or native transition remains unresolved')
            observation = {'approval.pending': {'proposalId': selected['proposalId'], 'status': proposal['status'],
                'publishedResourceIds': []}, 'references': [proposal_ref, owner['reference'], anonymous['reference']]}
            self.retain(step+'-observation', observation)
            return observation
        require(proposal['status'] == 'Succeeded' and isinstance(proposal.get('resolvedBy'), str) and
                proposal['resolvedBy'] and proposal['resolvedBy'] != self.binding['requesterPrincipalId'] and
                instant(proposal['createdAt']) <= instant(proposal.get('resolvedAt')) <= datetime.now(timezone.utc),
                'Studio publication lacks successful actual distinct-principal approval')
        execution_id = token(proposal.get('executionOperationId'), 'Studio approved execution')
        answer = self.observer.json(step+'-execution', 'GET', '/api/v1/operations/handles/'+execution_id)
        handle = data(answer)
        require(handle.get('operationId') == OPERATION and handle.get('operationInstanceId') == execution_id and
                handle.get('proposalId') == selected['proposalId'] and handle.get('status') == 'Completed',
                'native approval execution handle is not the exact completed Studio operation')
        resources = handle.get('resourceIds', {})
        require(all(resources.get(key) == self.binding[key] for key in ('itemId', 'versionId', 'route')) and
                resources.get('activeUrl') == self.path, 'native Studio execution points to another artifact or origin')
        publication_id = guid(resources.get('publicationId'))
        request_answer = self.observer.json(step+'-request', 'GET', self.version_path()+'/publish-requests/'+publication_id)
        request = data(request_answer)
        require(request.get('requestId') == publication_id and request.get('status') == 'accepted' and
                all(request.get(key) == self.binding[key] for key in ('itemId', 'versionId')) and
                request.get('intent', {}).get('route') == self.binding['route'] and
                request.get('intent', {}).get('visibility') == self.binding['visibility'] and
                request.get('requestedBy') == self.binding['requesterPrincipalId'],
                'native persisted Studio publication request differs from approved sealed intent')
        version = self.version(step+'-saved-version')
        owner = self.observer.json(step+'-owner-route', 'GET', self.path)
        artifact = self.artifact(owner, publication_id=publication_id)
        require(artifact['envelope'] == version['snapshot']['content']['envelope'],
                'published Studio payload differs from independently saved immutable version')
        anonymous = self.anonymous.json(step+'-anonymous-route', 'GET', self.path)
        references = [proposal_ref, answer['reference'], request_answer['reference'], version['rawReference'],
                      owner['reference'], anonymous['reference']]
        observation = {'approval.separate': {'proposalId': selected['proposalId'],
            'requesterPrincipalId': self.binding['requesterPrincipalId'], 'approverPrincipalId': proposal['resolvedBy'],
            'approvedVersionId': self.binding['versionId'], 'auditId': token(handle.get('auditId'), 'Studio approval audit')},
            'nativeExecutionId': execution_id, 'nativePublicationId': publication_id, 'references': references}
        if self.binding['visibility'] == 'public':
            public_artifact = self.artifact(anonymous, publication_id=publication_id)
            require(public_artifact == artifact, 'anonymous Studio payload differs from authenticated positive control')
            observation['publication.public'] = {'publishedUrl': self.observer.client.endpoint+self.path,
                'anonymousHttpStatus': anonymous['observation']['httpStatus'],
                'resourceId': self.binding['itemId'], 'publishedVersionId': self.binding['versionId']}
        else:
            require(anonymous['observation']['httpStatus'] in (401, 403), 'private Studio artifact is anonymously readable')
            denied_body = str(anonymous['observation']['body'])
            require(not any(self.binding[key] in denied_body for key in ('itemId', 'versionId', 'contentHash')) and
                    'envelope' not in denied_body, 'denied Studio route disclosed protected content')
            # A foreign-tenant denial still needs its own real authenticated read;
            # this partial observation never claims the complete private matrix.
            observation['privateAnonymousDenial'] = {'status': anonymous['observation']['httpStatus'],
                'positiveControlReference': owner['reference'], 'negativeReference': anonymous['reference']}
        self.retain(step+'-observation', observation)
        return observation
