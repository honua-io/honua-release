"""An HTTP opener that never follows redirects, so request credentials stay on their origin."""
from __future__ import annotations

import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect; urllib then raises the 3xx response as an HTTPError."""

    def redirect_request(self, *_redirect):  # urllib passes req, fp, code, msg, headers, newurl
        return None


def opener() -> urllib.request.OpenerDirector:
    """An opener whose responses are never redirected to another URL."""
    return urllib.request.build_opener(NoRedirect)
