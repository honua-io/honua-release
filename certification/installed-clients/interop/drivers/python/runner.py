#!/usr/bin/env python3
"""Python SDK runner for the interop scenarios.

Runs against the installed, manifest-pinned ``honua-sdk`` wheel only and calls its client
classes, never raw HTTP. Reads one JSON request per line on stdin and answers one JSON reply per
line on stdout. Client instances live for the whole run, so the identity scenario observes a
revocation on the very client instance that used the key. Replies carry an observation, or an
error's type and status only; messages go to stderr.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import traceback
from typing import Any, Callable

from honua_sdk import HonuaClient  # noqa: E402

BASE = json.loads(open(os.environ["SDKREG_PLAN"], encoding="utf-8").read())["baseUrl"]
ROOT = HonuaClient(BASE, api_key=os.environ["SDKREG_API_KEY"])
IDENTITY: dict[str, HonuaClient] = {}


class Unsupported(Exception):
    pass


def error_of(exc: BaseException) -> dict[str, Any]:
    status = getattr(exc, "error_code", None)
    if type(status) is not int:
        status = getattr(exc, "status_code", None)
    print(f"[python-runner] {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr, flush=True)
    return {"type": type(exc).__name__, "status": status if type(status) is int else None}


def rows_of(payload: dict[str, Any], fields: list[str]) -> list[dict[str, Any]]:
    oid = payload.get("objectIdFieldName")
    rows = []
    for feature in payload.get("features") or []:
        attributes = feature.get("attributes") or {}
        geometry = feature.get("geometry") or {}
        rows.append({"attributes": {field: attributes.get(field) for field in fields}, "x": geometry.get("x"),
                     "y": geometry.get("y"), "objectId": attributes.get(oid) if oid else None})
    return rows


def query_features(args: dict[str, Any]) -> dict[str, Any]:
    """GeoServicesFeatureServerClient.query: every row, its attributes, ordinates and object id."""
    payload = ROOT.feature_server(args["service"]).query(args["layerId"], where="1=1", out_fields="*")
    return {"features": rows_of(payload, args["fields"]), "objectIdField": payload.get("objectIdFieldName")}


def query_ids(args: dict[str, Any]) -> dict[str, Any]:
    payload = ROOT.feature_server(args["service"]).query(args["layerId"], where="1=1", return_geometry=False,
                                                         extra_params={"returnIdsOnly": "true"})
    return {"ids": payload.get("objectIds")}


def query_count(args: dict[str, Any]) -> dict[str, Any]:
    """The documented count API: HonuaClient.query(return_count_only=True)."""
    result = ROOT.query(args["service"], protocol="geoservices-feature-service", layer_id=args["layerId"], where="1=1",
                        return_count_only=True)
    return {"count": result.total_count}


def job_status(args: dict[str, Any]) -> dict[str, Any]:
    """HonuaGeoprocessing.job: read another client's job by its id until it is terminal."""
    gp = ROOT.geoprocessing()
    deadline = time.monotonic() + args["timeoutSeconds"]
    job = gp.job(args["jobId"])
    while not job.is_terminal and time.monotonic() < deadline:
        time.sleep(0.5)
        job = gp.job(args["jobId"])
    return {"status": job.status}


def job_result(args: dict[str, Any]) -> dict[str, Any]:
    """HonuaGeoprocessing.results: the job's FeatureLayer output, carried inline as a data: URI."""
    results = ROOT.geoprocessing().results(args["jobId"])
    layers = [item for item in (results.values() if isinstance(results, dict) else [])
              if isinstance(item, dict) and item.get("kind") == "FeatureLayer"]
    if len(layers) != 1:
        return {"geometry": None, "featureLayers": len(layers)}
    href = layers[0].get("href") or layers[0].get("value") or ""
    header, _, data = str(href).partition(",")
    if not header.startswith("data:") or ";base64" not in header:
        return {"geometry": None, "inline": False}
    return {"geometry": json.loads(base64.b64decode(data, validate=True))}


def identity_query(client: HonuaClient, args: dict[str, Any]) -> int:
    payload = client.feature_server(args["service"]).query(args["layerId"], where="1=1", out_fields="*")
    return len(payload.get("features") or [])


def identity_use(args: dict[str, Any]) -> dict[str, Any]:
    """HonuaClient(api_key=) with the key the admin CLI minted; the instance is kept for the revocation probe."""
    with open(args["secretFile"], encoding="utf-8") as handle:
        IDENTITY["client"] = HonuaClient(BASE, api_key=handle.read().strip())
    return {"count": identity_query(IDENTITY["client"], args)}


def identity_revoked(args: dict[str, Any]) -> dict[str, Any]:
    """Same client instance after revocation: count successes until the first refusal, then confirm it holds."""
    client, bound = IDENTITY["client"], args["observationSeconds"]
    successes, started = 0, time.time()
    while True:
        try:
            identity_query(client, args)
        except Exception as exc:  # noqa: BLE001 - the refusal is the observation
            refused = error_of(exc)
            after = round(time.time() - (args.get("revokedAt") or started), 3)
            confirmations = []
            for _ in range(args["confirmations"]):
                try:
                    identity_query(client, args)
                    confirmations.append(False)
                except Exception:  # noqa: BLE001
                    confirmations.append(True)
            return {"refused": True, "status": refused["status"], "succeededAfterRevocation": successes,
                    "refusedAfterSeconds": after, "confirmations": confirmations, "observationSeconds": bound}
        successes += 1
        if time.time() - started > bound:
            return {"refused": False, "succeededAfterRevocation": successes, "observationSeconds": bound}
        time.sleep(0.25)


OPS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "query-features": query_features, "query-ids": query_ids, "query-count": query_count,
    "job-status": job_status, "job-result": job_result,
    "identity-use": identity_use, "identity-revoked": identity_revoked,
}


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        reply: dict[str, Any] = {"id": request.get("id")}
        try:
            reply["observed"] = OPS[request["op"]](request.get("args") or {})
        except Unsupported as exc:
            reply["unsupported"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - every SDK failure is an observation
            traceback.print_exc(file=sys.stderr)
            reply["error"] = error_of(exc)
        print(json.dumps(reply, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
