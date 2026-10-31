"""Concurrent customer probes. No result is evidence without independent readback.

The caller supplies a dedicated managed FeatureServer layer and customer credential.
This module is not itself an authorization to enable a priced scenario.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ProbeTraffic:
    base_url: str
    layer_path: str
    customer_key: str
    note_field: str
    geometry: dict
    interval: float = 0.1
    writer_interval: float = 2.0
    timeout: float = 3.0
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _threads: list[threading.Thread] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _samples: list[dict] = field(default_factory=list, init=False)
    _started: float | None = field(default=None, init=False)
    _ended: float | None = field(default=None, init=False)

    def _request(self, path: str, params: dict, method: str = "GET") -> dict:
        encoded = urllib.parse.urlencode(params).encode()
        url = self.base_url.rstrip("/") + self.layer_path + path
        request = urllib.request.Request(
            url + ("?" + encoded.decode() if method == "GET" else ""),
            data=encoded if method == "POST" else None, method=method,
            headers={"X-API-Key": self.customer_key, "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.load(response)
        if not isinstance(body, dict) or body.get("error"):
            raise ValueError("invalid or failed FeatureServer response")
        return body

    def _read(self) -> dict:
        body = self._request("/query", {"f": "json", "where": "1=1", "returnCountOnly": "true"})
        if type(body.get("count")) is not int:
            raise ValueError("read response has no count")
        return {"count": body["count"]}

    def _write(self) -> dict:
        marker = "ops-" + uuid.uuid4().hex
        feature = {"attributes": {self.note_field: marker}, "geometry": self.geometry}
        body = self._request("/applyEdits", {
            "f": "json", "adds": json.dumps([feature], separators=(",", ":")),
            "rollbackOnFailure": "true",
        }, method="POST")
        results = body.get("addResults")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise ValueError("write response lacks a single result")
        result = results[0]
        object_id = result.get("objectId")
        if result.get("success") is not True or type(object_id) is not int or object_id < 0:
            raise ValueError("write was not acknowledged with an object id")
        return {"object_id": object_id, "marker": marker}

    def _loop(self, kind: str, period: float, operation: Callable[[], dict]) -> None:
        deadline = time.monotonic()
        while not self._stop.is_set():
            start = time.time()
            monotonic_start = time.monotonic()
            try:
                result = operation()
                sample = {"kind": kind, "ok": True, **result}
            except (OSError, ValueError, TypeError, urllib.error.URLError):
                sample = {"kind": kind, "ok": False}
            sample.update({"started_at": start, "finished_at": time.time(),
                           "started_monotonic": monotonic_start, "finished_monotonic": time.monotonic()})
            with self._lock:
                self._samples.append(sample)
            deadline += period
            # Never fabricate samples to fill missed ticks after a slow request.
            if deadline < time.monotonic():
                deadline = time.monotonic()
            self._stop.wait(max(0.0, deadline - time.monotonic()))

    def start(self) -> None:
        if self._started is not None or self.interval <= 0 or self.writer_interval <= 0:
            raise ValueError("traffic can be started once with positive intervals")
        self._started = time.monotonic()
        for kind, period, operation in (("read", self.interval, self._read),
                                        ("write", self.interval, self._write),
                                        ("writer", self.writer_interval, self._write)):
            thread = threading.Thread(target=self._loop, args=(kind, period, operation), daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self) -> dict:
        if self._started is None or self._ended is not None:
            raise ValueError("traffic was not started or was already stopped")
        self._stop.set()
        for thread in self._threads:
            thread.join(self.timeout + 1)
        self._ended = time.monotonic()
        with self._lock:
            samples = list(self._samples)
        return {"started_monotonic": self._started, "ended_monotonic": self._ended,
                "complete": all(not thread.is_alive() for thread in self._threads),
                "samples": samples}


def assess_traffic(record: dict, observed: dict[int, str]) -> dict:
    """Bound downtime by gaps between successful probes; compare every acknowledged write.

    `observed` must be a fresh controller-credential read of all committed IDs and
    marker values. Missing/duplicate observations or incomplete sampling fail closed.
    """
    samples = record.get("samples")
    start, end = record.get("started_monotonic"), record.get("ended_monotonic")
    if (record.get("complete") is not True or not isinstance(samples, list) or
        not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start or
        not isinstance(observed, dict)):
        raise ValueError("incomplete traffic or readback")
    gaps = {}
    for kind in ("read", "write", "writer"):
        rows = [sample for sample in samples if sample.get("kind") == kind]
        if not rows or any(type(row.get("ok")) is not bool or
                           not isinstance(row.get("started_monotonic"), (int, float)) or
                           not isinstance(row.get("finished_monotonic"), (int, float)) or
                           not start <= row["started_monotonic"] <= row["finished_monotonic"] <= end
                           for row in rows):
            raise ValueError("missing or invalid samples")
        successes = sorted(row["finished_monotonic"] for row in rows if row["ok"])
        if len(successes) < 2:
            raise ValueError("insufficient successful samples")
        points = [start, *successes, end]
        gaps[kind] = max(b - a for a, b in zip(points, points[1:]))
    writes = [row for row in samples if row.get("kind") in ("write", "writer") and row.get("ok")]
    ids = [row.get("object_id") for row in writes]
    if (any(type(oid) is not int or oid < 0 for oid in ids) or len(ids) != len(set(ids)) or
        any(not isinstance(row.get("marker"), str) or not row["marker"] for row in writes)):
        raise ValueError("invalid committed-write evidence")
    lost = [row["object_id"] for row in writes if observed.get(row["object_id"]) != row["marker"]]
    return {"downtime_upper_bound_s": max(gaps["read"], gaps["write"]),
            "probe_gaps_s": gaps, "committed_writes": len(writes),
            "lost_writes": lost, "rpo_zero": not lost}


def readback_writes(base_url: str, layer_path: str, controller_key: str,
                    object_id_field: str, marker_field: str, ids: list[int], *, timeout: float = 3.0) -> tuple[dict[int, str], list[dict]]:
    """Read acknowledged IDs from the live API using the controller principal.

    An empty feature result is a missing write. A malformed or failed response is
    missing evidence and raises; it must never be interpreted as zero lost writes.
    """
    import base64
    import hashlib

    if (not controller_key or not object_id_field or not marker_field or not ids or
        any(type(oid) is not int or oid < 0 for oid in ids) or len(set(ids)) != len(ids)):
        raise ValueError("invalid readback request")
    observed: dict[int, str] = {}
    evidence = []
    for oid in ids:
        params = urllib.parse.urlencode({"f": "json", "objectIds": oid,
                                        "outFields": f"{object_id_field},{marker_field}",
                                        "returnGeometry": "false"})
        url = base_url.rstrip("/") + layer_path + "/query?" + params
        req = urllib.request.Request(url, headers={"X-API-Key": controller_key})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            status = response.status
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("error") or not isinstance(body.get("features"), list):
            raise ValueError("invalid write readback")
        rows = body["features"]
        if len(rows) > 1:
            raise ValueError("ambiguous write readback")
        if rows:
            attrs = rows[0].get("attributes") if isinstance(rows[0], dict) else None
            if not isinstance(attrs, dict) or attrs.get(object_id_field) != oid or not isinstance(attrs.get(marker_field), str):
                raise ValueError("write readback identity mismatch")
            observed[oid] = attrs[marker_field]
        evidence.append({"method": "GET", "url": url, "status": status,
                         "sha256": hashlib.sha256(raw).hexdigest(),
                         "body_b64": base64.b64encode(raw).decode("ascii"), "observed_at": time.time()})
    return observed, evidence
