"""Candidate P13 projection; registration requires the core assertion hook.

This is not yet wired into the runner. It consumes only retained controller
responses, and requires immutable version history showing a later agent-authored
shelter style change before accepting the original saved version as current.
"""
import hashlib
import json

from agent_e2e_local_recorder import ProjectionError


def _require(value, message):
    if not value:
        raise ProjectionError(message)


def _raw_matches(record):
    try:
        return (record.sha256 == 'sha256:' + hashlib.sha256(record.raw).hexdigest()
                and json.loads(record.raw) == record.json)
    except (ValueError, TypeError, UnicodeDecodeError):
        return False


def _data(records, step, phase):
    record = records[step]
    _require(record.phase == phase and record.kind == 'http' and record.principal == 'controller'
             and record.method == 'GET' and record.status == 200 and _raw_matches(record)
             and isinstance(record.json, dict) and record.json.get('success') is True
             and isinstance(record.json.get('data'), dict), 'invalid controller read: ' + step)
    return record, record.json['data']


def _shelter_style(version):
    body = version['envelope']['body']
    layers = body['layers']
    _require(isinstance(layers, list), 'map layers missing')
    matches = [layer for layer in layers if isinstance(layer, dict) and layer.get('id') == 'shelters']
    _require(len(matches) == 1, 'shelter layer missing or ambiguous')
    return matches[0].get('style')


def project_restore(records, *, item_id, version_id, agent_principal):
    """Return the restored-to v1 binding only with a later changed saved version.

    The caller must bind ``item_id`` and ``version_id`` to seed-phase records.
    A current pointer alone must never satisfy this predicate.
    """
    try:
        seed = records['p13-target-save']
        _require(seed.phase == 'seed' and seed.kind == 'http' and seed.principal == 'controller'
                 and seed.method == 'POST' and seed.status == 201 and _raw_matches(seed) and isinstance(seed.json, dict)
                 and seed.json.get('success') is True and isinstance(seed.json.get('data'), dict),
                 'saved seed version missing')
        initial = seed.json['data']
        _require(initial.get('itemId') == item_id and initial.get('versionId') == version_id
                 and initial.get('ownerId') == agent_principal, 'seed version identity differs')
        current_record, listing = _data(records, 'p13-target-after', 'observe')
        history_record, history = _data(records, 'p13-target-versions', 'observe')
        version_record, original = _data(records, 'p13-target-v1', 'observe')
        root = '/api/v1/studio/content-items/' + item_id + '/versions'
        _require(history_record.path == root and version_record.path == root + '/' + version_id,
                 'version reads address a different item')
        _require(current_record.path.startswith('/api/v1/studio/content-items?'), 'invalid item inventory route')
        rows = listing['items']
        _require(listing['total'] == 1 and listing['nextCursor'] is None and len(rows) == 1,
                 'target item inventory incomplete')
        current = rows[0]
        _require(current['itemId'] == item_id and current['ownerId'] == agent_principal
                 and current['currentVersionId'] == version_id, 'original is not current or target is foreign')
        _require(original['itemId'] == item_id and original['versionId'] == version_id
                 and original['ownerId'] == agent_principal and original['versionNumber'] == 1,
                 'original version differs from seed')
        _require(history['itemId'] == item_id and isinstance(history['versions'], list), 'version history missing')
        _require(original.get('contentHash') == initial.get('contentHash')
                 and original.get('envelope') == initial.get('envelope'), 'original version content changed')
        original_style = _shelter_style(original)
        changed = [v for v in history['versions'] if v.get('itemId') == item_id
                   and v.get('ownerId') == agent_principal and v.get('createdBy') == agent_principal
                   and type(v.get('versionNumber')) is int and v['versionNumber'] > 1
                   and v.get('versionId') != version_id and _shelter_style(v) != original_style]
        _require(bool(changed), 'no later agent-authored shelter style version')
        digest = original['contentHash']
        _require(isinstance(digest, str) and len(digest) == 64
                 and all(c in '0123456789abcdef' for c in digest), 'original content hash missing')
        return {'resourceId': item_id, 'restoredVersionId': version_id, 'documentSha256': digest}
    except (KeyError, TypeError, IndexError) as exc:
        raise ProjectionError('incomplete restore evidence') from exc
