"""Controller-owned audit snapshots around a Studio customer session."""
import json
from urllib.parse import quote
from uuid import UUID

from agent_e2e_local_recorder import ProjectionError, require


def verify_capabilities(recorder, model):
    """Read the server's effective default Studio AI provider through the controller."""
    response = recorder.json('studio-ai-capabilities', 'GET', '/api/v1/studio/ai/capabilities')['observation']
    body = response.get('body')
    data = body.get('data') if isinstance(body, dict) else None
    require(response.get('httpStatus') == 200 and isinstance(data, dict) and data.get('enabled') is True
            and isinstance(data.get('defaultProvider'), str) and isinstance(data.get('providers'), list),
            'Studio AI capabilities are unavailable')
    providers = [row for row in data['providers'] if isinstance(row, dict)
                 and row.get('provider') == data['defaultProvider']]
    require(len(providers) == 1 and providers[0].get('kind') == 'bedrock'
            and providers[0].get('model') == model and providers[0].get('isDefault') is True
            and providers[0].get('configured') is True and providers[0].get('toolSupport') is True,
            'Studio default AI provider is not the required configured Bedrock model')
    return providers[0]['provider']


def snapshot(recorder, label):
    """Read a bounded, complete audit inventory; an incomplete inventory fails closed."""
    require(label in ('before', 'after'), 'invalid Studio audit snapshot')
    rows, seen_cursors = {}, set()
    cursor = None
    for page in range(20):
        path = '/api/v1/admin/observability/audit?pageSize=200'
        if cursor is not None:
            path += '&cursor=' + quote(cursor, safe='')
        response = recorder.json(f'studio-audit-{label}-{page}', 'GET', path)['observation']
        body = response.get('body')
        require(response.get('httpStatus') == 200 and isinstance(body, dict)
                and isinstance(body.get('items'), list),
                'Studio session audit is unavailable')
        for event in body['items']:
            require(isinstance(event, dict) and type(event.get('auditId')) is int
                    and event['auditId'] > 0 and isinstance(event.get('actor'), str),
                    'Studio session audit event is malformed')
            event_id = event['auditId']
            require(event_id not in rows, 'Studio session audit contains duplicate events')
            rows[event_id] = event
        # The server's JSON settings omit null properties on the final page.
        cursor = body.get('nextCursor')
        if cursor is None:
            return rows
        require(isinstance(cursor, str) and 0 < len(cursor) <= 2048 and cursor not in seen_cursors,
                'Studio session audit cursor is invalid')
        seen_cursors.add(cursor)
    raise ProjectionError('Studio session audit exceeds the page limit')


def controller_actors(recorder, key):
    """Bind managed controller keys to server key IDs without retaining the key."""
    require(isinstance(key, str) and key, 'controller credential is unavailable')
    response = recorder.json('studio-audit-controller-keys', 'GET', '/api/v1/admin/api-keys/')['observation']
    body = response.get('body')
    require(response.get('httpStatus') == 200 and isinstance(body, dict)
            and isinstance(body.get('data'), list), 'controller key inventory is unavailable')
    actors = {'admin', 'installer-bootstrap-admin'}
    for row in body['data']:
        require(isinstance(row, dict) and isinstance(row.get('keyPrefix'), str)
                and row['keyPrefix'] and isinstance(row.get('id'), str), 'controller key inventory is malformed')
        if key.startswith(row['keyPrefix']):
            try:
                key_id = str(UUID(row['id']))
            except ValueError as exc:
                raise ProjectionError('controller key identity is malformed') from exc
            actors.update((key_id, 'api-key:' + key_id, 'apikey:api-key:' + key_id))
    return frozenset(actor.casefold() for actor in actors)


def verify(before, after, controller_principals, *, forbid_assistant=False):
    """Reject controller-attributed actions appearing during the customer session."""
    require(before.keys() <= after.keys(), 'Studio session audit lost baseline events')
    require(all(after[key] == event for key, event in before.items()),
            'Studio session audit baseline changed')
    require(isinstance(controller_principals, frozenset) and controller_principals,
            'controller principal evidence is unavailable')
    require(not any(event['actor'].casefold() in controller_principals
                    for key, event in after.items() if key not in before),
            'controller principal appeared in Studio session audit')
    if forbid_assistant:
        require(not any(event.get('action') == 'studio_ai.chat'
                        for key, event in after.items() if key not in before),
                'Studio assistant was invoked during CLI session')


def assistant_usage(before, after, *, user_id, subject, model, observed_calls, agent_key_id=None, allow_failed=False):
    """Bind model and usage to customer-attributed server AI audit records."""
    require(isinstance(user_id, str) and user_id.startswith('subject:')
            and isinstance(subject, str) and subject, 'customer identity is unavailable')
    canonical_subject = user_id.split('@tenant:', 1)[0]
    key_actors = set()
    if agent_key_id is not None:
        try:
            key_id = str(UUID(agent_key_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProjectionError('customer agent key identity is malformed') from exc
        key_actors = {key_id, 'api-key:' + key_id, 'apikey:api-key:' + key_id}
    events = [event for key, event in after.items() if key not in before
              and event.get('resourceType') == 'studio_ai_provider' and event.get('action') == 'studio_ai.chat']
    require(type(observed_calls) is int and 0 < observed_calls <= 60 and len(events) == observed_calls,
            'Studio AI audit does not account for every model call')
    usage = []
    for event in events:
        actor = event.get('actor', '')
        require(actor in (subject, user_id, canonical_subject) or actor.endswith(':' + canonical_subject)
                or actor.casefold() in key_actors,
                'Studio AI audit is not attributed to the customer')
        require((event.get('outcome') == 'Success' or allow_failed and isinstance(event.get('outcome'), str)
                 and bool(event['outcome'])) and isinstance(event.get('details'), str),
                'Studio AI audit reports a failed or incomplete call')
        try:
            detail = json.loads(event['details'])
        except ValueError as exc:
            raise ProjectionError('Studio AI audit details are malformed') from exc
        require(isinstance(detail, dict) and detail.get('kind') == 'bedrock' and detail.get('model') == model
                and type(detail.get('promptTokens')) is int and detail['promptTokens'] > 0
                and type(detail.get('completionTokens')) is int and detail['completionTokens'] >= 0,
                'Studio AI audit lacks the required model or usage')
        usage.append({'prompt_tokens': detail['promptTokens'], 'completion_tokens': detail['completionTokens']})
    return usage
