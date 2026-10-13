"""Exercise the real shell driver against controlled HTTP responses, not copied logic."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name == "nt" or not all(shutil.which(tool) for tool in ("bash", "curl", "jq")),
    reason="Shell driver controls require the Linux gate's bash, curl and jq",
)


def exercise(tmp_path, collections, *, ogc_count=2, ogc_status=200, catalog_status=200,
             geo_status=200, odata_status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            target = urlsplit(self.path)
            requests.append((target.path, parse_qs(target.query)))
            status, body = 200, {}
            if target.path == "/healthz/ready":
                body = {"ready": True}
            elif target.path == "/rest/services/maui-zoning/FeatureServer/2/query":
                status, body = geo_status, {"count": 2}
            elif target.path == "/odata/Layers(2)/Features":
                status, body = odata_status, {"@odata.count": 2, "value": [{}, {}]}
            elif target.path == "/ogc/features/collections":
                status, body = catalog_status, {"collections": collections}
            elif target.path == "/ogc/features/collections/2/items":
                status, body = ogc_status, {"numberMatched": ogc_count, "features": [{}] * ogc_count}
            else:
                status, body = 404, {"error": "unexpected route"}
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    (tmp_path / "seed-manifest.json").write_text(json.dumps({
        "service": "maui-zoning", "layers": {"maui_zoning": 2},
    }))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            ["bash", str(ROOT / "e2e/drivers/protocols/run.sh")], cwd=ROOT,
            env={**os.environ, "E2E_BASE": f"http://127.0.0.1:{server.server_port}",
                 "E2E_OUT": str(tmp_path), "E2E_API_KEY": "protocol-control-key"},
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    scenarios = [json.loads(line) for line in (tmp_path / "scenarios.jsonl").read_text().splitlines()]
    protocols = [json.loads(line) for line in (tmp_path / "protocols.jsonl").read_text().splitlines()]
    assert len(scenarios) == 1 and len(protocols) == 3
    return scenarios[0], protocols, requests


def test_numeric_published_collection_executes_same_filter_across_three_protocols(tmp_path):
    scenario, protocols, requests = exercise(tmp_path, [{"id": "maui-zoning-lookalike"}, {"id": "2"}])
    assert scenario["status"] == "pass"
    assert {row["status"] for row in protocols} == {"pass"}
    by_path = dict(requests)
    assert by_path["/rest/services/maui-zoning/FeatureServer/2/query"]["where"] == ["zone_code='030'"]
    assert by_path["/odata/Layers(2)/Features"]["$filter"] == ["zone_code eq '030'"]
    assert by_path["/ogc/features/collections/2/items"]["filter"] == ["zone_code='030'"]
    assert by_path["/ogc/features/collections/2/items"]["filter-lang"] == ["cql2-text"]


@pytest.mark.parametrize("collections", [[], [{"id": "maui-zoning"}], [{"id": "2"}, {"id": "2"}]])
def test_missing_wrong_or_duplicate_collection_never_passes(tmp_path, collections):
    scenario, protocols, requests = exercise(tmp_path, collections)
    assert scenario["status"] == "blocked"
    assert protocols[-1]["status"] == "blocked"
    assert not any(path.endswith("/items") for path, _ in requests)


def test_actual_ogc_count_disagreement_fails(tmp_path):
    scenario, protocols, _ = exercise(tmp_path, [{"id": "2"}], ogc_count=3)
    assert scenario["status"] == "fail"
    assert protocols[-1]["status"] == "fail"


@pytest.mark.parametrize("options", [
    {"catalog_status": 503}, {"ogc_status": 503}, {"geo_status": 503}, {"odata_status": 503},
])
def test_error_status_with_plausible_success_body_cannot_pass(tmp_path, options):
    scenario, _, _ = exercise(tmp_path, [{"id": "2"}], **options)
    assert scenario["status"] == "blocked"
