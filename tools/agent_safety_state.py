"""What changed in an administrator state snapshot, so a failed state check names the items."""
from __future__ import annotations

ITEM_KEYS = ('id', 'name', 'key', 'serviceName', 'layerId')
SHOWN = 5


def item_key(item, index: int) -> str:
    if isinstance(item, dict):
        for key in ITEM_KEYS:
            if item.get(key) not in (None, ''):
                return str(item[key])
    return f'#{index}'


def describe(before, after) -> dict:
    """Items added, removed and modified (with the fields that differ) between two reads of one route."""
    if isinstance(before, list) and isinstance(after, list):
        old = {item_key(item, i): item for i, item in enumerate(before)}
        new = {item_key(item, i): item for i, item in enumerate(after)}
        modified = {key: fields(old[key], new[key]) for key in sorted(old.keys() & new.keys()) if old[key] != new[key]}
        return {'added': sorted(new.keys() - old.keys()), 'removed': sorted(old.keys() - new.keys()),
                'modified': modified}
    if isinstance(before, dict) and isinstance(after, dict):
        changed = fields(before, after)
        if len(changed) == 1 and all(isinstance(body.get(changed[0]), list) for body in (before, after)):
            return describe(before[changed[0]], after[changed[0]])
        return {'added': [], 'removed': [], 'modified': {'': changed}}
    return {'added': [], 'removed': [], 'modified': {'': ['value']}}


def fields(before, after) -> list[str]:
    if not (isinstance(before, dict) and isinstance(after, dict)):
        return ['value']
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def state_diff(before: dict, after: dict) -> dict:
    """Per changed snapshot route: the item diff, and whether only the ETag moved."""
    changes = {}
    for route, read in before.items():
        if after[route] == read:
            continue
        if read['body'] == after[route]['body']:
            changes[route] = {'added': [], 'removed': [], 'modified': {}, 'etagOnly': True}
        else:
            change = describe(read['body'], after[route]['body'])
            if not (change['added'] or change['removed'] or change['modified']):
                change['modified'] = {'': ['order or duplicate items']}
            changes[route] = {**change, 'etagOnly': False}
    return changes


def summary(route: str, change: dict) -> str:
    if change['etagOnly']:
        return f'{route}: ETag changed, body unchanged'
    parts = [f"{label} {', '.join(items[:SHOWN])}" for label, items in
             (('added', change['added']), ('removed', change['removed'])) if items]
    modified = [f"{key or 'body'}[{','.join(names)}]" for key, names in list(change['modified'].items())[:SHOWN]]
    if modified:
        parts.append('modified ' + ', '.join(modified))
    return f"{route}: {'; '.join(parts)}"
