"""Controller adapters for current import and typed service.publish contracts.

Contract source: Honua Server fcae944b5375a10026982053ef4acc280c6ea1c3,
ImportEndpoints/ImportProgress/TableDiscoveryModels and OperationsEndpoints.
All reads and writes use the caller's guarded, durable ScenarioHttp transport.
This module never approves a proposal, resends a mutation, infers provider table
names, creates infrastructure, or manufactures cleanup evidence.
"""
from __future__ import annotations

from datetime import datetime
import re
import time
from urllib.parse import quote
from uuid import UUID

from agent_e2e_pack import PackError


def require(value, reason):
    if not value:
        raise PackError(reason)


def token(value, name):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value),
            'missing or invalid observed '+name)
    return value


def identifier(value, name):
    require(isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value),
            'missing or invalid observed '+name)
    return value


def connection_id(value):
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise PackError('secure connection requires an observed GUID') from None
    require(str(parsed) == value.lower(), 'secure connection requires canonical GUID')
    return str(parsed)


def body(answer, statuses=(200,)):
    observation = answer['observation']
    require(observation['httpStatus'] in statuses, 'candidate product operation did not return expected HTTP status')
    value = observation['body']
    require(isinstance(value, dict) and value.get('success', True) is not False and 'error' not in value,
            'candidate product response reports failure')
    return value


def data(answer, statuses=(200,)):
    value = body(answer, statuses)
    value = value.get('data', value)
    require(isinstance(value, dict), 'candidate response lacks resource object')
    return value


def tables(answer):
    value = body(answer)
    rows = value.get('tables')
    require(isinstance(rows, list), 'table discovery lacks observed table list')
    result = {}
    for row in rows:
        require(isinstance(row, dict), 'table discovery contains malformed table')
        key = (identifier(row.get('schema'), 'table schema'), identifier(row.get('table'), 'physical table'))
        require(key not in result, 'table discovery contains duplicate identity')
        result[key] = row
    return result


def discover_tables(http, connection, step):
    return http.json(step, 'GET', '/api/v1/admin/connections/'+connection_id(connection)+'/tables')


def completed_import(http, accepted, *, table, feature_count, step, before_tables=None,
                     connection=None, timeout_seconds=240, max_polls=120, poll_seconds=2,
                     clock=time.monotonic, sleep=time.sleep):
    """Observe a 200 result or 202 job, without replaying the original upload.

    ImportProgress serializes ImportStatus numerically (Queued=0, Processing=1,
    Completed=2, Failed=3, Cancelled=4) and does not expose physical table/schema.
    For a background import, bind exactly one new table from retained discovery
    before upload and an independent discovery after observed job completion.
    The target environment must have one active case; concurrent additions fail.
    """
    identifier(table, 'logical import table')
    token(step, 'import observation step')
    require(type(feature_count) is int and feature_count > 0, 'invalid expected import feature count')
    require(type(max_polls) is int and 1 <= max_polls <= 150 and
            type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 300 and
            type(poll_seconds) in (int, float) and 0 <= poll_seconds <= 5,
            'invalid bounded import observation policy')
    raw = body(accepted, (200, 202))
    references = [accepted['reference']]
    if accepted['observation']['httpStatus'] == 200:
        require(raw.get('success') is True and raw.get('tableName') == table and
                type(raw.get('featureCount')) is int and raw['featureCount'] == feature_count,
                'synchronous import does not match completed source count/table')
        physical = identifier(raw.get('physicalTableName'), 'physical import table')
        schema = identifier(raw.get('schema'), 'physical import schema')
        return {'table': physical, 'schema': schema, 'featureCount': raw['featureCount'],
                'sourceTable': table, 'references': references, 'jobId': None}
    require(before_tables is not None and connection is not None,
            'background import needs independently retained table discovery before upload')
    before = tables(before_tables)
    job = token(raw.get('jobId'), 'import job ID')
    path = '/api/v1/admin/import/jobs/'+quote(job, safe='')
    require(raw.get('statusUrl') == path, 'background status URL differs from observed job identity')
    deadline = clock()+timeout_seconds
    terminal = None
    for ordinal in range(max_polls):
        require(clock() < deadline, 'background import observation deadline expired; retain job and liability')
        answer = http.json(f'{step}-poll-{ordinal:03}', 'GET', path)
        progress = body(answer)
        references.append(answer['reference'])
        require(progress.get('jobId') == job and progress.get('tableName') == table,
                'background import progress belongs to another job/table')
        status = progress.get('status')
        require(type(status) is int and status in (0, 1, 2, 3, 4), 'unknown background import status contract')
        if status in (3, 4):
            raise PackError('background import failed or cancelled; original job retained')
        if status == 2:
            require(type(progress.get('featuresProcessed')) is int and progress['featuresProcessed'] == feature_count
                    and type(progress.get('failedFeatures')) is int and progress['failedFeatures'] == 0,
                    'completed background import has missing, failed or unexpected features')
            completed = progress.get('completedAt')
            require(isinstance(completed, str), 'completed background import lacks completion time')
            try:
                instant = datetime.fromisoformat(completed.replace('Z', '+00:00'))
            except ValueError:
                raise PackError('invalid background import completion time') from None
            require(instant.tzinfo is not None, 'background import completion time lacks timezone')
            terminal = progress
            break
        remaining = deadline-clock()
        require(remaining > 0, 'background import observation deadline expired; retain job and liability')
        if ordinal+1 < max_polls:
            sleep(min(poll_seconds, remaining))
    require(terminal is not None, 'background import remains incomplete after bounded polling; retain job and liability')
    after_answer = discover_tables(http, connection, step+'-tables-after')
    after = tables(after_answer)
    require(set(before) <= set(after), 'source table disappeared during isolated import')
    added = set(after)-set(before)
    require(len(added) == 1, 'cannot independently bind one new physical import table; reconcile observed discovery')
    schema, physical = added.pop()
    references.extend((before_tables['reference'], after_answer['reference']))
    return {'table': physical, 'schema': schema, 'featureCount': terminal['featuresProcessed'],
            'sourceTable': table, 'references': references, 'jobId': job}


