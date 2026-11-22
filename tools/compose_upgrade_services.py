"""Prove published service metadata and feature content survive an upgrade."""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from compose_upgrade_credentials import Api, ProbeError, preservation_evidence, run_probe, selected_fields
from compose_upgrade_maps import snapshot_hash, snapshot_hash_fields


LAYER_FIELDS = ("id", "name", "type", "geometryType", "spatialReference", "fields", "objectIdField")
CHECKS = ("servicePublished", "layerUnchanged", "featuresUnchanged")
HASH_FIELDS = ("serviceSnapshotSha256", "featuresSnapshotSha256")
EXPECTED_COUNT = 2


class FeatureApi(Api):
    def __init__(self, base: str, key: str):
        super().__init__(base, key)
        self.base = base.rstrip("/") + "/rest/services"

    def unwrap(self, document):
        if not isinstance(document, dict) or "error" in document:
            raise ProbeError("published service request returned an error")
        return document


def source_identity(value: dict) -> dict:
    service, layer = value.get("service"), value.get("layerId")
    if (not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", service)
            or not isinstance(layer, int) or isinstance(layer, bool) or layer <= 0):
        raise ProbeError("seeded service identity is invalid")
    return {"service": service, "layerId": layer}


def publication(api: Api, source: dict) -> dict:
    root = f"/{quote(source['service'], safe='')}/FeatureServer"
    service = api.request(root + "?f=json")
    layers = service.get("layers")
    if service.get("serviceName") != source["service"] or not isinstance(layers, list):
        raise ProbeError("seeded service is no longer published")
    matches = [layer for layer in layers if isinstance(layer, dict) and layer.get("id") == source["layerId"]]
    if len(matches) != 1:
        raise ProbeError("seeded layer is missing or duplicated in the published service")
    layer = selected_fields(api.request(f"{root}/{source['layerId']}?f=json"), LAYER_FIELDS)
    if (layer["id"] != source["layerId"] or layer["name"] != matches[0].get("name")
            or not isinstance(layer["objectIdField"], str) or not layer["objectIdField"]):
        raise ProbeError("published layer does not match its catalog entry")
    return {"serviceName": service["serviceName"], "layer": layer}


def features(api: Api, source: dict, object_id: str) -> list:
    root = f"/{quote(source['service'], safe='')}/FeatureServer/{source['layerId']}/query?"
    count = api.request(root + urlencode({"f": "json", "where": "1=1", "returnCountOnly": "true"})).get("count")
    if type(count) is not int or count != EXPECTED_COUNT:
        raise ProbeError("published feature count does not match the populated seed")
    query = api.request(root + urlencode({"f": "json", "where": "1=1", "outFields": "*",
                                         "returnGeometry": "true", "resultRecordCount": EXPECTED_COUNT + 1}))
    rows = query.get("features")
    if (query.get("exceededTransferLimit") is not False or not isinstance(rows, list) or len(rows) != count
            or any(not isinstance(row, dict) or not isinstance(row.get("attributes"), dict)
                   or not isinstance(row.get("geometry"), dict) for row in rows)):
        raise ProbeError("published feature query is incomplete")
    identifiers = [row["attributes"].get(object_id) for row in rows]
    if any(type(value) is not int for value in identifiers) or len(set(identifiers)) != count:
        raise ProbeError("published feature identifiers are missing or duplicated")
    return sorted(rows, key=snapshot_hash)


def seed(api: Api, manifest: dict) -> dict:
    source = source_identity(manifest["slice1"]["e2e_src_fs"])
    metadata = publication(api, source)
    return {"source": source, "publication": metadata,
            "features": features(api, source, metadata["layer"]["objectIdField"])}


def verify(api: Api, state: dict) -> dict:
    source = source_identity(state["source"])
    metadata = publication(api, source)
    after = {"publication": metadata, "features": features(api, source, metadata["layer"]["objectIdField"])}
    for field in ("publication", "features"):
        if after[field] != state[field]:
            raise ProbeError(f"published service {field} changed across the upgrade")
    return {"schema": 1, "status": "pass", **source, "featureCount": EXPECTED_COUNT,
            "checks": dict.fromkeys(CHECKS, True),
            "serviceSnapshotSha256": snapshot_hash(after["publication"]),
            "featuresSnapshotSha256": snapshot_hash(after["features"])}


def service_fields(value: dict) -> dict:
    if type(value.get("featureCount")) is not int or value["featureCount"] != EXPECTED_COUNT:
        raise ProbeError("published feature count does not match the populated seed")
    return {**source_identity(value), "featureCount": EXPECTED_COUNT,
            **snapshot_hash_fields(value, HASH_FIELDS)}


def service_evidence(pair: dict, observed: dict) -> dict:
    return preservation_evidence(pair, observed, {"key": "services", "checks": CHECKS, "fields": service_fields})


def main() -> int:
    return run_probe(FeatureApi, seed, verify)


if __name__ == "__main__":
    raise SystemExit(main())
