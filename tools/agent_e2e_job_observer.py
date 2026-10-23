"""Independent native reads for the model's managed-buffer job.

The original MCP action selects a job; a native GET must join its accepted
fingerprint, requester and creation time before any output is used. This
observer never submits, polls by mutation, fabricates a missed lifecycle
transition, or derives expected geometry/CRS from the response.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlsplit

from agent_e2e_gp_fingerprint import fingerprint
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_backend import successful
from agent_e2e_studio_observer import instant

EXECUTE = 'honua_execute_plan'
PROCESS = 'analytics.buffer-aggregate-managed'
DATA_PREFIX = 'data:application/geo+json;base64,'


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value), 'invalid native job identity')
    return value


def selected_plan(action):
    require(isinstance(action, dict) and action.get('name') == 'mcp_call_tool' and
            isinstance(action.get('input'), dict) and set(action['input']) == {'name', 'arguments'} and
            action['input']['name'] == EXECUTE, 'job observer requires the original asynchronous execute action')
    arguments = action['input']['arguments']
    require(isinstance(arguments, dict) and set(arguments) <= {'plan', 'idempotencyKey'} and 'plan' in arguments,
            'unreviewed managed job execution controls')
    plan = arguments['plan']
    digest = fingerprint(plan)
    steps = plan.get('steps') or []
    require(len(steps) == 1 and steps[0].get('kind', '').strip().lower() == 'geoprocess' and
            steps[0].get('processId') == PROCESS and not steps[0].get('dependsOn') and
            [value.strip().lower() for value in (plan.get('outputs') or [])] == ['featurelayer'],
            'job observer admits only the reviewed single managed-buffer output')
    return digest


def detail(value, *, selection, binding, observed_at):
    require(isinstance(value, dict) and value.get('jobId') == selection['jobId'] and
            value.get('kind') == 'Geoprocessing' and value.get('requestedBy') == binding['requesterPrincipalId'] and
            value.get('requestFingerprint') == selection['requestFingerprint'],
            'native job does not join the exact original model plan/requester')
    created, updated = instant(value.get('createdAt')), instant(value.get('updatedAt'))
    require(instant(binding['notBefore']) <= created == instant(selection['createdAt']) <= updated <= observed_at,
            'native job is outside original attempt or has inconsistent timestamps')
    state = value.get('status')
    require(state in ('Queued', 'Running', 'Succeeded', 'Failed', 'Cancelled', 'Retrying', 'CancelRequested'),
            'native job has an unsupported lifecycle state')
    if state in ('Succeeded', 'Failed', 'Cancelled'):
        require(created <= instant(value.get('completedAt')) <= updated, 'terminal native job lacks actual completion time')
    return copy.deepcopy(value)


def metric_buffer(document):
    require(isinstance(document, dict) and document.get('type') == 'FeatureCollection' and
            document.get('processId') == PROCESS and type(document.get('featureCount')) is int and
            document['featureCount'] == 1 and isinstance(document.get('features'), list) and len(document['features']) == 1,
            'native output is not one managed-buffer feature')
    crs = document.get('honua:spatialReference')
    require(isinstance(crs, dict) and crs.get('schemaVersion') == 'honua.feature-crs/v1' and
            crs.get('state') == 'known' and type(crs.get('srid')) is int and crs['srid'] == 3857,
            'native artifact has no producer-proven EPSG:3857 coordinate frame')
    feature = document['features'][0]
    require(isinstance(feature, dict) and feature.get('type') == 'Feature' and
            isinstance(feature.get('properties'), dict) and isinstance(feature.get('geometry'), dict),
            'native buffer output lacks actual geometry or source identity')
    source = feature['properties'].get('source_id')
    require(isinstance(source, str) and source and feature['geometry'].get('type') == 'Polygon',
            'native buffer output lacks preserved source_id or polygon')
    return {'srid': crs['srid'], 'sourceId': source, 'geometry': copy.deepcopy(feature['geometry'])}


class JobObserver:
    def __init__(self, *, http, binding, retain):
        require(isinstance(binding, dict) and set(binding) == {'requesterPrincipalId', 'notBefore'} and
                isinstance(binding['requesterPrincipalId'], str) and binding['requesterPrincipalId'],
                'native job observer requires exact original requester/attempt scope')
        instant(binding['notBefore'])
        self.http, self.binding, self.retain = http, copy.deepcopy(binding), retain

    @staticmethod
    def eligible(action):
        return action.get('name') == 'mcp_call_tool' and action.get('input', {}).get('name') == EXECUTE

    def selected(self, action, dispatch, worker_result):
        digest = selected_plan(action)
        request = {key: copy.deepcopy(action[key]) for key in ('name', 'input')}
        require(dispatch.get('requestDigest') == canonical_digest(request) and
                type(dispatch.get('ordinal')) is int and dispatch['ordinal'] > 0,
                'job selection differs from original dispatched model request')
        action_id = identifier(dispatch.get('actionId'))
        public = worker_result.get('agentResult', {})
        require(public.get('isError') is False and public.get('errorCategory') is None and
                isinstance(public.get('content'), dict) and public['content'].get('isError', False) is False,
                'model execute result is not successful')
        output = public['content'].get('structuredContent')
        require(isinstance(output, dict), 'model execute result has no actual native job output')
        job = identifier(output.get('jobId'))
        require(output.get('resourceUri') == 'honua://jobs/'+job, 'model execute resource is not this native job')
        selection = {'jobId': job, 'requestFingerprint': digest, 'createdAt': output.get('createdAt'),
            'ordinal': dispatch['ordinal'], 'actionId': action_id, 'request': request, 'publicResult': copy.deepcopy(public)}
        observed, reference = self.observe(selection, step='gp-'+action_id[:32]+'-accepted')
        selection['initial'] = {'resource': observed, 'reference': reference}
        self.retain('job-selected-'+action_id, selection)
        return selection

    def observe(self, selection, *, step):
        answer = self.http.json(step, 'GET', '/api/v1/admin/jobs/'+identifier(selection['jobId']))
        observed = detail(successful(answer), selection=selection, binding=self.binding,
                          observed_at=datetime.now(timezone.utc))
        return observed, answer['reference']

    def results(self, selection, *, step):
        final, final_ref = self.observe(selection, step=step+'-completed')
        require(final['status'] == 'Succeeded', 'native managed job has not succeeded')
        job = selection['jobId']
        answer = self.http.json(step+'-results', 'GET', '/ogc/processes/jobs/'+job+'/results')
        outputs = successful(answer).get('outputs')
        require(isinstance(outputs, dict) and len(outputs) == 1, 'native managed job lacks its single result artifact')
        artifact = next(iter(outputs.values()))
        require(isinstance(artifact, dict) and artifact.get('kind') == 'FeatureLayer' and
                artifact.get('type') == 'application/geo+json' and isinstance(artifact.get('id'), str) and artifact['id'],
                'native managed job artifact kind/media/identity differs')
        href = artifact.get('href')
        require(isinstance(href, str), 'native artifact content is unavailable')
        references = {'completed': final_ref, 'results': answer['reference']}
        if href.startswith(DATA_PREFIX):
            require(len(href) <= 6*1024*1024, 'native inline artifact exceeds observer byte bound')
            try:
                document = json.loads(base64.b64decode(href[len(DATA_PREFIX):], validate=True))
            except (ValueError, UnicodeError) as error:
                raise PackError('invalid original native inline GeoJSON') from error
        else:
            target = urlsplit(href)
            require(not target.query and not target.fragment and
                    re.fullmatch('/api/geoprocessing/jobs/'+re.escape(job)+r'/artifacts/[0-9]+/content', target.path) and
                    (not target.scheme and not target.netloc or href == self.http.client.endpoint+target.path),
                    'native artifact link is outside exact same-origin job content route')
            answer = self.http.json(step+'-content', 'GET', target.path)
            document, references['content'] = successful(answer), answer['reference']
        return {'resource': final, 'artifact': copy.deepcopy(artifact), 'document': document,
                'buffer': metric_buffer(document), 'nativeReferences': references}
