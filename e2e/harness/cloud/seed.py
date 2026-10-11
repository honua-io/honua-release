"""Publish the local harness's deterministic fixtures in a disposable cloud database."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

SEED = Path(__file__).resolve().parents[1] / "seed/seed.sh"
# Per-instance identity on every response (honua-server#5790). A two-task cell's requests are
# spread across tasks; recording which task answered each call shows a cross-task read-after-write.
INSTANCE_HEADER = "Honua-Instance"
_INSTANCE = re.compile(r"[\x21-\x7e]{1,128}\Z")
_SENSITIVE_WORDS = (r"key|token|secret|password|passwd|authorization|credential|cookie|"
                    r"connectionstring|username|host")
_SENSITIVE = re.compile(_SENSITIVE_WORDS, re.IGNORECASE)
MAX_BODY_CHARS = 300


class SeedHttpError(Exception):
    """A non-2xx answer to one fixture publication call.

    Carries what is safe to report: method, path, status, the answering instance, a bounded and
    credential-masked body excerpt, and the instances that answered the calls before it. The
    request body (which carries the database credentials) is never kept.
    """

    def __init__(self, method, path, status, instance, body, earlier):
        super().__init__(f"HTTP {status} on {method} {path}")
        self.method, self.path, self.status = method, path, status
        self.instance, self.body, self.earlier = instance, body, earlier

    def describe(self, secrets=()):
        text = (f"HTTP {self.status} on {self.method} {self.path}"
                + (f" (answered by instance {self.instance})" if self.instance else "")
                + (f": {self.body}" if self.body else ""))
        if self.earlier:
            distinct = list(dict.fromkeys(i for i in self.earlier if i))
            text += (f"; {len(self.earlier)} earlier publication call(s) were answered by "
                     + (", ".join(distinct) if distinct else "no named instance"))
        for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        return text


def _scrub(text, secrets):
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    return text


def _redact(value, secrets=()):
    if isinstance(value, dict):
        return {k: "[redacted]" if _SENSITIVE.search(str(k)) else _redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    if isinstance(value, str):
        return _scrub(value, secrets)
    return value


def body_excerpt(raw, secrets=()):
    """A bounded excerpt of an error body: credential-named JSON members masked and every known
    secret value replaced, both before the excerpt is cut, so a cut never keeps part of a secret."""
    if not raw:
        return ""
    secrets = sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True)
    try:
        text = json.dumps(_redact(json.loads(raw.decode("utf-8")), secrets), ensure_ascii=True,
                          separators=(",", ":"))
    except (UnicodeError, ValueError, RecursionError):
        # Not JSON (a load balancer page, say): printable ASCII only, never a credential-like token.
        text = re.sub(r"[^ -~]", "?", _scrub(raw[:4096].decode("utf-8", "replace"), secrets))
        text = re.sub(r"(?i)(" + _SENSITIVE_WORDS + r")([\"']?\s*[:=]\s*)[^\s\"',;&<]+", r"\1\2[redacted]", text)
    text = _scrub(text, secrets)
    return text if len(text) <= MAX_BODY_CHARS else text[:MAX_BODY_CHARS] + "...(truncated)"


def _instance(headers):
    value = headers.get(INSTANCE_HEADER) if headers is not None else None
    return value if isinstance(value, str) and _INSTANCE.match(value) else None


def seed(endpoint, key, target, out):
    # Keep one fixture source: the SQL is exactly what the composed-server harness applies.
    sql = SEED.read_text(encoding="utf-8").split("psql_apply <<'SQL'\n", 1)[1].split("\nSQL\n", 1)[0]
    connection = target.seed_database(sql)
    answered = []

    def request(method, path, body):
        req = urllib.request.Request(endpoint.rstrip("/") + path,
            data=json.dumps(body).encode(), method=method,
            headers={"Content-Type": "application/json", "X-API-Key": key})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                answered.append(_instance(response.headers))
                return json.load(response)
        except urllib.error.HTTPError as error:
            raw = error.read(64 * 1024) if error.fp else b""
            # An echoed database coordinate or the key never reaches the scenario line, whole or cut.
            excerpt = body_excerpt(raw, (*connection.values(), key))
            raise SeedHttpError(method, path, error.code, _instance(error.headers),
                                excerpt, list(answered)) from None

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
