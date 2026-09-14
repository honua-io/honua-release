#!/usr/bin/env python3
"""#343 retry probe: can the manifest-pinned server create a MANAGED-store collection that
accepts an OGC API Features create edit through any supported product surface?

Boots the exact drill topology (e2e/dr-drill/compose.full-platform.yml, image@digest from
platform-manifest.yaml) through the drill's own Stack, records every request/response, and
tears the stack down. Diagnostic only; not a DR receipt.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

WT = Path(os.environ["HONUA_RELEASE_WT"])
sys.path.insert(0, str(WT / "e2e" / "dr-drill"))
import full_platform as fp  # noqa: E402

OUT = Path(os.environ.get("PROBE_OUT", "/tmp/honua-343-probe/out"))
OUT.mkdir(parents=True, exist_ok=True)
log: list[dict] = []


def call(stack, method, path, body=None, headers=None):
    url = f"http://127.0.0.1:{stack.server_port}{path}"
    head = {"X-API-Key": fp.ADMIN_KEY}
    head.update(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        head.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=head)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status, payload = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    text = payload.decode("utf-8", "replace")
    entry = {"request": {"method": method, "path": path,
                         "body": body if not isinstance(body, bytes) else body.decode("utf-8", "replace")},
             "status": status, "response": text[:4000]}
    log.append(entry)
    print(f"{method} {path} -> {status}: {text[:300]}", flush=True)
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def main():
    stack = fp.Stack(OUT)
    identity = {"image": stack.image, "serverSha": stack.server_sha,
                "manifestSha256": stack.candidate_digest}
    print(identity, flush=True)
    try:
        stack.compose("up", "-d", "--wait")
        stack.wait_healthy("server")

        # 1. Where the managed feature table lives on this image.
        managed = stack.psql("SELECT table_schema || '.' || table_name FROM information_schema.tables "
                             "WHERE table_name = 'features' ORDER BY 1")
        log.append({"sql": "managed features table", "result": managed})
        print("managed features table:", managed, flush=True)
        schema = managed.splitlines()[0].split(".")[0] if managed else "honua"

        # 2. The admin OpenAPI publish request shape, as served.
        for path in ("/openapi/admin.json", "/openapi/v1.json", "/swagger/v1/swagger.json",
                     "/api/v1/admin/openapi.json", "/openapi.json"):
            status, doc = call(stack, "GET", path)
            if status == 200 and isinstance(doc, dict) and "components" in doc:
                schemas = doc.get("components", {}).get("schemas", {})
                picked = {k: v for k, v in schemas.items() if "PublishLayer" in k}
                (OUT / "openapi-publish-schemas.json").write_text(json.dumps({"path": path, "schemas": picked},
                                                                             indent=2))
                ops = {p: list(v) for p, v in doc.get("paths", {}).items() if "/layers" in p}
                (OUT / "openapi-layer-paths.json").write_text(json.dumps(ops, indent=2))
                log[-1]["response"] = f"<openapi document; PublishLayer schemas: {sorted(picked)}>"

        # 3. Connection, then publish a layer directly onto the managed features table.
        _, conn = call(stack, "POST", "/api/v1/admin/connections/", {
            "name": "dr-pg", "host": "db", "port": 5432, "databaseName": "honua",
            "username": "honua", "password": "honua", "provider": "PostGIS",
            "sslRequired": False, "sslMode": "Disable"})
        connection = conn["data"]["connectionId"]
        # Keep the password out of the retained log.
        log[-1]["request"]["body"]["password"] = "<redacted>"
        call(stack, "POST", f"/api/v1/admin/connections/{connection}/tables/validate",
             {"schema": schema, "table": "features"})
        status, published = call(stack, "POST", f"/api/v1/admin/connections/{connection}/layers", {
            "schema": schema, "table": "features", "layerName": "dr-managed", "serviceName": "dr-managed",
            "geometryType": "Point", "srid": 4326, "enabled": True, "allowEmptyTable": True,
            "capabilities": ["Query", "Create", "Update", "Delete"]})
        if status in (200, 201):
            data = published.get("data", published) if isinstance(published, dict) else {}
            layer_id = data.get("layerId")
            call(stack, "GET", "/ogc/features/collections/dr-managed")
            call(stack, "GET", f"/ogc/features/collections/{layer_id}")
            for collection in ("dr-managed", str(layer_id)):
                call(stack, "POST", f"/ogc/features/collections/{collection}/items",
                     {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-156.5, 20.9]},
                      "properties": {}}, headers={"Content-Type": "application/geo+json"})
            call(stack, "POST", f"/rest/services/dr-managed/FeatureServer/{layer_id}/addFeatures",
                 b"f=json&features=" + urllib.request.quote(json.dumps(
                     [{"geometry": {"x": -156.5, "y": 20.9, "spatialReference": {"wkid": 4326}},
                       "attributes": {}}])).encode(),
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
            call(stack, "GET", f"/rest/services/dr-managed/FeatureServer/{layer_id}?f=json")
            call(stack, "GET", "/ogc/features/collections/dr-managed/items?limit=10")
    finally:
        (OUT / "probe-log.json").write_text(json.dumps({"identity": identity, "log": log}, indent=2) + "\n")
        stack.compose("logs", "--no-color", "server", check=False)
        (OUT / "server.log").write_text(stack.compose("logs", "--no-color", "--tail", "400", "server", check=False))
        if not os.environ.get("PROBE_KEEP"):
            stack.compose("down", "-v", "--remove-orphans", check=False)


if __name__ == "__main__":
    main()
