"""Concurrent customer probes. No result is evidence without independent readback.

The caller supplies a dedicated managed FeatureServer layer and customer credential.
This module is not itself an authorization to enable a priced scenario.
"""
from __future__ import annotations

import json
import math
import hashlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable

from local_http import open_local, require_layer_path, require_loopback


class ProbeFailure(ValueError):
    def __init__(self, message: str, receipt: dict):
        super().__init__(message)
        self.receipt = receipt


@dataclass
class ProbeTraffic:
    base_url: str
    layer_path: str
    customer_key: str
    note_field: str
    geometry: dict
    write_attributes: dict = field(default_factory=dict)
    interval: float = 0.1
    writer_interval: float = 2.0
    timeout: float = 3.0
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _threads: list[threading.Thread] = field(default_factory=list, init=False)
    _workers: list[threading.Thread] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _samples: list[dict] = field(default_factory=list, init=False)
    _started: float | None = field(default=None, init=False)
    _ended: float | None = field(default=None, init=False)

    def _request(self, path: str, params: dict, method: str = "GET") -> tuple[dict, dict]:
        require_loopback(self.base_url)
        require_layer_path(self.layer_path)
        encoded = urllib.parse.urlencode(params).encode()
        url = self.base_url.rstrip("/") + self.layer_path + path
        request = urllib.request.Request(
            url + ("?" + encoded.decode() if method == "GET" else ""),
            data=encoded if method == "POST" else None, method=method,
            headers={"X-API-Key": self.customer_key, "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            response = open_local(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(2_000_001)
            status = response.status
        receipt = {"method": method, "path": self.layer_path + path, "status": status,
                   "request_sha256": hashlib.sha256(encoded).hexdigest(),
                   "response_sha256": hashlib.sha256(raw).hexdigest()}
        if status < 200 or status >= 300 or len(raw) > 2_000_000:
            raise ProbeFailure("HTTP request failed", receipt)
        try:
            body = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ProbeFailure("invalid FeatureServer JSON", receipt) from exc
        if not isinstance(body, dict) or body.get("error"):
            raise ProbeFailure("invalid or failed FeatureServer response", receipt)
        return body, receipt

    def _read(self) -> dict:
        body, receipt = self._request("/query", {"f": "json", "where": "1=1", "returnCountOnly": "true"})
        if type(body.get("count")) is not int:
            raise ProbeFailure("read response has no count", receipt)
        return {"count": body["count"], "http": receipt}

    def _write(self) -> dict:
        marker = "ops-" + uuid.uuid4().hex
        feature = {"attributes": {**self.write_attributes, self.note_field: marker}, "geometry": self.geometry}
        body, receipt = self._request("/applyEdits", {
            "f": "json", "adds": json.dumps([feature], separators=(",", ":")),
            "rollbackOnFailure": "true",
        }, method="POST")
        if body.get("success") is False:
            raise ProbeFailure("write transaction failed", receipt)
        results = body.get("addResults")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise ProbeFailure("write response lacks a single result", receipt)
        result = results[0]
        object_id = result.get("objectId")
        if result.get("success") is not True or type(object_id) is not int or object_id < 0:
            raise ProbeFailure("write was not acknowledged with an object id", receipt)
        return {"object_id": object_id, "marker": marker, "http": receipt}

    def _loop(self, kind: str, period: float, operation: Callable[[], dict]) -> None:
        deadline = time.monotonic()
        slots = threading.BoundedSemaphore(max(2, math.ceil(self.timeout / period) + 1))

        def sample_request() -> None:
            start = time.time()
            monotonic_start = time.monotonic()
            try:
                result = operation()
                sample = {"kind": kind, "ok": True, **result}
            except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
                sample = {"kind": kind, "ok": False, "error_type": type(exc).__name__}
                if isinstance(exc, ProbeFailure):
                    sample["http"] = exc.receipt
                if isinstance(exc, urllib.error.HTTPError):
                    sample["http_status"] = exc.code
            finally:
                slots.release()
            sample.update({"started_at": start, "finished_at": time.time(),
                           "started_monotonic": monotonic_start, "finished_monotonic": time.monotonic()})
            with self._lock:
                self._samples.append(sample)

        while not self._stop.is_set():
            if slots.acquire(blocking=False):
                thread = threading.Thread(target=sample_request, daemon=True)
                with self._lock:
                    self._workers.append(thread)
                thread.start()
            deadline += period
            # Do not fabricate attempts when the scheduler falls behind.
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
        stop_requested = time.monotonic()
        self._stop.set()
        for thread in self._threads:
            thread.join(self.timeout + 1)
        with self._lock:
            workers = list(self._workers)
        deadline = time.monotonic() + self.timeout + 1
        for thread in workers:
            thread.join(max(0.0, deadline - time.monotonic()))
        self._ended = time.monotonic()
        with self._lock:
            samples = list(self._samples)
        return {"started_monotonic": self._started, "ended_monotonic": self._ended,
                "stop_requested_monotonic": stop_requested,
                "complete": all(not thread.is_alive() for thread in self._threads + workers),
                "interval": self.interval, "writer_interval": self.writer_interval,
                "samples": samples}


def assess_traffic(record: dict, readback: dict, *, max_readback_delay_s: float = 10.0) -> dict:
    """Bound downtime by gaps between successful probes; compare every acknowledged write.

    `readback` must be a fresh controller-credential read of all committed IDs and
    marker values. Missing/duplicate observations or incomplete sampling fail closed.
    """
    samples = record.get("samples")
    start, end = record.get("started_monotonic"), record.get("ended_monotonic")
    stop_requested = record.get("stop_requested_monotonic")
    if (record.get("complete") is not True or not isinstance(samples, list) or
        not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start or
        not isinstance(stop_requested, (int, float)) or not start < stop_requested <= end or
        not isinstance(readback, dict)):
        raise ValueError("incomplete traffic or readback")
    gaps = {}
    intervals = {"read": record.get("interval"), "write": record.get("interval"),
                 "writer": record.get("writer_interval")}
    if (any(not isinstance(value, (int, float)) or value <= 0 for value in intervals.values()) or
        intervals["read"] > 0.1 or intervals["write"] > 0.1 or intervals["writer"] > 2.0):
        raise ValueError("missing or insufficient probe cadence")
    for kind in ("read", "write", "writer"):
        rows = [sample for sample in samples if sample.get("kind") == kind]
        if not rows or any(type(row.get("ok")) is not bool or
                           not isinstance(row.get("started_monotonic"), (int, float)) or
                           not isinstance(row.get("finished_monotonic"), (int, float)) or
                           not start <= row["started_monotonic"] <= row["finished_monotonic"] <= end
                           for row in rows):
            raise ValueError("missing or invalid samples")
        starts = sorted(row["started_monotonic"] for row in rows)
        if (starts[0] - start > intervals[kind] * 2.5 or stop_requested - starts[-1] > intervals[kind] * 2.5 or
            any(b - a > intervals[kind] * 2.5 for a, b in zip(starts, starts[1:]))):
            raise ValueError("probe cadence was not maintained")
        successes = sorted((row for row in rows if row["ok"]), key=lambda row: row["started_monotonic"])
        if len(successes) < 2:
            raise ValueError("insufficient successful samples")
        # A response may finish after the server processed it. Use the earlier
        # request start and later response finish to bound a possible outage.
        gaps[kind] = max([successes[0]["finished_monotonic"] - start,
                          end - successes[-1]["started_monotonic"],
                          *(b["finished_monotonic"] - a["started_monotonic"]
                            for a, b in zip(successes, successes[1:]))])
    writes = [row for row in samples if row.get("kind") in ("write", "writer") and row.get("ok")]
    ids = [row.get("object_id") for row in writes]
    if (any(type(oid) is not int or oid < 0 for oid in ids) or len(ids) != len(set(ids)) or
        any(not isinstance(row.get("marker"), str) or not row["marker"] for row in writes)):
        raise ValueError("invalid committed-write evidence")
    import base64
    import hashlib
    entries = readback.get("responses")
    began = readback.get("started_monotonic")
    if (not isinstance(began, (int, float)) or not end <= began <= end + max_readback_delay_s or
        not isinstance(entries, list) or len(entries) != len(ids)):
        raise ValueError("missing or late controller readback")
    base = readback.get("base_url")
    layer = readback.get("layer_path")
    if not isinstance(base, str) or not isinstance(layer, str) or not layer.startswith("/"):
        raise ValueError("readback origin missing")
    expected_prefix = base.rstrip("/") + layer + "/query"
    observed = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("object_id") not in ids or entry["object_id"] in observed:
            raise ValueError("invalid readback identity")
        observed_at = entry.get("observed_monotonic")
        if not isinstance(observed_at, (int, float)) or not began <= observed_at <= end + max_readback_delay_s:
            raise ValueError("late controller readback")
        url = entry.get("url")
        parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if (entry.get("method") != "GET" or parsed is None or
            urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) != expected_prefix or
            urllib.parse.parse_qs(parsed.query).get("objectIds") != [str(entry["object_id"])]):
            raise ValueError("readback request does not match trial layer")
        try:
            raw = base64.b64decode(entry["body_b64"], validate=True)
            body = json.loads(raw)
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("invalid readback response") from exc
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256") or entry.get("status") != 200:
            raise ValueError("readback response failed integrity check")
        if not isinstance(body, dict) or body.get("error") or not isinstance(body.get("features"), list) or len(body["features"]) > 1:
            raise ValueError("invalid readback response")
        rows = body["features"]
        if not rows:
            observed[entry["object_id"]] = None
            continue
        attrs = rows[0].get("attributes") if isinstance(rows[0], dict) else None
        if (not isinstance(attrs, dict) or attrs.get(readback.get("object_id_field")) != entry["object_id"] or
            not isinstance(attrs.get(readback.get("marker_field")), str)):
            raise ValueError("readback attributes do not match")
        observed[entry["object_id"]] = attrs[readback["marker_field"]]
    if set(observed) != set(ids):
        raise ValueError("missing committed-write readback")
    lost = [row["object_id"] for row in writes if observed[row["object_id"]] != row["marker"]]
    return {"downtime_upper_bound_s": max(gaps["read"], gaps["write"]),
            "probe_gaps_s": gaps, "committed_writes": len(writes),
            "lost_writes": lost, "rpo_zero": not lost}


def readback_writes(base_url: str, layer_path: str, controller_key: str,
                    object_id_field: str, marker_field: str, ids: list[int], *, timeout: float = 3.0) -> dict:
    """Read acknowledged IDs from the live API using the controller principal.

    An empty feature result is a missing write. A malformed or failed response is
    missing evidence and raises; it must never be interpreted as zero lost writes.
    """
    import base64
    import hashlib

    require_loopback(base_url)
    require_layer_path(layer_path)
    if (not controller_key or not object_id_field or not marker_field or not ids or
        any(type(oid) is not int or oid < 0 for oid in ids) or len(set(ids)) != len(ids)):
        raise ValueError("invalid readback request")
    evidence = []
    started_monotonic = time.monotonic()
    for oid in ids:
        params = urllib.parse.urlencode({"f": "json", "objectIds": oid,
                                        "outFields": f"{object_id_field},{marker_field}",
                                        "returnGeometry": "false"})
        url = base_url.rstrip("/") + layer_path + "/query?" + params
        req = urllib.request.Request(url, headers={"X-API-Key": controller_key})
        with open_local(req, timeout=timeout) as response:
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
        evidence.append({"object_id": oid, "method": "GET", "url": url, "status": status,
                         "sha256": hashlib.sha256(raw).hexdigest(),
                         "body_b64": base64.b64encode(raw).decode("ascii"), "observed_at": time.time(),
                         "observed_monotonic": time.monotonic()})
    return {"started_monotonic": started_monotonic, "base_url": base_url.rstrip("/"),
            "layer_path": layer_path, "object_id_field": object_id_field,
            "marker_field": marker_field, "responses": evidence}
