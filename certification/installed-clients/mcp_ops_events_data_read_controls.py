"""Controlled HTTP transport and semantic mutations; no candidate qualification."""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import mcp_ops_events_data_read as reader
import mcp_ops_events_state as state
from mcp_render_validation_controls import FIXTURE, bootstrap, runtime

KEY = "controlled-http-key-never-retained"
SALT = "a" * 32


@contextmanager
def controlled_http(body=None, *, status=200, media="application/json"):
    """A real local HTTP responder, explicitly separate from a product receipt."""
    raw = json.dumps({"exceededTransferLimit": False, "count": len(json.loads(FIXTURE)["area"]["features"])}).encode() if body is None else body
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"path": self.path, "keyPresent": self.headers.get("X-API-Key") == KEY,
                "authorizationPresent": self.headers.get("Authorization") is not None,
                "cookiePresent": self.headers.get("Cookie") is not None})
            self.send_response(status)
            self.send_header("Content-Type", media)
            if status == 302:
                self.send_header("Location", "http://foreign.invalid/credential-forwarding-refused")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class LocalControlTransport:
        def __init__(self):
            self.selected_urls = []

        def open(self, request, timeout):
            # Exercise actual HTTP bytes on an ephemeral owned test port. The
            # production observer still authors and checks its fixed URL.
            self.selected_urls.append(request.full_url)
            parsed = urlsplit(request.full_url)
            url = f"http://127.0.0.1:{server.server_port}{parsed.path}?{parsed.query}"
            local = Request(url, method=request.method, headers=dict(request.header_items()))
            return build_opener(ProxyHandler({}), reader.RefuseRedirect()).open(local, timeout=timeout)

    transport = LocalControlTransport()
    try:
        yield transport, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def observed_read(transport, *, inspect=None):
    return reader.read(FIXTURE, bootstrap(), credential=KEY, credential_salt=SALT,
        inspect=inspect or (lambda source, image: runtime()),
        clock=lambda: datetime(2026, 10, 19, 22, tzinfo=timezone.utc), opener=transport)


def identity():
    fingerprint = hashlib.scrypt(KEY.encode(), salt=bytes.fromhex(SALT), n=16384, r=8, p=1, dklen=32).hex()
    return {"credentialFingerprint": "scrypt:" + SALT + ":" + fingerprint}


