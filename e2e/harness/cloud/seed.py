"""Publish the local harness's deterministic fixtures in a disposable cloud database."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

SEED = Path(__file__).resolve().parents[1] / "seed/seed.sh"


def seed(endpoint, key, target, out):
    # Keep one fixture source: the SQL is exactly what the composed-server harness applies.
    sql = SEED.read_text().split("psql_apply <<'SQL'\n", 1)[1].split("\nSQL\n", 1)[0]
    connection = target.seed_database(sql)

    def request(method, path, body):
        req = urllib.request.Request(endpoint.rstrip("/") + path,
            data=json.dumps(body).encode(), method=method,
            headers={"Content-Type": "application/json", "X-API-Key": key})
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)

    result = request("POST", "/api/v1/admin/connections/", {"name": "e2e-pg", **connection})
    data = result.get("data", result)
    cid = data.get("connectionId", data.get("id"))
    if not cid:
        raise ValueError("cloud connection response has no id")
    fixtures = [
        ("maui_parcels", "maui-parcels", "Polygon", "gid", False),
        ("maui_zoning", "maui-zoning", "Polygon", "gid", False),
        ("maui_roads", "maui-roads", "LineString", "gid", False),
        ("maui_flood_hazard", "maui-flood-hazard", "Polygon", "gid", False),
        ("maui_sea_level_rise", "maui-sea-level-rise", "Polygon", "gid", False),
        ("maui_place_names", "maui-place-names", "Point", "gid", False),
        ("maui_inspections", "maui-inspections", "Point", "id", True),
        ("workbench_assets", "honua-workbench", "Point", "OBJECTID", False),
        ("e2e_src_fs", "e2e", "Point", "gid", False),
    ]
    demo = {}
    ids = {}
    for table, service, geometry, pk, write in fixtures:
        result = request("POST", f"/api/v1/admin/connections/{cid}/layers", {
            "schema": "honua_data", "table": table, "layerName": service,
            "serviceName": service, "geometryColumn": "geom", "geometryType": geometry,
            "primaryKey": pk, "srid": 4326, "enabled": True})
        layer = result["data"]["layerId"]
        ids[table] = layer
        if service != "e2e":
            policy = {"allowAnonymous": True, "allowAnonymousWrite": write}
            request("PUT", f"/api/v1/admin/services/{service}/access-policy", policy)
            request("PUT", f"/api/v1/admin/services/{service}/layers/{layer}/metadata",
                    {"accessPolicy": policy})
            demo["workbench-assets" if table == "workbench_assets" else service] = {
                "service": service, "layerId": layer}
    manifest = {"connectionId": cid, "service": "maui-zoning",
        "layers": {"e2e_src_fs": ids["e2e_src_fs"], "maui_zoning": ids["maui_zoning"]},
        "slice1": {"e2e_src_fs": {"service": "e2e", "layerId": ids["e2e_src_fs"]}},
        "demo": demo}
    (out / "seed-manifest.json").write_text(json.dumps(manifest) + "\n")
    # The in-memory connection, for a caller that hands it on sealed; never written here.
    return connection
