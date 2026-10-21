#!/usr/bin/env python3
"""Real published-package process controls against controlled loopback HTTP fixtures.

This does not import services or emit certification evidence. It checks application
configuration behavior, actual SDK request routing, credential isolation and stale
configuration refusals before the separate real-candidate journey can qualify.
"""
from __future__ import annotations

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import yaml

import import_fidelity_journeys as journeys
import produce_import_fidelity as producer


KEY = "synthetic-readback-control-key"


def server(stack, label, candidate):
    requests = []
    features = [{"attributes": {"OBJECTID": oid, "label": label},
                 "geometry": {"x": oid + (10 if candidate else 0), "y": oid}}
                for oid in (1, 2)]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.respond(parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()))

        def do_GET(self):
            self.respond(parse_qs(urlsplit(self.path).query))

        def respond(self, parameters):
            authorized = self.headers.get("X-API-Key") == KEY if candidate else "X-API-Key" not in self.headers
            requests.append({"path": urlsplit(self.path).path, "credentialCorrect": authorized})
            path = urlsplit(self.path).path
            status = 200
            if not authorized:
                status, body = 401, {"error": {"code": 401, "message": "fixture authorization"}}
            elif path.endswith("/query"):
                if parameters.get("returnCountOnly", [""])[0].lower() == "true":
                    body = {"count": 2}
                elif parameters.get("returnIdsOnly", [""])[0].lower() == "true":
                    body = {"objectIdFieldName": "OBJECTID", "objectIds": [1, 2]}
                else:
                    body = {"features": features, "exceededTransferLimit": False}
            else:
                body = {"id": 1, "objectIdField": "OBJECTID", "geometryType": "esriGeometryPoint",
                        "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"},
                                   {"name": "label", "type": "esriFieldTypeString"}], "maxRecordCount": 2}
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    stack.callback(http.server_close)
    stack.callback(lambda: thread.join(timeout=5))
    stack.callback(http.shutdown)
    return f"http://127.0.0.1:{http.server_port}/rest/services/{label}/FeatureServer", features, requests


def inspect(client, url, candidate, expected):
    layer = client.call("layer", baseUrl=url, layerId=1, candidate=candidate)
    assert layer["objectIdField"] == "OBJECTID"
    assert client.call("count", baseUrl=url, layerId=1, candidate=candidate) == 2
    assert sorted(client.call("ids", baseUrl=url, layerId=1, candidate=candidate)) == [1, 2]
    value = client.call("query", baseUrl=url, layerId=1, candidate=candidate,
                        query={"where": "1=1", "outFields": "*", "returnGeometry": True})
    assert value["features"] == expected and value["exceededTransferLimit"] is False


def controls(sdk):
    with ExitStack() as stack:
        original, source_features, source_requests = server(stack, "original", False)
        imported, imported_features, imported_requests = server(stack, "imported", True)
        client = journeys.ConfiguredSdk(sdk, original, 1)
        inspect(client, original, False, source_features)
        for address, candidate, authenticated in (("https://other.example/FeatureServer", True, False),
                                                   ("https://other.example/FeatureServer", False, True)):
            before = len(source_requests) + len(imported_requests)
            try:
                sdk.call("repoint-readback", configurationId=client.configuration_id, revision=1,
                         baseUrl=address, layerId=1, candidate=candidate, sourceAuth=authenticated)
            except producer.ProducerError:
                pass
            else:
                raise AssertionError("unapproved credential destination was accepted")
            assert len(source_requests) + len(imported_requests) == before
        client.repoint(imported, 1)
        inspect(client, imported, True, imported_features)
        client.restore()
        inspect(client, original, False, source_features)
        assert len(source_requests) >= 8 and len(imported_requests) >= 4
        assert all(item["credentialCorrect"] for item in [*source_requests, *imported_requests])
        for operation, arguments in [
            ("configured-read", {"configurationId": client.configuration_id, "revision": 1, "readOperation": "layer"}),
            ("configured-read", {"configurationId": "f" * 32, "revision": 3, "readOperation": "layer"}),
            ("configured-read", {"configurationId": client.configuration_id, "revision": 3, "readOperation": "layer", "baseUrl": imported}),
            ("rollback-readback", {"configurationId": client.configuration_id, "revision": 3, "baseUrl": imported}),
            ("repoint-readback", {"configurationId": client.configuration_id, "revision": 3,
                                   "baseUrl": imported, "layerId": 1, "candidate": True, "sourceAuth": False}),
            ("configure-readback", {"revision": 0, "baseUrl": original, "layerId": 1, "candidate": False, "sourceAuth": False}),
        ]:
            before = len(source_requests) + len(imported_requests)
            try:
                sdk.call(operation, **arguments)
            except producer.ProducerError:
                pass
            else:
                raise AssertionError("invalid configuration transition was accepted")
            assert len(source_requests) + len(imported_requests) == before
        return {"configurationRevisions": [1, 2, 3], "actualReadOperations": len(client.observations),
                "refusalControls": 8, "credentialIsolation": True, "qualification": False}


def main():
    manifest = yaml.safe_load((producer.ROOT / "platform-manifest.yaml").read_text())
    root = producer.ROOT / "out" / "import-client-readback-controls"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clean-", dir=root) as directory:
        command, env, consumer = producer.install_consumer(manifest, Path(directory) / "published")
        env["IMPORT_FIDELITY_API_KEY"] = KEY
        # No external source/cloud/native credentials belong in controlled fixtures.
        for name in ("IMPORT_FIDELITY_SOURCE_TOKEN", "IMPORT_FIDELITY_AUTH_SOURCE_URL"):
            env.pop(name, None)
        sdk = producer.Sdk(command, env, consumer)
        try:
            result = controls(sdk)
            assert consumer["assemblyMatchesPackage"] is True
            print(json.dumps({"control": "published-sdk-configured-readback", "package": consumer["version"], **result}))
        finally:
            sdk.close()


if __name__ == "__main__":
    main()