def publication_request(*, connection, schema, table, service, geometry_type, srid=3857):
    connection = connection_id(connection)
    identifier(schema, 'publication schema')
    identifier(table, 'publication table')
    require(isinstance(service, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,62}', service),
            'invalid case-owned publication service')
    require(geometry_type in ('Point', 'Polygon', 'LineString', 'MultiPoint', 'MultiPolygon', 'MultiLineString')
            and type(srid) is int and srid > 0, 'invalid publication geometry/CRS')
    return {'connectionId': connection, 'serviceName': service, 'parameters': {
        'schema': schema, 'table': table, 'layerName': service,
        'geometryType': geometry_type, 'srid': str(srid)}, 'fields': [], 'dryRun': False}


def publication_binding(handle, *, service, reference):
    require(handle.get('operationId') == 'service.publish', 'publication handle belongs to another operation')
    operation = token(handle.get('operationInstanceId'), 'publication operation identity')
    token(handle.get('correlationId'), 'publication correlation identity')
    require(handle.get('status') == 'Completed', 'publication is not independently observed completed')
    ids = handle.get('resourceIds')
    require(isinstance(ids, dict) and ids.get('serviceName') == service and
            isinstance(ids.get('layerId'), str) and re.fullmatch(r'0|[1-9][0-9]*', ids['layerId']) and
            int(ids['layerId']) <= 2147483647, 'completed publication lacks actual service/layer binding')
    return {'status': 'completed', 'service': service, 'layerId': int(ids['layerId']),
            'operationInstanceId': operation, 'reference': reference}


def submit_publication(http, request, *, step):
    """Use the typed policy/approval dispatcher and preserve a pending proposal.

    An HTTP403 operator approval refusal is retained and fails closed. A typed
    RequiresApproval handle names a durable proposal, but is never a successful
    publication. An independent actor must genuinely approve it before callers
    can use observe_approved_publication; this adapter never clicks approval.
    """
    token(step, 'publication step')
    validate_answer = http.json(step+'-validate', 'POST', '/api/v1/operations/service.publish/validate', request)
    validation = data(validate_answer)
    require(validation.get('isValid') is True, 'candidate refused publication validation')
    answer = http.json(step+'-submit', 'POST', '/api/v1/operations/service.publish/submit', request)
    handle = data(answer)
    require(handle.get('operationId') == 'service.publish', 'submit handle belongs to another operation')
    if handle.get('status') == 'RequiresApproval':
        pending = {'status': 'awaiting-approval', 'proposalId': token(handle.get('proposalId'), 'publication proposal'),
            'auditId': token(handle.get('auditId'), 'publication acceptance audit'),
            'operationInstanceId': token(handle.get('operationInstanceId'), 'publication operation identity'),
            'correlationId': token(handle.get('correlationId'), 'publication correlation identity'),
            'service': request['serviceName'], 'reference': answer['reference']}
        return pending
    result = publication_binding(handle, service=request['serviceName'], reference=answer['reference'])
    result['validationReference'] = validate_answer['reference']
    return result


def observe_approved_publication(http, pending, *, step, requested_by):
    """Read actual proposal resolution and canonical replay handle; never approve."""
    require(pending.get('status') == 'awaiting-approval', 'publication does not carry a retained pending proposal')
    proposal_id = token(pending.get('proposalId'), 'publication proposal')
    proposal_answer = http.json(step+'-proposal', 'GET', '/api/v1/admin/proposals/'+proposal_id)
    proposal = body(proposal_answer)
    require(proposal.get('proposalId') == proposal_id and proposal.get('kind') == 'ServicePublish',
            'resolved proposal differs from publication request')
    require(isinstance(requested_by, str) and requested_by and proposal.get('requestedBy') == requested_by,
            'proposal requester differs from independently observed task principal')
    if proposal.get('status') in ('AwaitingApproval', 'Planned', 'Executing', 'Submitted', 'Reconciling'):
        return {**pending, 'proposalReference': proposal_answer['reference']}
    require(proposal.get('status') == 'Succeeded' and isinstance(proposal.get('resolvedBy'), str) and
            proposal['resolvedBy'] and proposal['resolvedBy'] != requested_by,
            'publication lacks genuine successful distinct-actor approval')
    replay = token(proposal.get('executionOperationId'), 'approved execution operation')
    answer = http.json(step+'-replay', 'GET', '/api/v1/operations/handles/'+replay)
    handle = data(answer)
    require(handle.get('operationInstanceId') == replay and handle.get('proposalId') == proposal_id,
            'canonical execution handle is not bound to approved proposal')
    result = publication_binding(handle, service=pending['service'], reference=answer['reference'])
    result.update(proposalId=proposal_id, requestedBy=requested_by, approvedBy=proposal['resolvedBy'],
                  proposalReference=proposal_answer['reference'])
    return result
