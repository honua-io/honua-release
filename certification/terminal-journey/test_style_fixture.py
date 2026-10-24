"""Actual HTTP fixture preparation; no model journey qualification is implied."""
import copy
import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import local_fixture
from transport import ExecutionError, Transport

TARGET = json.loads((Path(__file__).parent / "targets/local-docker.json").read_text())
STYLE = TARGET["execution"]["style"]
STYLE_PATH = "/ogc/styles/" + TARGET["execution"]["styleId"]


@contextmanager
def candidate(tmp_path, existing=None, corrupt_created=False, read_status=200):
    state = {"style": copy.deepcopy(existing), "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state["calls"].append(("GET", self.path))
            assert self.path == STYLE_PATH
            assert self.headers["Accept"] == "application/vnd.mapbox.style+json"
            assert self.headers["X-API-Key"] == "unit-operator"
            self.send_response(read_status if state["style"] is not None else 404)
            self.end_headers()
            self.wfile.write(json.dumps(state["style"]).encode())

        def do_POST(self):
            state["calls"].append(("POST", self.path))
            assert self.path == "/ogc/styles"
            assert self.headers["X-Style-Id"] == TARGET["execution"]["styleId"]
            assert self.headers["X-API-Key"] == "unit-operator"
            state["style"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if corrupt_created:
                state["style"]["layers"][0]["paint"]["circle-color"] = "#000000"
            self.send_response(201)
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Transport(f"http://127.0.0.1:{server.server_port}", None, None, tmp_path,
                        {"operator": "unit-operator"}), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_missing_preset_is_created_and_read_back_without_applying_it(tmp_path):
    with candidate(tmp_path) as (transport, state):
        local_fixture.prepare_style_catalog(TARGET, transport)
        assert state["style"] == STYLE
        assert state["calls"] == [("GET", STYLE_PATH), ("POST", "/ogc/styles"), ("GET", STYLE_PATH)]
        local_fixture.prepare_style_catalog(TARGET, transport)
        assert state["calls"][-1] == ("GET", STYLE_PATH)
        assert len(state["calls"]) == 4


def test_existing_different_preset_is_refused_without_overwrite(tmp_path):
    changed = copy.deepcopy(STYLE)
    changed["layers"][0]["paint"]["circle-color"] = "#000000"
    with candidate(tmp_path, existing=changed) as (transport, state):
        with pytest.raises(ExecutionError, match="differs from the authored fixture"):
            local_fixture.prepare_style_catalog(TARGET, transport)
        assert state["calls"] == [("GET", STYLE_PATH)]
        assert state["style"] == changed


def test_created_preset_requires_independent_exact_readback(tmp_path):
    with candidate(tmp_path, corrupt_created=True) as (transport, _):
        with pytest.raises(ExecutionError, match="differs from the authored fixture"):
            local_fixture.prepare_style_catalog(TARGET, transport)


def test_unavailable_read_cannot_be_treated_as_missing(tmp_path):
    with candidate(tmp_path, existing=STYLE, read_status=403) as (transport, state):
        with pytest.raises(ExecutionError, match="HTTP 403"):
            local_fixture.prepare_style_catalog(TARGET, transport)
        assert state["calls"] == [("GET", STYLE_PATH)]
