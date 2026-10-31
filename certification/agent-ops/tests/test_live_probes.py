"""Exercise actual HTTP probes with a local fake public FeatureServer."""
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from live_probes import ProbeTraffic, assess_traffic, readback_writes


class FeatureHandler(BaseHTTPRequestHandler):
    features = {}
    lock = threading.Lock()
    fail_reads = False

    def log_message(self, *_args):
        pass

    def do_GET(self):
        with self.lock:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if self.fail_reads:
                body = {"error": {"code": 500}}
            elif "objectIds" in query:
                oid = int(query["objectIds"][0])
                body = {"features": ([{"attributes": {"objectid": oid, "note": self.features[oid]}}]
                                     if oid in self.features else [])}
            else:
                body = {"count": len(self.features)}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        params = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        feature = json.loads(params["adds"][0])[0]
        with self.lock:
            object_id = len(self.features) + 1
            self.features[object_id] = feature["attributes"]["note"]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"addResults": [{"success": True, "objectId": object_id}]}).encode())


def test_real_http_samples_and_missing_committed_write():
    FeatureHandler.features = {}
    FeatureHandler.fail_reads = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FeatureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    traffic = ProbeTraffic(f"http://127.0.0.1:{server.server_port}", "/layer", "customer", "note",
                           {"x": 1, "y": 2}, interval=0.03, writer_interval=0.04)
    try:
        traffic.start()
        time.sleep(0.3)
        record = traffic.stop()
        ids = [row["object_id"] for row in record["samples"]
               if row["kind"] in ("write", "writer") and row["ok"]]
        observed, evidence = readback_writes(f"http://127.0.0.1:{server.server_port}", "/layer",
                                             "controller", "objectid", "note", ids)
        assert len(evidence) == len(ids)
        assert all(len(item["sha256"]) == 64 and "controller" not in str(item) for item in evidence)
        result = assess_traffic(record, observed)
        assert result["rpo_zero"] is True
        assert result["committed_writes"] >= 4
        assert {row["kind"] for row in record["samples"]} == {"read", "write", "writer"}
        assert all(row["finished_at"] >= row["started_at"] for row in record["samples"])
        observed = dict(FeatureHandler.features)
        lost = next(iter(observed))
        del observed[lost]
        assert lost in assess_traffic(record, observed)["lost_writes"]
        record["complete"] = False
        with pytest.raises(ValueError, match="incomplete"):
            assess_traffic(record, observed)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_missing_channel_and_duplicate_ack_fail_closed():
    def sample(kind, timestamp, **extra):
        return {"kind": kind, "ok": True, "started_monotonic": timestamp,
                "finished_monotonic": timestamp, **extra}
    record = {"complete": True, "started_monotonic": 0.0, "ended_monotonic": 1.0,
              "samples": [sample("read", .2), sample("read", .8)]}
    with pytest.raises(ValueError, match="samples"):
        assess_traffic(record, {})
    for kind in ("write", "writer"):
        record["samples"].extend([sample(kind, .2, object_id=1, marker="a"),
                                  sample(kind, .8, object_id=1, marker="a")])
    with pytest.raises(ValueError, match="committed-write"):
        assess_traffic(record, {1: "a"})
