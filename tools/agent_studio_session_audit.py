"""Controller-owned audit snapshots around a Studio customer session."""
from urllib.parse import quote
from uuid import UUID

from agent_e2e_local_recorder import ProjectionError, require


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
                and isinstance(body.get('items'), list) and 'nextCursor' in body,
                'Studio session audit is unavailable')
        for event in body['items']:
            require(isinstance(event, dict) and type(event.get('auditId')) is int
                    and event['auditId'] > 0 and isinstance(event.get('actor'), str),
                    'Studio session audit event is malformed')
            event_id = event['auditId']
            require(event_id not in rows, 'Studio session audit contains duplicate events')
            rows[event_id] = event['actor']
        cursor = body['nextCursor']
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


def verify(before, after, controller_principals):
    """Reject controller-attributed actions appearing during the customer session."""
    require(before.keys() <= after.keys(), 'Studio session audit lost baseline events')
    require(all(after[key] == actor for key, actor in before.items()),
            'Studio session audit baseline changed')
    require(isinstance(controller_principals, frozenset) and controller_principals,
            'controller principal evidence is unavailable')
    require(not any(actor.casefold() in controller_principals for key, actor in after.items() if key not in before),
            'controller principal appeared in Studio session audit')
