"""Independent controller reads of a live FeatureServer layer.

These observations are building blocks, not evidence that the trial fixture,
all ACLs, saved maps, schema or running images were observed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
import urllib.request


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def observe_layer(base_url: str, layer_path: str, controller_key: str, *, timeout: float = 10) -> dict:
    """Read metadata, enumerate IDs, and fetch each feature without trusting pagination."""
    if not controller_key or not layer_path.startswith("/"):
        raise ValueError("controller key and absolute layer path required")
    evidence = []

    def get(path: str, params: dict) -> dict:
        url = base_url.rstrip("/") + layer_path + path + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"X-API-Key": controller_key})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw, status = response.read(), response.status
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("error"):
            raise ValueError("controller API read failed")
        evidence.append({"method": "GET", "url": url, "status": status,
                         "sha256": hashlib.sha256(raw).hexdigest(),
                         "body_b64": base64.b64encode(raw).decode("ascii")})
        return document

    metadata = get("", {"f": "json"})
    object_id_field = metadata.get("objectIdField")
    fields = metadata.get("fields")
    if not isinstance(object_id_field, str) or not object_id_field or not isinstance(fields, list) or not fields:
        raise ValueError("layer metadata lacks fields or object id")
    ids_response = get("/query", {"f": "json", "where": "1=1", "returnIdsOnly": "true"})
    ids = ids_response.get("objectIds")
    if (not isinstance(ids, list) or any(type(item) is not int or item < 0 for item in ids)
        or len(set(ids)) != len(ids) or ids_response.get("exceededTransferLimit") is True):
        raise ValueError("incomplete or invalid feature id enumeration")
    features = []
    for object_id in sorted(ids):
        page = get("/query", {"f": "json", "objectIds": object_id, "outFields": "*", "returnGeometry": "true"})
        rows = page.get("features")
        if not isinstance(rows, list) or len(rows) != 1 or page.get("exceededTransferLimit") is True:
            raise ValueError("missing or ambiguous feature")
        row = rows[0]
        if (not isinstance(row, dict) or not isinstance(row.get("attributes"), dict)
            or row["attributes"].get(object_id_field) != object_id or "geometry" not in row):
            raise ValueError("feature identity or geometry missing")
        features.append({"id": object_id, "attributes": row["attributes"],
                         "geometry": row["geometry"],
                         "geometry_sha256": hashlib.sha256(_canonical(row["geometry"])).hexdigest()})
    return {"count": len(features), "object_id_field": object_id_field,
            "fields": fields, "features": features, "evidence": evidence}
