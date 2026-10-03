#!/usr/bin/env python3
"""Python SDK driver for the installed-client regression suite.

Runs against the installed, manifest-pinned ``honua-sdk`` and ``honua-admin`` wheels only and
calls their client classes, never raw HTTP. Prints one JSON observation per contract step;
oracles are evaluated by the runner, not here.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import traceback
from typing import Any, Callable

PLAN = json.loads(open(os.environ["SDKREG_PLAN"], encoding="utf-8").read())
BASE = PLAN["baseUrl"]
API_KEY = os.environ["SDKREG_API_KEY"]
BEARER = os.environ["SDKREG_BEARER"]
DB_PASSWORD = os.environ["SDKREG_DB_PASSWORD"]

from honua_admin import HonuaAdminClient  # noqa: E402
from honua_admin._models import CreateSecureConnectionRequest, PublishLayerRequest  # noqa: E402
from honua_sdk import HonuaClient  # noqa: E402
from honua_sdk.auth import StaticAuthProvider  # noqa: E402

API = {
    "sdk-auth": {
        "api-key-query": "honua_sdk.HonuaClient(api_key=).feature_server(...).query",
        "bearer-admin-list": "honua_admin.HonuaAdminClient(auth_provider=StaticAuthProvider).list_services",
        "bearer-query": "honua_sdk.HonuaClient(auth_provider=StaticAuthProvider).feature_server(...).query",
        "anonymous-refused": "honua_sdk.HonuaClient().feature_server(...).query",
    },
    "sdk-admin-lifecycle": {
        "create-datasource": "honua_admin.HonuaAdminClient.create_connection",
        "test-datasource": "honua_admin.HonuaAdminClient.test_connection",
        "publish": "honua_admin.HonuaAdminClient.publish_layer",
        "list": "honua_admin.HonuaAdminClient.list_layers",
        "served": "honua_sdk.HonuaClient.feature_server(...).query",
        "unpublish": "honua_admin.HonuaAdminClient.set_layer_enabled(False)",
        "unpublished-refused": "honua_sdk.HonuaClient.feature_server(...).query",
    },
    "sdk-geoservices": {
        "query": "honua_sdk.GeoServicesFeatureServerClient.query",
        "ids": "honua_sdk.GeoServicesFeatureServerClient.query(extra_params returnIdsOnly)",
        "count": "honua_sdk.HonuaClient.query(return_count_only=True)",
        "resolve-edit-ids": "honua_sdk.GeoServicesFeatureServerClient.query",
        "apply-edits-add": "honua_sdk.GeoServicesFeatureServerClient.apply_edits(adds)",
        "apply-edits-update": "honua_sdk.GeoServicesFeatureServerClient.apply_edits(updates)",
        "apply-edits-delete": "honua_sdk.GeoServicesFeatureServerClient.apply_edits(deletes)",
        "edits-state": "honua_sdk.GeoServicesFeatureServerClient.query",
        "add-attachment": "honua_sdk.GeoServicesFeatureServerClient.add_attachment",
        "query-attachments": "honua_sdk.GeoServicesFeatureServerClient.list_attachments",
    },
    "sdk-ogc-features": {
        "items-bbox": "honua_sdk.HonuaOgcFeatures.items(bbox=)",
        "item": "honua_sdk.HonuaOgcFeatures.item",
    },
    "sdk-ogc-tiles": {
        "vector-tile": "honua_sdk.OgcTilesClient.tile",
        "raster-tile": "honua_sdk.OgcTilesClient.tile",
        "empty-tile": "honua_sdk.OgcTilesClient.tile",
    },
    "sdk-ogc-processes": {
        "submit": "honua_sdk.HonuaGeoprocessing.submit_geometry",
        "poll": "honua_sdk.HonuaGeoprocessing.job",
        "result": "honua_sdk.HonuaGeoprocessing.results",
    },
    "sdk-stac": {"search": "honua_sdk.StacClient.search(json_body)"},
}


def emit(scenario: str, step: str, **payload: Any) -> None:
    print(json.dumps({"scenario": scenario, "step": step, "api": API[scenario][step], **payload}, default=str), flush=True)


def error_of(exc: BaseException) -> dict[str, Any]:
    """Error identity only: type and status. Messages stay in the job log, never the receipt."""
    status = getattr(exc, "error_code", None)
    if type(status) is not int:
        status = getattr(exc, "status_code", None)
    print(f"[python-driver] {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
    return {"type": type(exc).__name__, "status": status if type(status) is int else None}


def step(scenario: str, name: str, action: Callable[[], dict[str, Any]], state: dict[str, Any], needs: tuple[str, ...] = ()) -> None:
    missing = [dependency for dependency in needs if dependency not in state]
    if missing:
        emit(scenario, name, skipped=f"depends on {missing}, which did not complete")
        return
    try:
        emit(scenario, name, observed=action())
    except Unsupported as exc:
        emit(scenario, name, unsupported=str(exc))
    except Exception as exc:  # noqa: BLE001 - every SDK failure is an observation
        traceback.print_exc(file=sys.stderr)
        emit(scenario, name, error=error_of(exc))


class Unsupported(Exception):
    pass


def data_client(**auth: Any) -> HonuaClient:
    return HonuaClient(BASE, **auth)


def bearer_auth() -> dict[str, Any]:
    return {"auth_provider": StaticAuthProvider({"Authorization": f"Bearer {BEARER}"})}


def count_of(client: HonuaClient, service: str, layer_id: int, where: str = "1=1") -> dict[str, Any]:
    """The documented count API: HonuaClient.query(return_count_only=True)."""
    result = client.query(service, protocol="geoservices-feature-service", layer_id=layer_id, where=where, return_count_only=True)
    return {"count": result.total_count}


def rows_of(client: HonuaClient, service: str, layer_id: int) -> dict[str, Any]:
    """Every row through the FeatureServer client; the count is how many came back."""
    payload = client.feature_server(service).query(layer_id, where="1=1", out_fields="*")
    return {"count": len(payload.get("features") or [])}


def point_rows(payload: dict[str, Any], fields: list[str], *, with_oid: str | None = None) -> list[dict[str, Any]]:
    rows = []
    for feature in payload.get("features") or []:
        attributes = feature.get("attributes") or {}
        geometry = feature.get("geometry") or {}
        row = {"attributes": {field: attributes.get(field) for field in fields}, "x": geometry.get("x"), "y": geometry.get("y")}
        if with_oid:
            row["objectId"] = attributes.get(with_oid)
            row["gid"] = attributes.get("gid")
        rows.append(row)
    return rows


def edit_results(payload: dict[str, Any], key: str) -> dict[str, Any]:
    results = payload.get(key) or []
    return {"results": [{"success": item.get("success"), "objectId": item.get("objectId"),
                         "code": (item.get("error") or {}).get("code")} for item in results]}


def run_auth(state: dict[str, Any]) -> None:
    sites = PLAN["sites"]
    scenario = "sdk-auth"

    def api_key_query() -> dict[str, Any]:
        with data_client(api_key=API_KEY) as client:
            return rows_of(client, sites["service"], sites["layerId"])

    def bearer_admin_list() -> dict[str, Any]:
        with HonuaAdminClient(BASE, **bearer_auth()) as admin:
            return {"services": [getattr(item, "service_name", None) for item in admin.list_services()]}

    def bearer_query() -> dict[str, Any]:
        with data_client(**bearer_auth()) as client:
            return rows_of(client, sites["service"], sites["layerId"])

    def anonymous() -> dict[str, Any]:
        with data_client() as client:
            payload = client.feature_server(sites["service"]).query(sites["layerId"], where="1=1")
            return {"returned": len(payload.get("features") or [])}

    step(scenario, "api-key-query", api_key_query, state)
    step(scenario, "bearer-admin-list", bearer_admin_list, state)
    step(scenario, "bearer-query", bearer_query, state)
    step(scenario, "anonymous-refused", anonymous, state)


def run_admin(state: dict[str, Any]) -> None:
    life = PLAN["lifecycle"]
    db = life["database"]
    scenario = "sdk-admin-lifecycle"
    admin = HonuaAdminClient(BASE, api_key=API_KEY)

    def create() -> dict[str, Any]:
        created = admin.create_connection(CreateSecureConnectionRequest(
            name=life["connectionName"], host=db["host"], port=db["port"], database_name=db["databaseName"],
            username=db["username"], password=DB_PASSWORD, ssl_required=False, ssl_mode="Disable"))
        state["connection"] = created.connection_id
        return {"connectionId": created.connection_id}

    def test() -> dict[str, Any]:
        return {"success": admin.test_connection(state["connection"]).is_healthy}

    def publish() -> dict[str, Any]:
        published = admin.publish_layer(state["connection"], PublishLayerRequest(
            schema="honua_data", table=life["table"], layer_name=life["layerName"], geometry_column="geom",
            geometry_type=life["geometryType"], srid=4326, primary_key="gid", service_name=life["service"], enabled=True))
        state["layer"] = published.layer_id
        return {"layerId": published.layer_id, "layerName": published.layer_name,
                "serviceName": published.service_name, "enabled": published.enabled}

    def listed() -> dict[str, Any]:
        layers = admin.list_layers(state["connection"], life["service"])
        return {"layers": [{"layerId": item.layer_id, "enabled": item.enabled, "layerName": item.layer_name} for item in layers]}

    def served() -> dict[str, Any]:
        with data_client(api_key=API_KEY) as client:
            return rows_of(client, life["service"], state["layer"])

    def unpublish() -> dict[str, Any]:
        summary = admin.set_layer_enabled(state["connection"], state["layer"], False, life["service"])
        state["unpublished"] = True
        return {"layerId": summary.layer_id, "enabled": summary.enabled}

    def refused() -> dict[str, Any]:
        with data_client(api_key=API_KEY) as client:
            payload = client.feature_server(life["service"]).query(state["layer"], where="1=1")
            return {"returned": len(payload.get("features") or [])}

    try:
        step(scenario, "create-datasource", create, state)
        step(scenario, "test-datasource", test, state, ("connection",))
        step(scenario, "publish", publish, state, ("connection",))
        step(scenario, "list", listed, state, ("layer",))
        step(scenario, "served", served, state, ("layer",))
        step(scenario, "unpublish", unpublish, state, ("layer",))
        step(scenario, "unpublished-refused", refused, state, ("unpublished",))
    finally:
        admin.close()


def run_geoservices(state: dict[str, Any]) -> None:
    sites, edits = PLAN["sites"], PLAN["edits"]
    scenario = "sdk-geoservices"
    client = data_client(api_key=API_KEY)
    sites_layer = client.feature_server(sites["service"])
    edits_layer = client.feature_server(edits["service"])
    oid_field = "objectid"

    def query() -> dict[str, Any]:
        return {"features": point_rows(sites_layer.query(sites["layerId"], where=sites["where"], out_fields="*"), sites["fields"])}

    def ids() -> dict[str, Any]:
        payload = sites_layer.query(sites["layerId"], where=sites["where"], return_geometry=False,
                                    extra_params={"returnIdsOnly": "true"})
        return {"ids": payload.get("objectIds")}

    def count() -> dict[str, Any]:
        return count_of(client, sites["service"], sites["layerId"], sites["where"])

    def resolve() -> dict[str, Any]:
        payload = edits_layer.query(edits["layerId"], where="1=1", out_fields="*")
        nonlocal oid_field
        oid_field = payload.get("objectIdFieldName") or oid_field
        rows = point_rows(payload, edits["fields"], with_oid=oid_field)
        state["oids"] = {row["gid"]: row["objectId"] for row in rows}
        return {"features": [{"gid": row["gid"], "objectId": row["objectId"]} for row in rows]}

    def add() -> dict[str, Any]:
        feature = edits["add"]
        payload = edits_layer.apply_edits(edits["layerId"], adds=[{
            "geometry": {"x": feature["x"], "y": feature["y"], "spatialReference": {"wkid": 4326}},
            "attributes": {field: feature[field] for field in edits["fields"]}}])
        return edit_results(payload, "addResults")

    def update() -> dict[str, Any]:
        target = edits["update"]
        payload = edits_layer.apply_edits(edits["layerId"], updates=[{
            "attributes": {oid_field: state["oids"][target["gid"]], **target["attributes"]}}])
        return edit_results(payload, "updateResults")

    def delete() -> dict[str, Any]:
        payload = edits_layer.apply_edits(edits["layerId"], deletes=[state["oids"][edits["delete"]["gid"]]])
        return edit_results(payload, "deleteResults")

    def edited() -> dict[str, Any]:
        return {"features": point_rows(edits_layer.query(edits["layerId"], where="1=1", out_fields="*"), edits["fields"])}

    def attach() -> dict[str, Any]:
        attachment = edits["attachment"]
        # Upload from a file on disk, as a customer does; the SDK sends its base name.
        path = os.path.join(os.getcwd(), attachment["name"])
        with open(path, "wb") as handle:
            handle.write(attachment["content"].encode("utf-8"))
        result = edits_layer.add_attachment(edits["layerId"], state["oids"][attachment["gid"]], path,
                                            content_type=attachment["contentType"])
        state["attached"] = True
        return {"results": [{"success": getattr(result, "success", None), "objectId": getattr(result, "object_id", None)}]}

    def attachments() -> dict[str, Any]:
        infos = edits_layer.list_attachments(edits["layerId"], state["oids"][edits["attachment"]["gid"]])
        return {"attachments": [{"name": info.name, "contentType": info.content_type, "size": info.size} for info in infos]}

    try:
        step(scenario, "query", query, state)
        step(scenario, "ids", ids, state)
        step(scenario, "count", count, state)
        step(scenario, "resolve-edit-ids", resolve, state)
        step(scenario, "apply-edits-add", add, state, ("oids",))
        step(scenario, "apply-edits-update", update, state, ("oids",))
        step(scenario, "apply-edits-delete", delete, state, ("oids",))
        step(scenario, "edits-state", edited, state, ("oids",))
        step(scenario, "add-attachment", attach, state, ("oids",))
        step(scenario, "query-attachments", attachments, state, ("attached",))
    finally:
        client.close()


def geojson_point(feature: dict[str, Any]) -> tuple[Any, Any]:
    coordinates = (feature.get("geometry") or {}).get("coordinates") or [None, None]
    return coordinates[0], coordinates[1]


def run_ogc_features(state: dict[str, Any]) -> None:
    sites = PLAN["sites"]
    scenario = "sdk-ogc-features"
    with data_client(api_key=API_KEY) as client:
        ogc = client.ogc_features()

        def items() -> dict[str, Any]:
            payload = ogc.items(sites["collectionId"], bbox=sites["bbox"], limit=100)
            rows = []
            for feature in payload.get("features") or []:
                x, y = geojson_point(feature)
                rows.append({"id": feature.get("id"), "x": x, "y": y})
            return {"features": rows}

        def item() -> dict[str, Any]:
            feature = ogc.item(sites["collectionId"], sites["itemId"])
            x, y = geojson_point(feature)
            return {"id": feature.get("id"), "properties": feature.get("properties"), "x": x, "y": y}

        step(scenario, "items-bbox", items, state)
        step(scenario, "item", item, state)


def run_tiles(state: dict[str, Any]) -> None:
    area = PLAN["area"]
    scenario = "sdk-ogc-tiles"
    with data_client(api_key=API_KEY) as client:
        tiles = client.ogc_tiles()

        def fetch(spec: dict[str, Any]) -> bytes:
            return tiles.tile(area["tileMatrixSet"], str(spec["tileMatrix"]), spec["tileRow"], spec["tileCol"],
                              collection_id=area["collectionId"])

        def vector() -> dict[str, Any]:
            return {"bytes": base64.b64encode(fetch(area["painted"])).decode()}

        def raster() -> dict[str, Any]:
            # OgcTilesClient.tile(tile_matrix_set_id, tile_matrix, row, col, *, collection_id) takes no
            # format, extra_params or Accept override, so a raster (PNG) tile of a vector collection
            # cannot be requested through the published client.
            raise Unsupported("OgcTilesClient.tile has no format/extra_params selector; only the default (vector) encoding is reachable")

        def empty() -> dict[str, Any]:
            data = fetch(area["empty"])
            return {"empty": len(data) == 0, "size": len(data)}

        step(scenario, "vector-tile", vector, state)
        step(scenario, "raster-tile", raster, state)
        step(scenario, "empty-tile", empty, state)


def run_processes(state: dict[str, Any]) -> None:
    spec = PLAN["processes"]
    scenario = "sdk-ogc-processes"
    with data_client(api_key=API_KEY) as client:
        gp = client.geoprocessing()

        def submit() -> dict[str, Any]:
            job = gp.submit_geometry(spec["processId"], {
                "wkb": {"type": "Point", "coordinates": spec["point"]}, "srid": spec["srid"], "distance": spec["distance"]})
            state["job"] = job.job_id
            return {"jobId": job.job_id, "status": job.status}

        def poll() -> dict[str, Any]:
            deadline = time.monotonic() + spec["pollTimeoutSeconds"]
            job = gp.job(state["job"])
            while not job.is_terminal and time.monotonic() < deadline:
                time.sleep(0.5)
                job = gp.job(state["job"])
            if job.succeeded:
                state["succeeded"] = True
            return {"status": job.status}

        def result() -> dict[str, Any]:
            payload = gp.results(state["job"])
            output = next(iter(payload.values())) if isinstance(payload, dict) and payload else None
            value = output.get("value") if isinstance(output, dict) and "value" in output else output
            return {"geometry": value}

        step(scenario, "submit", submit, state)
        step(scenario, "poll", poll, state, ("job",))
        step(scenario, "result", result, state, ("succeeded",))


def run_stac(state: dict[str, Any]) -> None:
    sites = PLAN["sites"]
    with data_client(api_key=API_KEY) as client:
        def search() -> dict[str, Any]:
            payload = client.stac().search(json_body={"collections": [sites["collectionId"]], "bbox": sites["bbox"], "limit": 100})
            rows = []
            for feature in payload.get("features") or []:
                x, y = geojson_point(feature)
                rows.append({"id": feature.get("id"), "x": x, "y": y})
            return {"features": rows}

        step("sdk-stac", "search", search, state)


RUNNERS = {
    "sdk-auth": run_auth, "sdk-admin-lifecycle": run_admin, "sdk-geoservices": run_geoservices,
    "sdk-ogc-features": run_ogc_features, "sdk-ogc-tiles": run_tiles, "sdk-ogc-processes": run_processes,
    "sdk-stac": run_stac,
}


def main() -> int:
    for scenario in PLAN["scenarios"]:
        try:
            RUNNERS[scenario["id"]]({})
        except Exception:  # noqa: BLE001 - a crashed scenario leaves its steps unobserved (a fail)
            traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
