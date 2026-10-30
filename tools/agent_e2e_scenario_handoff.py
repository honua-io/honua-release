"""Independent customer-handoff observations from the candidate's own APIs.

The model transcript is not an input. Published URLs come from the service
catalogue. Each URL is read back and counts only when that read is the same
service. Access mode is the anonymous probe of that URL, not a field the
document claims. Feature counts come from returnCountOnly. Names, ids,
publication status and approval status come from the service, layer and
proposal documents. Refusal reason codes come from non-success authenticated
bodies and from refused proposals.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from agent_e2e_pack import PackError


_CODE = re.compile(r'[A-Za-z0-9_.:-]{1,80}')
_LAYER_TYPES = {'FeatureServer', 'MapServer'}
_PROPOSAL_WORD = {
    'succeeded': 'published', 'completed': 'published',
    'awaitingapproval': 'pending', 'requiresapproval': 'pending', 'planned': 'pending',
    'submitted': 'pending', 'executing': 'pending', 'reconciling': 'pending',
    'rejected': 'refused', 'denied': 'refused', 'refused': 'refused', 'failed': 'failed',
}
_REFUSAL = {'rejected', 'denied', 'refused', 'failed'}


def require(value, reason):
    if not value:
        raise PackError(reason)


def handoff_observations(http):
    """Same read every scenario backend returns. Model text is not accepted."""
    return observe_handoff(http)


def _canonical_url(value):
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if parts.port:
        host = f'{host}:{parts.port}'
    path = parts.path.rstrip('/') or '/'
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ''))


def _absolute(endpoint, url):
    if '://' not in url:
        url = endpoint.rstrip('/') + '/' + url.lstrip('/')
    canonical = _canonical_url(url)
    require(canonical is not None, 'published URL is not an http resource')
    base = urlsplit(endpoint)
    parts = urlsplit(canonical)
    require(parts.scheme == base.scheme and parts.hostname and base.hostname and
            parts.hostname.lower() == base.hostname.lower() and parts.port == base.port,
            'published URL is not on the candidate')
    return canonical, parts.path or '/'


def _get(http, step, path):
    answer = http.json(step, 'GET', path)
    observation = answer['observation']
    return observation['httpStatus'], observation.get('body')


def _add(items, value):
    if value not in items:
        items.append(value)


def _codes_from(body):
    found = []
    if not isinstance(body, dict):
        return found
    for key in ('code', 'errorCode', 'reasonCode'):
        value = body.get(key)
        if isinstance(value, str) and _CODE.fullmatch(value):
            _add(found, value)
    error = body.get('error')
    if isinstance(error, dict):
        value = error.get('code')
        if isinstance(value, str) and _CODE.fullmatch(value):
            _add(found, value)
        elif type(value) is int and not isinstance(value, bool):
            _add(found, str(value))
    elif isinstance(error, str) and _CODE.fullmatch(error):
        _add(found, error)
    kind = body.get('type')
    if isinstance(kind, str) and kind.startswith('urn:'):
        suffix = kind.rsplit(':', 1)[-1]
        if _CODE.fullmatch(suffix):
            _add(found, suffix)
    return found


def _service_name(body):
    if not isinstance(body, dict):
        return None
    for key in ('serviceName', 'name'):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _layers(body):
    rows = body.get('layers') if isinstance(body, dict) else None
    require(isinstance(rows, list), 'published service did not list its layers')
    layers = []
    for row in rows:
        require(isinstance(row, dict) and type(row.get('id')) is int and not isinstance(row.get('id'), bool) and
                isinstance(row.get('name'), str) and row['name'], 'published layer id or name was not observed')
        layers.append(row)
    return layers


def _catalogue(http):
    status, body = _get(http, 'handoff-catalog', '/rest/services?f=json')
    require(status == 200, 'candidate service catalogue was not observed')
    require(isinstance(body, dict) and isinstance(body.get('services'), list),
            'candidate catalogue did not list published services')
    services = list(body['services'])
    folders = body.get('folders') or []
    require(isinstance(folders, list), 'candidate catalogue folders are malformed')
    for index, name in enumerate(folders):
        require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', name),
                'candidate catalogue folder name is malformed')
        folder_status, folder = _get(http, f'handoff-folder-{index}', '/rest/services/'+quote(name, safe='')+'?f=json')
        require(folder_status == 200 and isinstance(folder, dict) and isinstance(folder.get('services'), list),
                'candidate folder catalogue did not list published services')
        services.extend(folder['services'])
    return services


def _proposals(http):
    status, body = _get(http, 'handoff-proposals', '/api/v1/admin/proposals')
    codes = [] if status == 200 else _codes_from(body)
    require(status == 200 and isinstance(body, dict) and isinstance(body.get('proposals'), list),
            'candidate proposal list was not observed')
    require(len(body['proposals']) <= 50, 'candidate proposal list is unbounded')
    details = []
    for index, row in enumerate(body['proposals']):
        require(isinstance(row, dict) and isinstance(row.get('proposalId'), str) and
                re.fullmatch(r'[A-Za-z0-9_-]{1,200}', row['proposalId']),
                'candidate proposal list lacks an id')
        detail_status, detail = _get(http, f'handoff-proposal-{index}',
                                      '/api/v1/admin/proposals/'+quote(row['proposalId'], safe=''))
        if detail_status != 200:
            codes.extend(code for code in _codes_from(detail) if code not in codes)
            require(False, 'candidate proposal was not observed')
        require(isinstance(detail, dict) and detail.get('proposalId') == row['proposalId'] and
                isinstance(detail.get('status'), str) and detail['status'],
                'candidate proposal detail belongs to another id')
        details.append(detail)
        if detail['status'].casefold() in _REFUSAL:
            for key in ('resolutionReason',):
                value = detail.get(key)
                if isinstance(value, str) and _CODE.fullmatch(value):
                    _add(codes, value)
            reasons = detail.get('blockingReasons') or []
            require(isinstance(reasons, list), 'candidate proposal refusal reasons are malformed')
            for value in reasons:
                if isinstance(value, str) and _CODE.fullmatch(value):
                    _add(codes, value)
    return details, codes


def _anonymous(http):
    if http.client.kind == 'anonymous':
        return http
    attempt = http.identity.get('attemptId')
    require(isinstance(attempt, str) and attempt, 'handoff access probe lacks the candidate attempt')
    anon_id = ('anon-'+attempt)[:80]
    require(re.fullmatch(r'[A-Za-z0-9-]{1,80}', anon_id) and anon_id != attempt,
            'handoff access probe attempt id is invalid')
    from agent_e2e_scenario_backend import ScenarioHttp
    return ScenarioHttp(durable=http.durable, context=http.context, endpoint=http.client.endpoint,
                        credential_env=None, attempt_id=anon_id, credential_kind='anonymous')


def _identifies(body, name, layer_id=None):
    if _service_name(body) == name:
        return True
    if layer_id is None or not isinstance(body, dict):
        return False
    observed = body.get('id')
    return observed == layer_id or (isinstance(observed, str) and observed == str(layer_id))


def observe_handoff(http):
    """Read the candidate. Do not pass model text, tool output, or a transcript."""
    require(hasattr(http, 'json') and hasattr(http, 'client') and isinstance(getattr(http.client, 'endpoint', None), str),
            'handoff observation requires the candidate HTTP client')
    endpoint = http.client.endpoint
    resources, probes, names, ids, seen_urls = [], [], [], [], set()
    feature_counts, layer_count, service_count, served_any = [], 0, 0, False
    access_modes = []
    refusal = []
    anonymous = _anonymous(http)
    for index, entry in enumerate(_catalogue(http)):
        require(isinstance(entry, dict) and isinstance(entry.get('name'), str) and entry['name'] and
                isinstance(entry.get('type'), str) and entry['type'] and isinstance(entry.get('url'), str) and entry['url'],
                'candidate catalogue entry lacks a published name, type or URL')
        canonical, path = _absolute(endpoint, entry['url'])
        if canonical in seen_urls:
            continue
        seen_urls.add(canonical)
        status, body = _get(http, f'handoff-service-{index}', path+'?f=json')
        if not 200 <= status <= 299:
            for code in _codes_from(body):
                _add(refusal, code)
        # A 2xx for a different service does not resolve this catalogue URL.
        served = 200 <= status <= 299 and _identifies(body, entry['name'])
        anon_status, anon_body = _get(anonymous, f'handoff-access-{index}', path+'?f=json')
        anon_served = 200 <= anon_status <= 299 and _identifies(anon_body, entry['name'])
        if served and anon_served:
            mode = 'public'
        elif served and anon_status in (401, 403):
            mode = 'private'
        else:
            mode = None
        if mode is not None:
            access_modes.append(mode)
        resources.append({'url': canonical, 'httpStatus': status, 'resourceId': entry['name'], 'served': served,
                          'name': entry['name'], 'accessMode': mode})
        probes.append({'method': 'GET', 'url': canonical, 'worked': served, 'httpStatus': status})
        service_count += 1
        _add(names, entry['name'])
        _add(ids, entry['name'])
        if not served or entry['type'] not in _LAYER_TYPES:
            continue
        served_any = True
        for layer in _layers(body):
            layer_count += 1
            layer_id = layer['id']
            layer_path = path+'/'+quote(str(layer_id), safe='')
            layer_status, layer_body = _get(http, f'handoff-layer-{index}-{layer_id}', layer_path+'?f=json')
            if layer_status != 200:
                for code in _codes_from(layer_body):
                    _add(refusal, code)
                feature_counts.append(None)
                continue
            require(isinstance(layer_body, dict) and layer_body.get('id') == layer_id and layer_body.get('name') == layer['name'],
                    'layer document does not match the published layer')
            _add(names, layer['name'])
            _add(ids, str(layer_id))
            # The customer can cite the layer URL. The scorer matches it exactly,
            # so a validated layer that is absent here becomes a false success.
            layer_canonical, _layer_path = _absolute(endpoint, layer_path)
            if layer_canonical not in seen_urls:
                seen_urls.add(layer_canonical)
                resources.append({'url': layer_canonical, 'httpStatus': layer_status, 'resourceId': str(layer_id),
                                  'served': True, 'name': layer['name'], 'accessMode': mode})
                probes.append({'method': 'GET', 'url': layer_canonical, 'worked': True, 'httpStatus': layer_status})
            count_status, count_body = _get(http, f'handoff-count-{index}-{layer_id}',
                                             layer_path+'/query?'+urlencode({'where': '1=1', 'returnCountOnly': 'true', 'f': 'json'}))
            count = count_body.get('count') if isinstance(count_body, dict) else None
            if count_status == 200 and type(count) is int and not isinstance(count, bool) and count >= 0:
                feature_counts.append(count)
            else:
                feature_counts.append(None)
                for code in _codes_from(count_body):
                    _add(refusal, code)
    proposals, proposal_codes = _proposals(http)
    for code in proposal_codes:
        _add(refusal, code)
    publication, approval = [], []
    for detail in proposals:
        _add(publication, detail['status'])
        _add(approval, detail['status'])
        _add(ids, detail['proposalId'])
    if not publication:
        publication.append('published' if served_any else 'unpublished')
    statuses = []
    private = any(mode == 'private' for mode in access_modes)
    if private:
        _add(statuses, 'private')
    elif any(mode == 'public' for mode in access_modes) and served_any:
        _add(statuses, 'published')
    for detail in proposals:
        word = _PROPOSAL_WORD.get(detail['status'].casefold())
        # A private resource is not a public publication, whatever the proposal called it.
        if word == 'published' and private:
            continue
        if word:
            _add(statuses, word)
    if served_any:
        _add(statuses, 'serving')
    counts = {}
    if service_count:
        counts['services'] = service_count
    if layer_count:
        counts['layers'] = layer_count
    if feature_counts and all(type(count) is int for count in feature_counts):
        counts['features'] = sum(feature_counts)
    limitations = [{'code': code, 'summary': code} for code in refusal]
    unique_modes = list(dict.fromkeys(access_modes))
    return {'resources': resources, 'accessProbes': probes,
            'facts': {'counts': counts, 'names': names, 'statuses': statuses},
            'ids': ids, 'accessMode': unique_modes[0] if len(unique_modes) == 1 else None,
            'publicationStatuses': publication, 'approvalStatuses': approval,
            'refusalReasonCodes': refusal, 'limitations': limitations}
