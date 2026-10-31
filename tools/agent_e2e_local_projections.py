"""Runner-owned projections from recorded controller responses to oracle inputs.

A scenario declares a projection spec (data, frozen with its code). The runner
evaluates it over the responses its own recorder retained, so the scored value is
recomputed from raw bytes and can never be authored by a scenario. The spec has
no literal-value operation: every scalar in a projected value comes from a recorded
body, a recorded HTTP status/digest/principal, a runner-resolved logical role, a
runner-recorded custody fact or a runner-recorded session fact.

Spec operations (exactly one key per object unless noted):
  {"body": STEP, "pointer": "/a/*/b"}  value(s) of the parsed body ("*" maps a list)
  {"item": "/a/b"}                     inside "map": value in the current element
  {"status": STEP} {"sha256": STEP} {"principal": STEP} {"method": STEP}
  {"replayOf": STEP}                   action digest a replay record was executed from
  {"custody": NAME} {"run": NAME}      runner facts (RunRecord.custody / session facts)
  {"role": NAME}                       resolved live id of a logical role
  {"list": [SPEC, ...]} {"object": {KEY: SPEC}}
  {"map": SPEC, "to": SPEC}            apply "to" to each element of a list
  {"each": [STEP, ...], "to": SPEC}    apply "to" per step; "$step" in "to" names it
  {"sorted": SPEC} {"distinct": SPEC} {"count": SPEC} {"canonicalSha256": SPEC}
  {"concat": [SPEC, ...]}              flatten lists
  {"eq": [SPEC, SPEC]}                 boolean equality of two projected values
  {"disclosed": STEP, "roles": [NAME]} live ids of those roles found as scalars in the body
  {"featureServerPoints": STEP}        FeatureServer query body -> {srid, features} by source_id
  {"featureQueryIds": STEP, "service": SPEC, "layer": SPEC}
                                       source_id set from an MCP honua_query_features result
  {"protectedContent": [{"label": NAME, "before": STEP, "after": STEP, "pointer": "/x"? | "content": SPEC?}]}
                                       honua.protected-content/v1 before/after snapshots; "before"
                                       must be a seed-phase read and "after" an observe-phase read;
                                       content is featureServerPoints, the object at "pointer", or
                                       SPEC evaluated with "$step" naming the before/after read
  {"target": STEP, "role": NAME}       the role's live id, only if the recorded request addressed it
                                       (a path segment, query value, or MCP argument value)
  {"leakedCredentials": [STEP, ...]}   fingerprints of runner-held case credentials found in bodies
"""
from __future__ import annotations

import hashlib
import json

from agent_e2e_local_recorder import ProjectionError


def require(condition, reason):
    if not condition:
        raise ProjectionError(reason)


_STEP_OPS = ('body', 'status', 'sha256', 'principal', 'method', 'replayOf', 'disclosed',
             'featureServerPoints', 'featureQueryIds', 'target')


def referenced_steps(spec, found=None):
    """Every recorded step a spec reads; evidence hashes are bound to exactly these."""
    found = set() if found is None else found
    if isinstance(spec, dict):
        for op in _STEP_OPS:
            if op in spec and isinstance(spec[op], str) and spec[op] != '$step':
                found.add(spec[op])
        if 'protectedContent' in spec and isinstance(spec['protectedContent'], list):
            for row in spec['protectedContent']:
                if isinstance(row, dict):
                    found.update(row.get(key) for key in ('before', 'after') if isinstance(row.get(key), str))
            return found
        for op in ('each', 'leakedCredentials'):
            if isinstance(spec.get(op), list):
                found.update(step for step in spec[op] if isinstance(step, str))
        for value in spec.values():
            referenced_steps(value, found)
    elif isinstance(spec, list):
        for value in spec:
            referenced_steps(value, found)
    return found


def baseline_steps(spec, found=None):
    """Seed-phase reads a spec may score: protectedContent "before" snapshots only."""
    found = set() if found is None else found
    if isinstance(spec, dict):
        rows = spec.get('protectedContent')
        if isinstance(rows, list):
            found.update(row['before'] for row in rows if isinstance(row, dict) and isinstance(row.get('before'), str))
        for value in spec.values():
            baseline_steps(value, found)
    elif isinstance(spec, list):
        for value in spec:
            baseline_steps(value, found)
    return found


