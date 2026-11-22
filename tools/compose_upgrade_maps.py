"""Prove a saved Studio map draft and immutable version survive an upgrade."""
from __future__ import annotations

from hashlib import sha256
import json
import re
from uuid import uuid4

from compose_upgrade_credentials import Api, ProbeError, identifier, preservation_evidence, run_probe, selected_fields


COMMON_FIELDS = ("itemId", "packageKey", "workspaceId", "ownerId", "envelope", "createdAt")
DRAFT_FIELDS = COMMON_FIELDS + ("draftId", "family", "generation", "updatedAt")
VERSION_FIELDS = COMMON_FIELDS + ("versionId", "versionNumber", "contentHash", "sourceDraftId", "changeNote")
CHECKS = ("durableStore", "draftUnchanged", "immutableVersionUnchanged")
HASH_FIELDS = ("draftSnapshotSha256", "versionSnapshotSha256")


class StudioApi(Api):
    def __init__(self, base: str, key: str):
        super().__init__(base, key)
        self.base = base.rstrip("/") + "/api/v1/studio"


def require_durable(api: Api):
    value = api.request("/package-families")
    if not isinstance(value, dict) or value.get("durable") is not True:
        raise ProbeError("Studio map store is not durable")


def map_envelope(manifest: dict) -> dict:
    source = manifest["slice1"]["e2e_src_fs"]
    layer_id = source["layerId"]
    if (not isinstance(layer_id, int) or isinstance(layer_id, bool) or layer_id <= 0
            or not isinstance(source.get("service"), str) or not source["service"]):
        raise ProbeError("seeded map source is invalid")
    return {"family": "map", "schemaVersion": "1.0", "format": "honua_map_package.v1",
            "bindings": [], "dependencies": [], "body": {
                "mapPackageId": f"upgrade-map-{uuid4().hex}", "format": "honua_map_package.v1",
                "status": "Draft", "createdAt": "2026-01-01T00:00:00Z", "title": "Upgrade preservation",
                "mapSpec": {"version": 8, "sources": {}, "layers": []},
                "view": {"center": [-157.8583, 21.3069], "zoom": 5},
                "layers": [], "widgets": [], "controls": [], "interactions": [],
                "sourceBindings": [{"sourceId": "seeded-features", "protocol": "geoservices_feature_service",
                                    "locator": {"serviceId": source["service"], "layerId": layer_id}}],
                "initialView": {"center": [-157.8583, 21.3069], "zoom": 5, "crs": "EPSG:4326"},
                "legend": [{"label": "Seeded features"}]}}


def snapshots(api: Api, ids: dict) -> dict:
    draft_id, item_id, version_id = (identifier(ids.get(field)) for field in ("draftId", "itemId", "versionId"))
    draft = selected_fields(api.request(f"/package-drafts/{draft_id}"), DRAFT_FIELDS)
    version = selected_fields(api.request(f"/content-items/{item_id}/versions/{version_id}"), VERSION_FIELDS)
    if (draft["draftId"] != draft_id or draft["itemId"] != item_id or draft["family"] != "map"
            or version["itemId"] != item_id or version["versionId"] != version_id
            or version["sourceDraftId"] != draft_id):
        raise ProbeError("saved map identifiers do not match the seeded map")
    return {"draft": draft, "version": version}


def seed(api: Api, manifest: dict) -> dict:
    require_durable(api)
    envelope = map_envelope(manifest)
    draft = api.request("/package-drafts", {
        "packageKey": f"upgrade-map-{uuid4().hex}", "workspaceId": "upgrade-preservation", "envelope": envelope})
    draft_id = identifier(draft.get("draftId"))
    item_id = identifier(draft.get("itemId"))
    version = api.request(f"/package-drafts/{draft_id}/content-versions", {"changeNote": "Upgrade preservation"})
    ids = {"draftId": draft_id, "itemId": item_id, "versionId": identifier(version.get("versionId"))}
    state = {"ids": ids, **snapshots(api, ids)}
    if any(state[kind]["envelope"].get("body") != envelope["body"] for kind in ("draft", "version")):
        raise ProbeError("saved map body does not match the seeded content")
    return state


def snapshot_hash(value: dict) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def verify(api: Api, state: dict) -> dict:
    require_durable(api)
    after = snapshots(api, state["ids"])
    for kind in ("draft", "version"):
        if after[kind] != state[kind]:
            raise ProbeError(f"saved map {kind} changed across the upgrade")
    return {"schema": 1, "status": "pass", **state["ids"], "checks": dict.fromkeys(CHECKS, True),
            "draftSnapshotSha256": snapshot_hash(after["draft"]),
            "versionSnapshotSha256": snapshot_hash(after["version"])}


def map_fields(value: dict) -> dict:
    if any(not isinstance(value.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", value[field])
           for field in HASH_FIELDS):
        raise ProbeError("saved map snapshot hash is invalid")
    return {**{field: identifier(value.get(field)) for field in ("draftId", "itemId", "versionId")},
            **{field: value[field] for field in HASH_FIELDS}}


def map_evidence(pair: dict, observed: dict) -> dict:
    return preservation_evidence(pair, observed, {"key": "maps", "checks": CHECKS, "fields": map_fields})


def main() -> int:
    return run_probe(StudioApi, seed, verify)

if __name__ == "__main__":
    raise SystemExit(main())
