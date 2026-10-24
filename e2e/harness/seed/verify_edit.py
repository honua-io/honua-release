"""Read back a managed scratch edit, then restore and verify the original note."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable
import urllib.parse
import urllib.request
import uuid


def verify(publication: dict[str, Any], request: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    layer = publication.get("data", publication)
    if layer.get("storageMode") != "managed" or layer.get("serviceName") != "maui-inspections":
        raise ValueError("Scratch publication must report managed storage for maui-inspections")
    layer_id = layer.get("layerId")
    if isinstance(layer_id, bool) or not isinstance(layer_id, int) or layer_id < 1:
        raise ValueError("Scratch publication has no positive layer id")
    route = f"/rest/services/maui-inspections/FeatureServer/{layer_id}"
    metadata = request(route, {"f": "json"})
    capabilities = {value.strip() for value in metadata.get("capabilities", "").split(",")}
    if not {"Query", "Create", "Update", "Delete"}.issubset(capabilities):
        raise ValueError("Live scratch metadata does not declare the expected edit capabilities")
    object_id_field = metadata.get("objectIdField")
    if not isinstance(object_id_field, str) or not object_id_field:
        raise ValueError("Live scratch metadata has no object-id field")
    query = {"f": "json", "where": "1=1", "outFields": "*", "returnGeometry": "true", "resultRecordCount": 1}
    features = request(route + "/query", query).get("features", [])
    if len(features) != 1:
        raise ValueError("Scratch query did not return a feature to update")
    attributes = features[0]["attributes"]
    geometry = features[0].get("geometry")
    if not isinstance(geometry, dict) or not geometry:
        raise ValueError("Scratch query did not return the seeded feature geometry")
    object_id = attributes[object_id_field]
    original_note = attributes["note"]

    def update_note(note: str | None) -> list[dict[str, Any]]:
        result = request(route + "/applyEdits", {
            "f": "json", "updates": json.dumps([{"attributes": {object_id_field: object_id, "note": note}}]),
            "rollbackOnFailure": "true",
        }, method="POST")
        updates = result.get("updateResults", [])
        if len(updates) != 1 or updates[0].get("success") is not True or updates[0].get("objectId") != object_id:
            raise ValueError(f"Scratch update did not report one successful update: {result}")
        return updates

    def readback(expected: dict[str, Any]) -> None:
        rows = request(route + "/query", {"f": "json", "objectIds": object_id,
                       "outFields": "*", "returnGeometry": "true"}).get("features", [])
        if len(rows) != 1 or rows[0].get("attributes") != expected:
            raise ValueError("Scratch update did not read back the expected attributes")
        if rows[0].get("geometry") != geometry:
            raise ValueError("Attribute-only scratch update changed the geometry")

    marker = "seed-edit-proof-" + uuid.uuid4().hex
    try:
        updates = update_note(marker)
        readback({**attributes, "note": marker})
    finally:
        update_note(original_note)
        readback(attributes)
    return {"status": "pass", "service": "maui-inspections", "layerId": layer_id,
            "storageMode": "managed", "objectIdField": object_id_field, "objectId": object_id,
            "capabilities": sorted(capabilities), "updateResults": updates,
            "mutationReadBack": True, "originalNoteRestored": True, "geometryReadBackUnchanged": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = os.environ.get("E2E_BASE", "http://localhost:8080").rstrip("/")
    key = os.environ.get("E2E_API_KEY", "honua-console-dev-key")

    def request(path: str, values: dict[str, Any], method: str = "GET") -> dict[str, Any]:
        encoded = urllib.parse.urlencode(values).encode()
        url = base + path + ("?" + encoded.decode() if method == "GET" else "")
        req = urllib.request.Request(url, data=encoded if method == "POST" else None, method=method,
                                     headers={"X-API-Key": key, "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError(f"Scratch read-back API error at {path}: {result}")
        return result

    receipt = verify(json.loads(args.publication.read_text(encoding="utf-8")), request)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