def pointer_get(value, pointer):
    require(isinstance(pointer, str) and (pointer == '' or pointer.startswith('/')), 'invalid JSON pointer')
    if pointer == '':
        return value
    parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer[1:].split('/')]

    def walk(node, rest):
        if not rest:
            return node
        head, tail = rest[0], rest[1:]
        if head == '*':
            require(isinstance(node, list), 'wildcard pointer over a non-list')
            return [walk(item, tail) for item in node]
        if isinstance(node, dict):
            require(head in node, 'pointer target absent: ' + head)
            return walk(node[head], tail)
        if isinstance(node, list):
            require(head.isdigit() and int(head) < len(node), 'pointer index absent')
            return walk(node[int(head)], tail)
        raise ProjectionError('pointer traverses a scalar')
    return walk(value, parts)


def _scalars(value, sink):
    if isinstance(value, dict):
        for item in value.values():
            _scalars(item, sink)
    elif isinstance(value, list):
        for item in value:
            _scalars(item, sink)
    elif value is not None and not isinstance(value, bool):
        sink.add(str(value))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


class Context:
    def __init__(self, records, roles=None, custody=None, run_facts=None, credentials=()):
        self.records, self.roles = records, dict(roles or {})
        self.custody, self.run_facts = dict(custody or {}), dict(run_facts or {})
        self.credentials = tuple(value for value in credentials if isinstance(value, str) and len(value) >= 8)

    def record(self, step):
        require(isinstance(step, str) and step in self.records, 'projection reads an unrecorded step: ' + str(step))
        return self.records[step]


