"""Controller backend routing for MCP actions, with retained intent and replies.

This component implements the MCP branch of agent_action. Scenario setup,
approvals, independent verification and cleanup remain the owning backend's work.
Worker output and correlation headers are observations, not independent oracles.
"""
from __future__ import annotations

import copy
import re

from agent_e2e_controller import admitted_resource_actions, validate_action
from agent_e2e_mcp import (McpError, RESOURCE_ACTIONS, validate_resource_catalog,
                           validate_resource_result)
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_telemetry import validate_events, validate_values
from agent_e2e_worker import encode


def verify_dispatch(state, candidate, launch, context, action, dispatch, *, names=('mcp_list_tools', 'mcp_call_tool')):
    validate_action(action)
    if action['name'] not in names:
        raise PackError('backend cannot execute another action interface')
    if (not isinstance(context, dict) or context.get('componentBindingDigest') != candidate
            or any(context.get(k) != v for k, v in launch['context'].items())):
        raise PackError('MCP action belongs to another candidate or attempt')
    if (not isinstance(dispatch, dict) or set(dispatch) != {'ordinal', 'actionId', 'intentDigest', 'requestDigest'}
            or type(dispatch['ordinal']) is not int or dispatch['ordinal'] < 1):
        raise PackError('invalid controller dispatch identity')
    identity = {k: context[k] for k in ('campaignId', 'caseId', 'attempt')}
    action_id = canonical_digest({**identity, 'componentBindingDigest': candidate, 'ordinal': dispatch['ordinal']})[7:]
    request_digest = canonical_digest({k: action[k] for k in ('name', 'input')})
    rows = state.get('journals', {}).get(canonical_digest(identity), [])
    validate_events(rows)
    contexts = [row for row in rows if row['event'] == 'context']
    if (len(contexts) != 1 or contexts[0]['campaignMode'] != context['campaignMode']
            or 'knowledge' not in context or contexts[0]['knowledge'] != context['knowledge']):
        raise PackError('MCP action differs from retained campaign mode or knowledge')
    intent = rows[-1]
    if (dispatch['actionId'] != action_id or dispatch['requestDigest'] != request_digest
            or intent['event'] != 'action-intent' or intent['action'] != dispatch['ordinal']
            or intent['kind'] != action['name'] or intent['requestDigest'] != request_digest
            or canonical_digest(intent) != dispatch['intentDigest']):
        raise PackError('MCP action lacks its latest retained controller intent')
    if action_id in state.get('workerActions', {}):
        raise PackError('MCP action already has retained evidence; do not redispatch')


def public_result(reply, action):
    if reply['status'] != 'completed':
        raise PackError('worker action did not complete; retain evidence for reconciliation')
    payload = reply['payload']
    if (not isinstance(payload, dict) or set(payload) != {'result', 'error', 'observations', 'sessionCleanup'}
            or not isinstance(payload['observations'], list)
            or payload['sessionCleanup'] != {'status': 'pass'}):
        raise PackError('MCP payload or session cleanup is incomplete')
    error, result = payload['error'], payload['result']
    if error is not None:
        if (result is not None or not isinstance(error, dict) or set(error) != {'category', 'rpcError'}
                or not isinstance(error['category'], str) or not re.fullmatch(r'[a-z0-9-]{1,80}', error['category'])
                or error['rpcError'] is not None and not isinstance(error['rpcError'], dict)):
            raise PackError('invalid worker MCP error')
        category = {'http-401': 'authorization', 'http-403': 'authorization', 'http-404': 'not-found',
                    'http-409': 'conflict', 'http-429': 'rate-limit', 'http-503': 'unavailable'}.get(error['category'], 'other')
        public = {'content': {'error': copy.deepcopy(error)}, 'isError': True, 'errorCategory': category}
    else:
        if not isinstance(result, dict):
            raise PackError('missing worker MCP result')
        if action['name'] == 'mcp_list_tools':
            tools = result.get('tools')
            if not isinstance(tools, list) or len(tools) > 1024:
                raise PackError('invalid worker tool catalog')
            names = set()
            for tool in tools:
                if (not isinstance(tool, dict) or not isinstance(tool.get('name'), str)
                        or not tool['name'].strip() or len(tool['name']) > 256
                        or not isinstance(tool.get('inputSchema'), dict) or tool['name'] in names):
                    raise PackError('invalid or duplicate worker tool descriptor')
                names.add(tool['name'])
            is_error = False
        elif action['name'] in RESOURCE_ACTIONS:
            try:
                if action['name'] == 'mcp_read_resource':
                    validate_resource_result(result, action['input']['uri'])
                else:
                    validate_resource_catalog(result, templates=action['name'] == 'mcp_list_resource_templates')
            except McpError:
                raise PackError('invalid worker resource result') from None
            is_error = False
        else:
            if not isinstance(result.get('content'), list) or type(result.get('isError', False)) is not bool:
                raise PackError('invalid worker tool result')
            is_error = result.get('isError', False)
        public = {'content': copy.deepcopy(result), 'isError': is_error, 'errorCategory': 'other' if is_error else None}
    if len(encode(public).encode()) > 600000:
        raise PackError('public MCP result exceeds controller response bound')
    return public


