import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from live_observer import observe_layer


class Handler(BaseHTTPRequestHandler):
    corrupt = False
    credentials = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.credentials.append(self.headers.get("X-API-Key"))
        path = urlparse(self.path)
        params = parse_qs(path.query)
        if not path.path.endswith("/query"):
            body = {"objectIdField": "id", "fields": [{"name": "id", "type": "integer"}]}
        elif "returnIdsOnly" in params:
            body = {"objectIds": [1, 2]}
        else:
            oid = int(params["objectIds"][0])
            body = {"features": [] if self.corrupt and oid == 2 else [{"attributes": {"id": oid, "name": "item"}, "geometry": {"x": oid, "y": 0}}]}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


def test_controller_reads_all_features_and_fails_on_missing():
    Handler.corrupt = False
    Handler.credentials = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        result = observe_layer(base, "/layer", "controller-secret")
        assert result["count"] == 2
        assert [f["id"] for f in result["features"]] == [1, 2]
        assert all(len(f["geometry_sha256"]) == 64 for f in result["features"])
        assert len(result["evidence"]) == 4
        assert set(Handler.credentials) == {"controller-secret"}
        assert "controller-secret" not in json.dumps(result)
        Handler.corrupt = True
        with pytest.raises(ValueError, match="missing or ambiguous"):
            observe_layer(base, "/layer", "controller-secret")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