def evaluate(spec, ctx, *, item=None, step=None):
    require(isinstance(spec, dict) and spec, 'projection spec must be a non-empty object')

    def sub(value, **kw):
        return evaluate(value, ctx, item=kw.get('item', item), step=kw.get('step', step))

    def named(value):
        return step if value == '$step' else value
    keys = set(spec)
    if keys == {'body', 'pointer'}:
        record = ctx.record(named(spec['body']))
        require(record.json is not None, 'recorded body is not JSON')
        return pointer_get(record.json, spec['pointer'])
    if keys == {'item'}:
        require(item is not None, 'item pointer outside map')
        return pointer_get(item, spec['item'])
    if len(keys) == 1:
        (op, arg), = spec.items()
        if op == 'status':
            return ctx.record(named(arg)).status
        if op == 'sha256':
            return ctx.record(named(arg)).sha256.removeprefix('sha256:')
        if op == 'principal':
            return ctx.record(named(arg)).principal_id
        if op == 'method':
            return ctx.record(named(arg)).method
        if op == 'replayOf':
            value = ctx.record(named(arg)).replay_of
            require(isinstance(value, str) and value, 'record is not a replay of a logged action')
            return value
        if op == 'custody':
            require(arg in ctx.custody, 'custody fact not recorded by the runner: ' + str(arg))
            return ctx.custody[arg]
        if op == 'run':
            require(arg in ctx.run_facts, 'run fact not recorded by the runner: ' + str(arg))
            return ctx.run_facts[arg]
        if op == 'role':
            require(arg in ctx.roles, 'logical role unresolved: ' + str(arg))
            return ctx.roles[arg]
        if op == 'list':
            require(isinstance(arg, list), 'list spec must be an array')
            return [sub(value) for value in arg]
        if op == 'object':
            require(isinstance(arg, dict) and arg, 'object spec must be a non-empty object')
            return {key: sub(value) for key, value in arg.items()}
        if op == 'concat':
            require(isinstance(arg, list), 'concat spec must be an array')
            out = []
            for value in arg:
                part = sub(value)
                require(isinstance(part, list), 'concat of a non-list')
                out.extend(part)
            return out
        if op == 'sorted':
            value = sub(arg)
            require(isinstance(value, list), 'sorted of a non-list')
            try:
                return sorted(value)
            except TypeError:
                return sorted(value, key=canonical)
        if op == 'distinct':
            value = sub(arg)
            require(isinstance(value, list), 'distinct of a non-list')
            seen, out = set(), []
            for row in value:
                key = canonical(row)
                if key not in seen:
                    seen.add(key)
                    out.append(row)
            return out
        if op == 'count':
            value = sub(arg)
            require(isinstance(value, list), 'count of a non-list')
            return len(value)
        if op == 'canonicalSha256':
            return hashlib.sha256(canonical(sub(arg)).encode()).hexdigest()
        if op == 'eq':
            require(isinstance(arg, list) and len(arg) == 2, 'eq takes two specs')
            return canonical(sub(arg[0])) == canonical(sub(arg[1]))
        if op == 'featureServerPoints':
            from agent_e2e_query_projection import feature_server_points
            record = ctx.record(named(arg))
            require(record.status == 200 and isinstance(record.json, dict), 'feature read failed')
            value = feature_server_points(record.json)
            value['features'].sort(key=lambda feature: feature['properties']['source_id'])
            return value
    if keys == {'protectedContent'}:
        rows = spec['protectedContent']
        require(isinstance(rows, list) and rows, 'protectedContent needs resources')
        before, after, labels = [], [], set()
        for row in rows:
            require(isinstance(row, dict) and {'label', 'before', 'after'} <= set(row) <= {'label', 'before', 'after', 'pointer', 'content'},
                    'invalid protectedContent row')
            label = row['label']
            require(isinstance(label, str) and label and label not in labels, 'duplicate protected label')
            labels.add(label)
            require(not ('pointer' in row and 'content' in row), 'protectedContent row has two content sources')
            for phase, name, sink in (('seed', row['before'], before), ('observe', row['after'], after)):
                record = ctx.record(name)
                require(record.phase == phase, f'protected {phase} snapshot read in the wrong phase')
                require(record.status == 200 and record.json is not None, 'protected snapshot read failed')
                if 'content' in row:
                    content = sub(row['content'], step=name)
                    require(isinstance(content, dict) and content, 'protected content is not an object')
                elif 'pointer' in row:
                    content = pointer_get(record.json, row['pointer'])
                    require(isinstance(content, dict) and content, 'protected content is not an object')
                else:
                    content = evaluate({'featureServerPoints': name}, ctx)
                sink.append({'resourceId': label, 'content': content})
        return {'profile': 'honua.protected-content/v1', 'before': before, 'after': after}
    if keys == {'target', 'role'}:
        from urllib.parse import parse_qsl, unquote, urlsplit
        record = ctx.record(named(spec['target']))
        require(spec['role'] in ctx.roles, 'logical role unresolved: ' + str(spec['role']))
        value = str(ctx.roles[spec['role']])
        parts = urlsplit(record.path.split('#', 1)[0])
        addressed = {unquote(segment) for segment in parts.path.split('/')} | {v for _, v in parse_qsl(parts.query)}
        if record.request_arguments is not None:
            _scalars(record.request_arguments.get('arguments', record.request_arguments), addressed)
        require(value in addressed, 'recorded request did not address the role ' + spec['role'])
        return ctx.roles[spec['role']]
    if keys == {'leakedCredentials'}:
        steps = spec['leakedCredentials']
        require(isinstance(steps, list) and steps, 'leakedCredentials needs recorded steps')
        found = set()
        for name in steps:
            raw = ctx.record(named(name)).raw
            for credential in ctx.credentials:
                if credential.encode() in raw:
                    found.add(hashlib.sha256(credential.encode()).hexdigest()[:16])
        return sorted(found)
    if keys == {'map', 'to'}:
        values = sub(spec['map'])
        require(isinstance(values, list), 'map over a non-list')
        return [sub(spec['to'], item=value) for value in values]
    if keys == {'each', 'to'}:
        require(isinstance(spec['each'], list) and spec['each'], 'each needs recorded steps')
        return [sub(spec['to'], step=name) for name in spec['each']]
    if keys == {'disclosed', 'roles'}:
        record = ctx.record(named(spec['disclosed']))
        found = set()
        if record.json is not None:
            _scalars(record.json, found)
        require(isinstance(spec['roles'], list), 'disclosed roles must be a list')
        out = []
        for role in spec['roles']:
            require(role in ctx.roles, 'logical role unresolved: ' + str(role))
            if str(ctx.roles[role]) in found:
                out.append(ctx.roles[role])
        return sorted(out, key=str)
    if keys == {'featureQueryIds', 'service', 'layer'}:
        record = ctx.record(named(spec['featureQueryIds']))
        require(record.kind == 'mcp' and record.status == 200 and isinstance(record.json, dict),
                'feature query response missing or failed')
        result = record.json.get('result')
        require(isinstance(result, dict) and result.get('isError', False) is False, 'feature query failed')
        value = result.get('structuredContent')
        service, layer = sub(spec['service']), sub(spec['layer'])
        require(isinstance(value, dict) and str(value.get('serviceId')) == str(service)
                and str(value.get('layerId')) == str(layer) and value.get('exceededTransferLimit') is False
                and value.get('resultOffset') == 0, 'feature query is foreign or incomplete')
        arguments = (record.request_arguments or {}).get('arguments') or {}
        require(arguments.get('returnCountOnly', False) is False, 'feature query is count-only')
        collection = value.get('geojson')
        require(isinstance(collection, dict) and collection.get('type') == 'FeatureCollection'
                and isinstance(collection.get('features'), list), 'feature query lacks features')
        features = collection['features']
        require(type(value.get('returnedCount')) is int and value['returnedCount'] == len(features),
                'feature query count differs from returned features')
        ids = [feature.get('properties', {}).get('source_id') if isinstance(feature, dict) else None
               for feature in features]
        require(all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids),
                'feature query lacks distinct source ids')
        return sorted(ids)
    raise ProjectionError('unknown projection operation: ' + ','.join(sorted(keys)))


