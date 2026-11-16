"""Observe the Launch Stack's effective Console and API HSTS responses.

The producer injects a TLS-verifying transport; this module opens no connections.
Header compliance does not establish browser preload-list membership.
"""
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit


class HttpsObservationError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise HttpsObservationError(reason)


def checked_origin(origin):
    require(isinstance(origin, str) and not re.search(r'[\x00-\x20\x7f]', origin),
            'Invalid HTTPS origin characters')
    parsed = urlsplit(origin)
    require(parsed.scheme == 'https' and parsed.hostname and parsed.username is None
            and parsed.password is None and parsed.port in (None, 443)
            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment,
            'Expected one HTTPS frontend origin')
    return origin.rstrip('/')


def hsts_age(headers):
    require(isinstance(headers, list), 'Transport must preserve repeated headers')
    values = []
    for entry in headers:
        require(isinstance(entry, (tuple, list)) and len(entry) == 2
                and all(isinstance(part, str) for part in entry), 'Malformed response headers')
        name, value = entry
        require(not any(c in name + value for c in '\r\n'), 'Invalid response header characters')
        if name.lower() == 'strict-transport-security':
            values.append(value)
    require(len(values) == 1, 'Missing or ambiguous HSTS policy')
    directives = {}
    for directive in values[0].split(';'):
        match = re.fullmatch(r'\s*([A-Za-z][A-Za-z-]*)(?:\s*=\s*("[0-9]+"|[0-9]+))?\s*', directive)
        require(match is not None, 'Malformed HSTS directive')
        name, value = match.groups()
        name = name.lower()
        require(name not in directives and name in ('max-age', 'includesubdomains', 'preload'),
                'Duplicate or unsupported HSTS directive')
        directives[name] = value
    require(set(directives) == {'max-age', 'includesubdomains', 'preload'},
            'Incomplete hardened HSTS policy')
    require(directives['includesubdomains'] is None and directives['preload'] is None
            and directives['max-age'] is not None, 'Invalid HSTS directive value')
    digits = directives['max-age'].strip('"')
    require(len(digits) <= 12, 'HSTS lifetime is out of range')
    age = int(digits)
    require(age >= 31536000, 'HSTS lifetime is shorter than one year')
    return age


def observe_https(request, origin):
    """Request Console/API without redirects using the producer's bound TLS client.

    request(url, follow_redirects=False) must verify certificates and hostname,
    preserve duplicate header pairs and return url/status/headers/redirected.
    Do not supply a transport that disables TLS validation or follows redirects.
    Transport errors propagate; bodies, cookies and header values are not emitted.
    """
    origin = checked_origin(origin)
    started = datetime.now(timezone.utc).isoformat()
    observations = []
    for path in ('/console/', '/healthz/ready'):
        url = origin + path
        response = request(url, follow_redirects=False)
        require(response.get('url') == url and response.get('redirected') is False,
                'Response was redirected or belongs to another URL')
        require(type(response.get('status')) is int and response['status'] == 200,
                'Console or API readiness response was unsuccessful')
        age = hsts_age(response.get('headers'))
        observations.append({'path': path, 'status': 200, 'hstsMaxAge': age})
    return {'startedAt': started, 'completedAt': datetime.now(timezone.utc).isoformat(),
            'responses': observations}