def telemetry(reply, action, ordinal):
    """Retain only bounded correlations; no raw bodies or worker secrets in logs."""
    payload = reply['payload']
    events = []
    methods = {'mcp_list_tools': 'tools/list', 'mcp_call_tool': 'tools/call',
               'mcp_list_resources': 'resources/list', 'mcp_list_resource_templates': 'resources/templates/list',
               'mcp_read_resource': 'resources/read'}
    method = methods[action['name']]
    if action['name'] in ('mcp_list_tools', 'mcp_list_resources', 'mcp_list_resource_templates') and payload['error'] is None:
        events.append({'event': 'discovery', 'interfaceKind': 'mcp', 'operation': method,
                       'schemaDigest': canonical_digest(payload['result']),
                       'observationDigest': canonical_digest(payload['observations'])})
    # Raw headers stay retained even if invalid/missing; never fabricate a trace.
    for observation in payload['observations']:
        if not isinstance(observation, dict) or observation.get('rpcMethod') != method:
            continue
        headers = observation.get('headers', {})
        if not isinstance(headers, dict):
            continue
        trace = headers.get('traceparent')
        match = re.fullmatch(r'00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}', trace) if isinstance(trace, str) else None
        if match is None:
            continue
        event = {'action': ordinal, 'traceId': match[1], 'spanId': match[2],
                 'requestId': headers.get('X-Request-Id'), 'evidenceDigest': canonical_digest(observation)}
        try:
            validate_values('runtime-trace', event)
        except ValueError:
            continue
        events.append({'event': 'runtime-trace', **event})
    if len(events) > 128:
        raise PackError('excessive MCP correlation events')
    return events


class McpActions:
    """Use inside the trusted backend with its existing durable controller lease."""
    def __init__(self, *, durable, lifecycle, launch, worker, evidence_factory, operation_policy=None):
        self.durable, self.lifecycle = durable, lifecycle
        self.launch, self.worker, self.evidence_factory = copy.deepcopy(launch), worker, evidence_factory
        resources = admitted_resource_actions(operation_policy)
        self.allowed_actions = ('mcp_list_tools', 'mcp_call_tool', *(name for name in RESOURCE_ACTIONS if name in resources))
        self._check_transport()
        lifecycle._check_record(self.launch)

    def _check_transport(self):
        config = self.lifecycle.config
        if (canonical_digest(self.worker.config) != config['transportDigest']
                or self.worker.config['attemptId'] != config['attemptId']
                or self.worker.config['expiresEpoch'] != config['expiresEpoch']):
            raise PackError('MCP transport differs from reviewed worker launch')

    def execute(self, context, action, dispatch, *, timeout=120):
        def authorize():
            self._check_transport()
            self.durable.require_worker_launch(self.launch, launching=True)
            verify_dispatch(self.durable.state, self.durable.store.candidate, self.launch, context, action, dispatch,
                            names=self.allowed_actions)
        authorize()
        observed = self.lifecycle.observe(self.launch)
        if (observed['executionStatus'] != 'RUNNING' or observed['taskStatus'] != 'RUNNING'
                or observed['stopRecorded'] is not False):
            raise PackError('MCP worker is not independently observed running')
        evidence = self.evidence_factory(dispatch['actionId'])
        expected = {'candidateKey': self.launch['candidateKey'], 'attemptId': self.launch['attemptId'], 'actionId': dispatch['actionId']}
        if evidence.identity != expected:
            raise PackError('MCP evidence store belongs to another action')
        def retain_dispatch(binding):
            authorize()
            reference = evidence.retain_dispatch(binding)
            authorize()  # A failed/slow retention must not outlive the lease.
            return reference
        reply = self.worker.execute_mcp({k: action[k] for k in ('name', 'input')}, dispatch['actionId'],
                                        retain_dispatch=retain_dispatch, retain_chunk=evidence, timeout=timeout)
        retained = evidence.recover()
        binding = evidence.load_dispatch()
        if (retained['reply'] != reply or any(reply.get(k) != v for k, v in binding['binding'].items())):
            raise PackError('MCP reply differs from retained dispatch/evidence')
        record = {'schemaVersion': 1, **expected, 'launchDigest': canonical_digest(self.launch),
                  'context': copy.deepcopy(self.launch['context']), 'ordinal': dispatch['ordinal'],
                  'requestDigest': dispatch['requestDigest'], 'intentDigest': dispatch['intentDigest'],
                  'dispatch': binding['reference'], 'header': retained['header'], 'chunks': retained['chunks'],
                  'replyDigest': canonical_digest(reply), 'evidenceBucket': evidence.bucket}
        # Preserve references even when payload validation subsequently blocks.
        self.durable.retain_worker_action(record, self.launch)
        public = public_result(reply, action)
        events = telemetry(reply, action, dispatch['ordinal'])
        category = ('discovery' if action['name'] in ('mcp_list_tools', 'mcp_list_resources', 'mcp_list_resource_templates')
                    else 'read' if action['name'] == 'mcp_read_resource' else 'other')
        return {'agentResult': public, 'operationCategory': category,
                'telemetry': events}