def substitute_roles(expected, roles):
    """Replace {"$role": name} with the resolved live id; unresolved roles fail closed."""
    if isinstance(expected, dict):
        if set(expected) == {'$role'}:
            name = expected['$role']
            require(isinstance(name, str) and name in roles, 'expectation names an unresolved role: ' + str(name))
            return roles[name]
        require('$role' not in expected, 'malformed role reference')
        return {key: substitute_roles(value, roles) for key, value in expected.items()}
    if isinstance(expected, list):
        return [substitute_roles(value, roles) for value in expected]
    return expected


def resolve_roles(bindings, records, *, agent_principal):
    """Bind logical roles to live ids using only recorded controller responses."""
    roles = {}
    for binding in bindings:
        name = binding.role
        require(isinstance(name, str) and name.split(':', 1)[0] in ('seeded', 'created') and name not in roles,
                'invalid or duplicate logical role: ' + str(name))
        require(binding.step in records, 'role binds an unrecorded step: ' + name)
        record = records[binding.step]
        require(record.json is not None and record.status in (200, 201), 'role source read failed: ' + name)
        if name.startswith('seeded:'):
            require(record.phase == 'seed' and binding.pointer is not None and binding.items is None,
                    'seeded role must point into a seed-phase response: ' + name)
            value = pointer_get(record.json, binding.pointer)
            require(isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) != '',
                    'seeded role is not one scalar: ' + name)
            roles[name] = value
            continue
        require(record.phase == 'observe' and binding.items is not None and binding.id is not None,
                'created role must enumerate observe-phase state: ' + name)
        # Both constraints are mandatory: owned by the agent principal AND created after seed.
        require(binding.owner is not None and binding.before is not None,
                'created role needs both the agent-owner filter and a seed-phase baseline: ' + name)
        rows = pointer_get(record.json, binding.items)
        require(isinstance(rows, list), 'created role enumeration is not a list: ' + name)
        require(binding.before in records, 'created role baseline is not recorded: ' + name)
        baseline_record = records[binding.before]
        require(baseline_record.phase == 'seed' and baseline_record.status == 200
                and baseline_record.kind == record.kind and baseline_record.path == record.path
                and baseline_record.principal == record.principal,
                'created role baseline is not a seed-phase read of the same enumeration: ' + name)
        baseline = pointer_get(baseline_record.json, binding.items)
        require(isinstance(baseline, list), 'created role baseline is not a list: ' + name)
        excluded = {str(pointer_get(row, binding.id)) for row in baseline}
        matches = []
        for row in rows:
            identity = pointer_get(row, binding.id)
            if str(identity) in excluded:
                continue
            if str(pointer_get(row, binding.owner)) != str(agent_principal):
                continue
            matches.append(identity)
        require(len(matches) == 1, f'created role {name} matched {len(matches)} live resources')
        roles[name] = matches[0]
    created = [str(value) for key, value in roles.items() if key.startswith('created:')]
    seeded = {str(value) for key, value in roles.items() if key.startswith('seeded:')}
    require(len(created) == len(set(created)) and not set(created) & seeded,
            'created roles collide with each other or with seeded state')
    return roles
