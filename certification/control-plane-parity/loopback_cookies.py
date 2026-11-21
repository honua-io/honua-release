"""Scripted HTTP session that sends Secure cookies to a loopback Production server.

The server marks its session cookies Secure outside Development and Test. Browsers treat
loopback HTTP (localhost, 127.0.0.1, ::1) as a secure context and send Secure cookies to it;
Python's cookiejar sends them only over https. LoopbackSession matches browsers for loopback
hosts and keeps the default rule everywhere else.
"""
import urllib.parse
from http.cookiejar import DefaultCookiePolicy

import requests

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class LoopbackSecureContextPolicy(DefaultCookiePolicy):
    def return_ok_secure(self, cookie, request):
        if super().return_ok_secure(cookie, request):
            return True
        parts = urllib.parse.urlsplit(request.get_full_url())
        return parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS


class LoopbackSession(requests.Session):
    """requests merges cookies into a fresh jar with the default policy before each request,
    so the Cookie header is rebuilt here from the session jar under the loopback policy."""

    def __init__(self):
        super().__init__()
        self.cookies.set_policy(LoopbackSecureContextPolicy())

    def prepare_request(self, request):
        prepared = super().prepare_request(request)
        prepared.headers.pop("Cookie", None)
        prepared.prepare_cookies(self.cookies)
        return prepared
