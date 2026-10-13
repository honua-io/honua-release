import copy
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

import capture_capabilities as capture


REVISION = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = "ghcr.io/honua-io/honua-server:fixture@" + DIGEST
KEY = "private-capture-test-key"


def body(available=False):
    return {"schemaVersion": "honua.capability_manifest.v1",
            "server": {"deploymentRevision": REVISION, "deploymentRevisionSource": "commit-sha"},
            "capabilities": [{"id": "jobs.runner", "lifecycle": "ga", "supported": True, "available": available}]}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "capability-manifest"
        self.manifest = self.root / "platform-manifest.yaml"
        self.manifest.write_text(json.dumps({"components": {"honua-server": {
            "sha": REVISION, "image": IMAGE.split("@")[0], "digest": DIGEST}}}))
        (self.root / "boot.json").write_text(json.dumps({"booted": True, "image": IMAGE}))

    def execute(self, anonymous=None, authenticated=None, statuses=(200, 200)):
        responses = iter(zip(statuses, (body() if anonymous is None else anonymous,
                                      body(True) if authenticated is None else authenticated)))
        calls = []
        def fetch(url, key):
            calls.append((url, key))
            status, document = next(responses)
            return status, json.dumps(document, indent=3).encode()
        report = capture.capture(self.manifest, self.out, "http://localhost:8080", KEY, fetch)
        return report, calls

    def test_raw_bytes_pin_binding_and_auth_availability_are_retained(self):
        original = self.manifest.read_bytes()
        report, calls = self.execute()
        self.assertEqual(report["status"], "pass")
        self.assertEqual([key for _, key in calls], [None, KEY])
        self.assertEqual(report["expectedImage"], IMAGE)
        self.assertEqual(report["expectedRevision"], REVISION)
        self.assertEqual(report["platformManifestSha256"], hashlib.sha256(original).hexdigest())
        for name, available in (("anonymous", False), ("authenticated", True)):
            raw = (self.out / (name + ".json")).read_bytes()
            self.assertEqual(raw, json.dumps(body(available), indent=3).encode())
            self.assertEqual(report["responses"][name]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(report["responses"][name]["capabilities"][0]["available"], available)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertFalse((self.root / "expected-ga-manifest.json").exists())
        self.assertNotIn(KEY, (self.out / "capture.json").read_text())

    def test_malformed_or_wrong_runtime_identity_cannot_pass(self):
        mutations = [
            lambda b: b["server"].update(deploymentRevision="c" * 40),
            lambda b: b["server"].update(deploymentRevisionSource="tag"),
            lambda b: b.update(schemaVersion="unknown"),
            lambda b: b.update(capabilities=[]),
            lambda b: b["capabilities"].append(copy.deepcopy(b["capabilities"][0])),
            lambda b: b["capabilities"][0].update(supported="true"),
            lambda b: b["capabilities"][0].update(available=None),
            lambda b: b["capabilities"][0].pop("lifecycle"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                changed = body(True)
                mutation(changed)
                report, _ = self.execute(authenticated=changed)
                self.assertEqual(report["status"], "fail")
                self.assertTrue((self.out / "authenticated.json").exists())
        for field, value in (("id", "other.id"), ("supported", False), ("lifecycle", "preview")):
            changed = body(True)
            changed["capabilities"][0][field] = value
            self.assertEqual(self.execute(authenticated=changed)[0]["status"], "fail")
        self.assertEqual(self.execute(statuses=(200, 403))[0]["status"], "fail")

    def test_boot_mismatch_refuses_network_and_credential_echo_is_not_retained(self):
        (self.root / "boot.json").write_text(json.dumps({"booted": True, "image": "another-image"}))
        report, calls = self.execute()
        self.assertEqual(report["status"], "fail")
        self.assertFalse(calls)
        (self.root / "boot.json").write_text(json.dumps({"booted": True, "image": IMAGE}))
        document = body()
        document["accidentalEcho"] = KEY
        report, _ = self.execute(anonymous=document)
        self.assertEqual(report["status"], "fail")
        self.assertFalse((self.out / "anonymous.json").exists())
        self.assertNotIn(KEY, (self.out / "capture.json").read_text())

    def test_http_credentials_only_on_authenticated_request_and_no_redirect(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("X-API-Key")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-follow")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"fixture": true}')
            def log_message(self, *_args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = "http://127.0.0.1:" + str(server.server_port)
            self.assertEqual(capture.read_response(origin + "/manifest", None), (200, b'{"fixture": true}'))
            self.assertEqual(capture.read_response(origin + "/manifest", KEY)[0], 200)
            self.assertEqual(capture.read_response(origin + "/redirect", KEY)[0], 302)
            self.assertEqual(requests, [("/manifest", None), ("/manifest", KEY), ("/redirect", KEY)])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
