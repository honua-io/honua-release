"""Exercise a built published-SDK bridge over real loopback HTTP.

Run explicitly with --dll. This validates SDK serialization/transport, not a
candidate import or release qualification; the peer is an authored HTTP fixture.
"""
import argparse
import json
import os
import subprocess
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONNECTION = "11111111-1111-4111-8111-111111111111"
JOB = "import-job-1"


def exercise(dll):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            if self.headers.get("Transfer-Encoding") == "chunked":
                chunks = []
                while True:
                    length = int(self.rfile.readline().strip().split(b";", 1)[0], 16)
                    if length == 0:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(length))
                    assert self.rfile.read(2) == b"\r\n"
                body = b"".join(chunks)
            else:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.command, self.path, self.headers.get("X-API-Key"), json.loads(body) if body else None))
            code = 200
            if self.path.endswith("/connections/"):
                value = {"data": {"connectionId": CONNECTION, "name": "source"}, "success": True}
            elif self.path.endswith("/test"):
                value = {"data": {"connectionId": CONNECTION, "isHealthy": True}, "success": True}
            elif self.path.endswith("/start"):
                code = 202
                value = {"jobId": JOB, "message": "queued", "statusUrl": "/status", "cancelUrl": "/cancel"}
            elif "/import/geoservices/jobs/" in self.path:
                value = {"jobId": JOB, "status": "Completed", "sourceServiceUrl": "https://fixture.example/FeatureServer",
                         "sourceLayerId": 0, "tableName": "source", "featuresProcessed": 2, "failedFeatures": 0}
            else:
                value = {"data": {"layerId": 7, "layerName": "source", "table": "source", "schema": "public"}, "success": True}
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
        do_GET = do_POST = respond
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    credential = "private-sdk-fixture-key"
    env = {**os.environ, "HONUA_JOURNEY_BASE_URL": f"http://127.0.0.1:{server.server_port}",
           "HONUA_JOURNEY_SDK_KEY": credential}
    calls = [
        ("CreateConnectionAsync", [{"name": "source", "host": "db", "databaseName": "honua", "username": "honua", "password": "private-database-key"}]),
        ("TestConnectionAsync", [CONNECTION]),
        ("StartGeoservicesImportAsync", [{"serviceUrl": "https://fixture.example/FeatureServer", "layerId": 0, "tableName": "source", "autoPublish": False}]),
        ("GetGeoservicesImportJobStatusAsync", [JOB]),
        ("PublishLayerAsync", [CONNECTION, {"schema": "public", "table": "source", "layerName": "source", "serviceName": "journey"}]),
    ]
    try:
        outputs = []
        for method, arguments in calls:
            result = subprocess.run(["dotnet", str(dll)], env=env,
                                    input=json.dumps({"method": method, "arguments": arguments}),
                                    capture_output=True, text=True, timeout=30, check=False)
            assert result.returncode == 0, f"{method}: bridge exited {result.returncode}"
            value = json.loads(result.stdout)
            assert value["status"] == "pass" and value["method"] == method
            assert credential not in result.stdout + result.stderr
            assert "private-database-key" not in result.stdout + result.stderr
            outputs.append(value["result"])
        assert len(seen) == 5
        assert Counter(path for _, path, _, _ in seen)["/api/v1/admin/import/geoservices/start"] == 1
        assert all(key == credential for _, _, key, _ in seen)
        assert seen[0][3]["password"] == "private-database-key"
        assert outputs[0]["connectionId"] == CONNECTION
        assert outputs[1]["isHealthy"] is True
        assert outputs[2]["jobId"] == JOB
        assert outputs[3]["featuresProcessed"] == 2
        assert outputs[4]["layerId"] == 7
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    print("5 published-SDK loopback calls passed; import submitted exactly once; no credential output; qualification=false")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", required=True, type=Path)
    exercise(parser.parse_args().dll.resolve())
