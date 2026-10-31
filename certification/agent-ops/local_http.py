"""Loopback-only HTTP transport for disposable local trial stacks."""
from __future__ import annotations

import urllib.parse
import urllib.request
import re


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def require_loopback(base_url: str) -> None:
    try:
        origin = urllib.parse.urlsplit(base_url)
        port = origin.port
    except ValueError as exc:
        raise ValueError('invalid local trial origin') from exc
    if (origin.scheme != 'http' or origin.hostname != '127.0.0.1' or port is None or
        origin.username or origin.password or origin.path not in ('', '/') or origin.query or origin.fragment):
        raise ValueError('local trial requires a loopback origin with explicit port')


def open_local(request: urllib.request.Request, *, timeout: float):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)


def require_layer_path(path: str) -> None:
    if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_/-]+', path) or '//' in path:
        raise ValueError('invalid local layer path')