class LimitedDataReadControls(unittest.TestCase):
    def test_actual_http_count_read_uses_fixed_owned_route_and_private_key(self):
        with controlled_http() as (transport, requests):
            value = observed_read(transport)
        expected_count = len(json.loads(FIXTURE)["area"]["features"])
        self.assertGreater(expected_count, 0)
        self.assertEqual(reader.verify(value, FIXTURE, bootstrap(), identity(), runtime()), expected_count)
        self.assertEqual(transport.selected_urls, [reader.data_url(FIXTURE, bootstrap())])
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]["keyPresent"])
        self.assertFalse(requests[0]["authorizationPresent"] or requests[0]["cookiePresent"])
        self.assertEqual(requests[0]["path"], urlsplit(value["url"]).path + "?" + urlsplit(value["url"]).query)
        self.assertNotIn(KEY, json.dumps(value))
        self.assertEqual(value["credentialFingerprint"], identity()["credentialFingerprint"])

    def test_success_status_cannot_hide_wrong_empty_boolean_error_echo_or_duplicate_body(self):
        count = len(json.loads(FIXTURE)["area"]["features"])
        bodies = [b'{"count":0,"exceededTransferLimit":false}', b'{"count":true,"exceededTransferLimit":false}', b'{"error":{"code":403}}',
            json.dumps({"count": count + 1, "exceededTransferLimit": False}).encode(),
            json.dumps({"count": count, "exceededTransferLimit": False, "echo": KEY}).encode(),
            ('{"count":' + str(count) + ',"count":' + str(count) + '}').encode(),
            json.dumps({"count": count}).encode(),
            json.dumps({"count": count, "exceededTransferLimit": True}).encode(),
            json.dumps({"count": count, "exceededTransferLimit": 0}).encode(), b" " * 65537]
        for body in bodies:
            with self.subTest(body_length=len(body)), controlled_http(body) as (transport, _):
                with self.assertRaises((ValueError, TypeError)):
                    observed_read(transport)

    def test_actual_http_error_wrong_media_and_redirect_are_not_read_success(self):
        for status, media in ((401, "application/json"), (403, "application/json"),
                              (200, "text/html"), (302, "application/json")):
            with self.subTest(status=status, media=media), controlled_http(status=status, media=media) as (transport, requests):
                with self.assertRaises((ValueError, HTTPError)):
                    observed_read(transport)
                self.assertEqual(len(requests), 1)

    def test_original_response_key_runtime_and_clock_mutations_refuse(self):
        with controlled_http() as (transport, _):
            original = observed_read(transport)
        mutations = (
            lambda v: v.update(schema="fixture-success"),
            lambda v: v.update(method="POST"),
            lambda v: v.update(url=v["url"].replace("allocated-service-id", "foreign-service")),
            lambda v: v.update(status=True),
            lambda v: v.update(credentialFingerprint="scrypt:" + "b" * 32 + ":" + "c" * 64),
            lambda v: v.update(responseText=v["responseText"] + " "),
            lambda v: v["runtimeBefore"].update(candidateSourceSha="0" * 40),
            lambda v: v["runtimeAfter"].update(startedAt="2026-10-19T21:00:00Z"),
            lambda v: v.update(startedAt="2026-10-19T23:00:00Z"),
            lambda v: v.update(startedAt="2026-10-18T23:00:00Z"),
            lambda v: v.update(extra="unretained"),
        )
        for mutation in mutations:
            value = copy.deepcopy(original)
            mutation(value)
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, TypeError)):
                reader.verify(value, FIXTURE, bootstrap(), identity(), runtime())
        # Rehashing a coherently altered response still cannot award a read.
        value = copy.deepcopy(original)
        value["responseText"] = '{"count":0,"exceededTransferLimit":false}'
        value["responseSha256"] = "sha256:" + hashlib.sha256(value["responseText"].encode()).hexdigest()
        with self.assertRaises(ValueError):
            reader.verify(value, FIXTURE, bootstrap(), identity(), runtime())

    def test_runtime_restart_during_actual_read_is_refused(self):
        readings = iter((runtime(), {**runtime(), "startedAt": "2026-10-19T21:00:00Z"}))
        with controlled_http() as (transport, _), self.assertRaises(ValueError):
            observed_read(transport, inspect=lambda source, image: next(readings))

    def test_invalid_private_key_or_salt_is_rejected_before_any_runtime_or_http(self):
        calls = []
        for key, salt in ((None, SALT), ("", SALT), ("header\r\ninjection", SALT), (KEY, "bad")):
            with self.subTest(key_valid=key == KEY, salt_valid=salt == SALT), self.assertRaises(ValueError):
                reader.read(FIXTURE, bootstrap(), credential=key, credential_salt=salt,
                    inspect=lambda *_: calls.append("inspect"), opener=object())
        self.assertEqual(calls, [])


class NormalPreflightHttpControls(unittest.TestCase):
    def test_actual_http_preflight_preserves_original_json_and_managed_key_binding(self):
        body = json.dumps({"platformRelease": {"releaseDeclared": True, "releaseVersion": "2026.1-rc.2",
            "isCoVersioned": False, "skewedIds": ["cert-ops-skew"]}}).encode()
        with controlled_http(body) as (transport, requests):
            value = state.preflight(KEY, SALT, opener=transport)
        self.assertEqual(transport.selected_urls, [state.PREFLIGHT_URL])
        self.assertEqual(value["responseText"].encode(), body)
        self.assertEqual(value["responseSha256"], "sha256:" + hashlib.sha256(body).hexdigest())
        self.assertEqual(value["credentialFingerprint"], identity()["credentialFingerprint"])
        self.assertTrue(requests[0]["keyPresent"])
        self.assertNotIn(KEY, json.dumps(value))

    def test_actual_http_preflight_refuses_private_echo_and_nonobject_body(self):
        bodies = [b"[]", json.dumps({"unsafeEcho": KEY}).encode(),
            json.dumps({"unsafeEcho": "controlled%2Dhttp%2Dkey%2Dnever%2Dretained"}).encode()]
        for body in bodies:
            with self.subTest(body_length=len(body)), controlled_http(body) as (transport, _), self.assertRaises(ValueError):
                state.preflight(KEY, SALT, opener=transport)


if __name__ == "__main__":
    unittest.main(verbosity=2)
