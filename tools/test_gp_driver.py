"""S5 must reject HTTP errors and plausible but incorrect results."""
import json
import math
import os
from pathlib import Path
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[1]


def exercise(tmp_path, *, submit_code=201, poll_code=200, result_code=200,
             wrong_area=False, wrong_buffer=False, wrong_job=False, redis_off=False,
             clipped_corners=False, long_chord=False):
    ring = [[10 * math.cos(i * math.pi / 32), 10 * math.sin(i * math.pi / 32)] for i in range(64)]
    ring.append(ring[0])
    if clipped_corners:
        # Exact expected bounds AND area, but all vertices are off the circle.
        a = 10 - math.sqrt((400 - 100 * math.pi) / 2)
        ring = [[a, 10], [-a, 10], [-10, a], [-10, -a], [-a, -10],
                [a, -10], [10, -a], [10, a], [a, 10]]
    if long_chord:
        # Every vertex is on the circle, but this edge cuts too far inside it.
        del ring[1:5]
    if wrong_buffer:
        ring = [[x * 500, y * 500] for x, y in ring]
    outputs = {
        "geometry.area": {"outputScalar": {"value": {
            "measure": "area", "inputSrid": 3857, "value": 2 if wrong_area else 1,
        }, "mediaType": "application/json"}},
        "geometry.buffer": {"outputFeatureLayer": {"value": {
            "type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {},
        }, "mediaType": "application/geo+json"}},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["inputs"]["srid"] == 3857
            if redis_off:
                self.send(503, {"type": "https://honua.io/problems/capability-unavailable", "missingDependency": "redis"})
            else:
                self.send(submit_code, {"jobID": self.path.split("/")[-2], "status": "accepted"})

        def do_GET(self):
            if self.path == "/healthz/ready":
                self.send(200, {})
                return
            job = self.path.split("/")[4]
            if self.path.endswith("/results"):
                self.send(result_code, outputs[job])
            else:
                self.send(poll_code, {"jobID": "wrong" if wrong_job else job, "status": "successful"})

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                ["bash", str(ROOT / "e2e/drivers/gp/run.sh")], cwd=ROOT,
                env={**os.environ, "E2E_BASE": f"http://127.0.0.1:{server.server_port}",
                     "E2E_OUT": str(tmp_path), "E2E_REDIS": "off" if redis_off else "on"},
                capture_output=True, text=True, timeout=20,
            )
            assert result.returncode == 0, result.stderr
        finally:
            server.shutdown()
            thread.join(timeout=5)
    return json.loads((tmp_path / "scenarios.jsonl").read_text())


def test_correct_metric_results_pass(tmp_path):
    assert exercise(tmp_path)["status"] == "pass"


@pytest.mark.parametrize("options", [
    {"submit_code": 500}, {"poll_code": 500}, {"result_code": 500},
    {"wrong_area": True}, {"wrong_buffer": True}, {"wrong_job": True},
    {"clipped_corners": True}, {"long_chord": True},
])
def test_failure_cannot_be_reported_as_pass(tmp_path, options):
    assert exercise(tmp_path, **options)["status"] == "fail"


def test_redis_off_still_requires_typed_refusal(tmp_path):
    assert exercise(tmp_path, redis_off=True)["status"] == "pass"
