"""Oracles for the SDK regression scenarios.

Every expected value is derived from ``fixture.v1.json``. A driver reports what the SDK returned
(an observation); an oracle compares it with the fixture and returns ``(passed, summary)``. The
summary is built here from fixture-derived numbers and ids only, so it is safe to retain in a
receipt: it never carries a response body, a credential or a message the server wrote.
"""
from __future__ import annotations

import base64
import binascii
import math
from typing import Any

from render_png import decode_png, oracle_map_render, render_expectations

from mcp_plan_validation import oracle_mcp_plan_invalid, oracle_mcp_plan_valid

COORDINATE_TOLERANCE = 1e-9
REFUSED_STATUSES = {401, 403, 499}


# ── fixture-derived expectations ─────────────────────────────────────────────────────────────


def matches_filter(feature: dict[str, Any], spec: dict[str, Any]) -> bool:
    value, operand = feature[spec["field"]], spec["value"]
    return {
        "=": value == operand, ">=": value >= operand, ">": value > operand,
        "<=": value <= operand, "<": value < operand,
    }[spec["op"]]


def where_clause(spec: dict[str, Any]) -> str:
    value = spec["value"]
    literal = f"'{value}'" if isinstance(value, str) else str(value)
    return f"{spec['field']} {spec['op']} {literal}"


def filtered_sites(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    sites = fixture["sites"]
    return [row for row in sites["features"] if matches_filter(row, sites["filter"])]


def sites_in_bbox(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    minx, miny, maxx, maxy = fixture["sites"]["bbox"]
    return [row for row in fixture["sites"]["features"] if minx <= row["x"] <= maxx and miny <= row["y"] <= maxy]


def edited_features(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """The edits layer after add, attribute-only update and delete."""
    edits = fixture["edits"]
    rows = {row["gid"]: dict(row) for row in edits["features"]}
    rows[edits["add"]["gid"]] = dict(edits["add"])
    rows[edits["update"]["gid"]].update(edits["update"]["attributes"])
    del rows[edits["delete"]["gid"]]
    return [rows[gid] for gid in sorted(rows)]


def attachment_size(fixture: dict[str, Any]) -> int:
    return len(fixture["edits"]["attachment"]["content"].encode("utf-8"))


# ── observation helpers ──────────────────────────────────────────────────────────────────────


def _error_summary(observation: dict[str, Any]) -> str:
    error = observation.get("error") or {}
    status = error.get("status")
    return f"raised {error.get('type') or 'an error'}" + (f" (status {status})" if status is not None else "")


def _close(a: Any, b: Any, tolerance: float = COORDINATE_TOLERANCE) -> bool:
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(float(a) - float(b)) <= tolerance


def _rows(observed: Any, key: str = "features") -> list[dict[str, Any]] | None:
    rows = observed.get(key) if isinstance(observed, dict) else None
    return rows if isinstance(rows, list) and all(isinstance(row, dict) for row in rows) else None


def _feature_rows(observed: dict[str, Any], fields: list[str]) -> list[tuple] | None:
    rows = _rows(observed)
    if rows is None:
        return None
    result = []
    for row in rows:
        attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else row.get("properties")
        if not isinstance(attributes, dict):
            return None
        result.append((tuple(attributes.get(field) for field in fields), row.get("x"), row.get("y")))
    return sorted(result, key=lambda item: repr(item[0]))


def _compare_features(observed: dict[str, Any], expected: list[dict[str, Any]], fields: list[str], label: str) -> tuple[bool, str]:
    rows = _feature_rows(observed, fields)
    if rows is None:
        return False, f"{label}: the SDK returned no feature list"
    want = sorted(((tuple(row[field] for field in fields), row["x"], row["y"]) for row in expected),
                  key=lambda item: repr(item[0]))
    gids = [row["gid"] for row in expected]
    if len(rows) != len(want):
        return False, f"{label}: {len(rows)} features, fixture expects {len(want)} (gids {gids})"
    for (got_attrs, gx, gy), (want_attrs, wx, wy) in zip(rows, want):
        if got_attrs != want_attrs:
            return False, f"{label}: attributes {dict(zip(fields, got_attrs))} differ from fixture {dict(zip(fields, want_attrs))}"
        if not (_close(gx, wx) and _close(gy, wy)):
            return False, f"{label}: gid {dict(zip(fields, want_attrs)).get('gid')} ordinates ({gx}, {gy}) differ from fixture ({wx}, {wy})"
    return True, f"{label}: {len(rows)} features equal the fixture (gids {gids}) with exact ordinates"


# ── oracle implementations ───────────────────────────────────────────────────────────────────


def oracle_count(observed: dict[str, Any], expected: int) -> tuple[bool, str]:
    count = observed.get("count")
    if type(count) is not int:
        return False, "the SDK returned no integer count"
    return count == expected, f"count {count}, fixture expects {expected}"


def oracle_contains_service(observed: dict[str, Any], service: str) -> tuple[bool, str]:
    services = observed.get("services")
    if not isinstance(services, list):
        return False, "the SDK returned no service list"
    return service in services, f"service {service!r} {'is' if service in services else 'is not'} among {len(services)} listed services"


def oracle_refused(observation: dict[str, Any]) -> tuple[bool, str]:
    if "error" not in observation:
        return False, "the SDK returned success for an anonymous call the fixture's access policy refuses"
    error = observation["error"]
    status = error.get("status")
    ok = status in REFUSED_STATUSES
    return ok, f"{_error_summary(observation)}; expected an authentication refusal ({sorted(REFUSED_STATUSES)})"


def oracle_not_found(observation: dict[str, Any]) -> tuple[bool, str]:
    if "error" not in observation:
        return False, "the SDK returned success for a layer the fixture unpublished"
    status = observation["error"].get("status")
    return status == 404, f"{_error_summary(observation)}; expected not found (404)"


def oracle_object_ids(observed: dict[str, Any], expected: list[int]) -> tuple[bool, str]:
    ids = observed.get("ids")
    if not isinstance(ids, list) or any(type(value) is not int for value in ids):
        return False, "the SDK returned no integer id list"
    return sorted(ids) == sorted(expected), f"ids {sorted(ids)}, fixture expects {sorted(expected)}"


def oracle_edit_ids(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    rows = _rows(observed)
    want = sorted(row["gid"] for row in fixture["edits"]["features"])
    if rows is None:
        return False, "the SDK returned no feature list"
    gids = sorted(row.get("gid") for row in rows)
    ids = [row.get("objectId") for row in rows]
    if gids != want or any(type(value) is not int for value in ids) or len(set(ids)) != len(ids):
        return False, f"edits layer gids {gids} (unique integer object ids: {len(set(map(str, ids))) == len(ids)}), fixture expects {want}"
    return True, f"{len(rows)} managed features resolved to unique object ids for fixture gids {want}"


def oracle_edit_success(observed: dict[str, Any]) -> tuple[bool, str]:
    results = observed.get("results")
    if not isinstance(results, list) or not results:
        return False, "the SDK returned no edit results"
    failed = [result for result in results if not (isinstance(result, dict) and result.get("success") is True)]
    if failed:
        codes = [result.get("code") for result in failed if isinstance(result, dict)]
        return False, f"{len(failed)} of {len(results)} edit results failed (codes {codes})"
    return len(results) == 1, f"{len(results)} edit result(s), all successful"


def oracle_attachments(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    rows = _rows(observed, "attachments")
    attachment = fixture["edits"]["attachment"]
    want = (attachment["name"], attachment["contentType"], attachment_size(fixture))
    if rows is None:
        return False, "the SDK returned no attachment list"
    got = [(row.get("name"), row.get("contentType"), row.get("size")) for row in rows]
    return got == [want], f"attachments {got}, fixture expects {[want]}"


def oracle_items(observed: dict[str, Any], expected: list[dict[str, Any]], label: str) -> tuple[bool, str]:
    rows = _rows(observed)
    if rows is None:
        return False, f"{label}: the SDK returned no item list"
    got = sorted((str(row.get("id")), row.get("x"), row.get("y")) for row in rows)
    want = sorted((str(row["gid"]), row["x"], row["y"]) for row in expected)
    if [item[0] for item in got] != [item[0] for item in want]:
        return False, f"{label}: item ids {[item[0] for item in got]}, fixture expects {[item[0] for item in want]}"
    for (gid, gx, gy), (_, wx, wy) in zip(got, want):
        if not (_close(gx, wx) and _close(gy, wy)):
            return False, f"{label}: item {gid} ordinates ({gx}, {gy}) differ from fixture ({wx}, {wy})"
    return True, f"{label}: items {[item[0] for item in want]} equal the fixture with exact ordinates"


def oracle_item(observed: dict[str, Any], expected: dict[str, Any]) -> tuple[bool, str]:
    properties = observed.get("properties") if isinstance(observed.get("properties"), dict) else {}
    fields = ("gid", "name", "rank")
    got = tuple(properties.get(field) for field in fields)
    want = tuple(expected[field] for field in fields)
    if str(observed.get("id")) != str(expected["gid"]) or got != want:
        return False, f"item {observed.get('id')!r} properties {dict(zip(fields, got))}, fixture expects {dict(zip(fields, want))}"
    if not (_close(observed.get("x"), expected["x"]) and _close(observed.get("y"), expected["y"])):
        return False, f"item {expected['gid']} ordinates ({observed.get('x')}, {observed.get('y')}) differ from the fixture"
    return True, f"item {expected['gid']} equals the fixture row with exact ordinates"


def oracle_job_accepted(observed: dict[str, Any]) -> tuple[bool, str]:
    job_id, status = observed.get("jobId"), observed.get("status")
    ok = isinstance(job_id, str) and bool(job_id) and status in {"accepted", "running", "successful"}
    return ok, f"job submitted asynchronously with status {status!r}" if ok else f"no asynchronous job (status {status!r})"


def oracle_job_succeeded(observed: dict[str, Any]) -> tuple[bool, str]:
    status = observed.get("status")
    return status == "successful", f"terminal job status {status!r}"


def _exterior_ring(geometry: Any) -> list[list[float]] | None:
    if isinstance(geometry, dict) and geometry.get("type") == "Feature":
        geometry = geometry.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
        return None
    rings = geometry.get("coordinates")
    if not isinstance(rings, list) or not rings or not isinstance(rings[0], list) or len(rings[0]) < 4:
        return None
    return rings[0]


def oracle_buffer(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    spec = fixture["processes"]
    (cx, cy), distance, tolerance = spec["point"], spec["distance"], spec["tolerance"]
    ring = _exterior_ring(observed.get("geometry"))
    if ring is None:
        return False, "the job result holds no polygon geometry"
    geometry = observed["geometry"]
    if geometry.get("type") == "Feature":
        geometry = geometry["geometry"]
    if len(geometry["coordinates"]) != 1:
        return False, "point buffer must not contain interior rings"
    if not all(isinstance(vertex, list) and len(vertex) >= 2
               and all(type(value) in (int, float) and math.isfinite(value) for value in vertex[:2]) for vertex in ring):
        return False, "buffer coordinates must be finite numbers"
    vertices = ring[:-1] if ring[0] == ring[-1] else ring
    # Radius alone accepts a diamond (or a crossed polygon) with correct vertices.
    area = abs(sum((a[0] - cx) * (b[1] - cy) - (b[0] - cx) * (a[1] - cy)
                   for a, b in zip(ring, ring[1:]))) / 2
    if not math.isclose(area, math.pi * distance ** 2, rel_tol=0.01):
        return False, "buffer area differs from the requested circle by more than 1%"
    if any(math.hypot((a[0] + b[0]) / 2 - cx, (a[1] + b[1]) / 2 - cy) < distance * 0.99
           for a, b in zip(ring, ring[1:])):
        return False, "buffer boundary has a chord cutting more than 1% inside the requested circle"
    worst = max(abs(math.hypot(x - cx, y - cy) - distance) for x, y in (vertex[:2] for vertex in vertices))
    mean_x = sum(vertex[0] for vertex in vertices) / len(vertices)
    mean_y = sum(vertex[1] for vertex in vertices) / len(vertices)
    centred = math.hypot(mean_x - cx, mean_y - cy) <= tolerance
    ok = ring[0] == ring[-1] and worst <= tolerance and centred
    return ok, (
        f"{len(vertices)}-vertex closed ring: max radius error {worst:.2e} (tolerance {tolerance:.0e}), "
        f"centroid offset {math.hypot(mean_x - cx, mean_y - cy):.2e} from fixture point ({cx}, {cy})"
    )


# ── tiles ────────────────────────────────────────────────────────────────────────────────────


def tile_bounds_mercator(z: int, row: int, col: int) -> tuple[float, float, float, float]:
    """WebMercatorQuad tile bounds as normalized world coordinates (0..1, y downwards)."""
    n = 2 ** z
    return col / n, row / n, (col + 1) / n, (row + 1) / n


def lonlat_to_world(lon: float, lat: float) -> tuple[float, float]:
    x = (lon + 180.0) / 360.0
    s = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return x, y


def world_to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))
    return lon, lat


def _envelope_in_tile(envelope: list[float], tile: dict[str, int], extent: float) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = tile_bounds_mercator(tile["tileMatrix"], tile["tileRow"], tile["tileCol"])
    (ax, ay), (bx, by) = lonlat_to_world(envelope[0], envelope[3]), lonlat_to_world(envelope[2], envelope[1])
    scale_x, scale_y = extent / (maxx - minx), extent / (maxy - miny)
    clamp = lambda value: min(max(value, 0.0), extent)  # noqa: E731
    return clamp((ax - minx) * scale_x), clamp((ay - miny) * scale_y), clamp((bx - minx) * scale_x), clamp((by - miny) * scale_y)


def expected_pixels(fixture: dict[str, Any]) -> list[tuple[int, int, bool]]:
    """(x, y, painted) for each fixture sample pixel, skipping pixels near a polygon edge."""
    area = fixture["area"]
    tiles = area["tiles"]
    margin = tiles["edgeMarginPixels"]
    boxes = [_envelope_in_tile(feature["envelope"], tiles["painted"], 256.0) for feature in area["features"]]
    result = []
    for x, y in tiles["samples"]:
        cx, cy = x + 0.5, y + 0.5
        inside = any(bx0 <= cx <= bx1 and by0 <= cy <= by1 for bx0, by0, bx1, by1 in boxes)
        near = any(
            min(abs(cx - bx0), abs(cx - bx1)) < margin and by0 - margin <= cy <= by1 + margin
            or min(abs(cy - by0), abs(cy - by1)) < margin and bx0 - margin <= cx <= bx1 + margin
            for bx0, by0, bx1, by1 in boxes
        )
        if not near:
            result.append((x, y, inside))
    return result


def oracle_raster_tile(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    try:
        data = base64.b64decode(observed.get("bytes") or "", validate=True)
        width, height, pixel = decode_png(data)
    except (binascii.Error, ValueError) as exc:
        return False, f"the tile is not a decodable PNG ({type(exc).__name__}); content type {observed.get('contentType')!r}"
    if (width, height) != (256, 256):
        return False, f"tile is {width}x{height}, WebMercatorQuad tiles are 256x256"
    mismatches, samples = [], expected_pixels(fixture)
    for x, y, painted in samples:
        alpha = pixel(x, y)[3]
        if (alpha > 0) != painted:
            mismatches.append(f"({x},{y}) alpha {alpha} expected {'painted' if painted else 'transparent'}")
    if not samples:
        return False, "the fixture yields no sample pixel away from a polygon edge"
    painted = sum(1 for *_, inside in samples if inside)
    if mismatches:
        return False, f"{len(mismatches)} of {len(samples)} sample pixels disagree with the fixture polygon: {mismatches}"
    return True, f"{painted} painted and {len(samples) - painted} transparent sample pixels match the fixture polygon"


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value, shift = 0, 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def _fields(data: bytes):
    offset = 0
    while offset < len(data):
        key, offset = _varint(data, offset)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 2:
            length, offset = _varint(data, offset)
            value, offset = data[offset:offset + length], offset + length
        elif wire == 5:
            value, offset = data[offset:offset + 4], offset + 4
        elif wire == 1:
            value, offset = data[offset:offset + 8], offset + 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def _packed(data: bytes) -> list[int]:
    values, offset = [], 0
    while offset < len(data):
        value, offset = _varint(data, offset)
        values.append(value)
    return values


def decode_mvt(data: bytes) -> list[dict[str, Any]]:
    """Decode the layers of a Mapbox vector tile: name, extent and each feature's type and points."""
    layers = []
    for number, wire, value in _fields(data):
        if number != 3 or wire != 2:
            continue
        layer = {"name": None, "extent": 4096, "features": []}
        for field, field_wire, item in _fields(value):
            if field == 1 and field_wire == 2:
                layer["name"] = item.decode("utf-8", "replace")
            elif field == 5 and field_wire == 0:
                layer["extent"] = item
            elif field == 2 and field_wire == 2:
                feature = {"type": 0, "points": []}
                for part, part_wire, content in _fields(item):
                    if part == 3 and part_wire == 0:
                        feature["type"] = content
                    elif part == 4 and part_wire == 2:
                        commands, index, x, y = _packed(content), 0, 0, 0
                        while index < len(commands):
                            command, count = commands[index] & 7, commands[index] >> 3
                            index += 1
                            if command in (1, 2):
                                for _ in range(count):
                                    dx, dy = commands[index], commands[index + 1]
                                    index += 2
                                    x += (dx >> 1) ^ -(dx & 1)
                                    y += (dy >> 1) ^ -(dy & 1)
                                    feature["points"].append((x, y))
                layer["features"].append(feature)
        layers.append(layer)
    return layers


def oracle_vector_tile(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    try:
        layers = decode_mvt(base64.b64decode(observed.get("bytes") or "", validate=True))
    except (ValueError, IndexError) as exc:
        return False, f"the tile is not a decodable vector tile ({type(exc).__name__}); content type {observed.get('contentType')!r}"
    features = [(layer, feature) for layer in layers for feature in layer["features"]]
    area = fixture["area"]
    if len(features) != len(area["features"]):
        return False, f"vector tile holds {len(features)} features, fixture expects {len(area['features'])}"
    layer, feature = features[0]
    if feature["type"] != 3 or not feature["points"]:
        return False, f"vector tile feature has geometry type {feature['type']}, fixture expects a polygon (3)"
    extent = float(layer["extent"])
    xs, ys = [point[0] for point in feature["points"]], [point[1] for point in feature["points"]]
    clamp = lambda value: min(max(value, 0.0), extent)  # noqa: E731
    got = (clamp(min(xs)), clamp(min(ys)), clamp(max(xs)), clamp(max(ys)))
    want = _envelope_in_tile(area["features"][0]["envelope"], area["tiles"]["painted"], extent)
    tolerance = extent / 128
    worst = max(abs(a - b) for a, b in zip(got, want))
    return worst <= tolerance, (
        f"one polygon in layer {layer['name']!r}; bounds {tuple(round(v) for v in got)} vs projected fixture "
        f"{tuple(round(v) for v in want)} (max deviation {worst:.0f}, tolerance {tolerance:.0f} of extent {extent:.0f})"
    )


def oracle_empty_tile(observed: dict[str, Any]) -> tuple[bool, str]:
    empty, size = observed.get("empty"), observed.get("size")
    ok = empty is True or size == 0
    return ok, f"empty={empty!r}, {size!r} bytes; the fixture has no feature in this tile"


# ── command-line and MCP workflows ───────────────────────────────────────────────────────────


def oracle_proposal_pending(observed: dict[str, Any]) -> tuple[bool, str]:
    status, proposal = observed.get("status"), observed.get("proposalId")
    ok = status == "RequiresApproval" and observed.get("requiresApproval") is True and isinstance(proposal, str) and bool(proposal)
    if not ok:
        return False, (f"status {status!r}, requiresApproval {observed.get('requiresApproval')!r}, proposal id present: {bool(proposal)}; "
                       "the fixture's approval policy requires a proposal")
    if observed.get("servedBeforeApproval") is not False:
        return False, (f"the proposed service was served before approval (servedBeforeApproval "
                       f"{observed.get('servedBeforeApproval')!r}); the approval policy must hold the publication")
    return True, f"publication recorded as a proposal awaiting approval (status {status!r}), not served before approval"


def oracle_self_approval_refused(observation: dict[str, Any]) -> tuple[bool, str]:
    if "error" not in observation:
        return False, "the proposer approved its own proposal; separation of duties requires a refusal"
    status = observation["error"].get("status")
    after = (observation.get("observed") or {}).get("status")
    ok = status == 403 and after == "AwaitingApproval"
    return ok, f"{_error_summary(observation)}; the proposal is {after!r} afterwards (expected 403 and 'AwaitingApproval')"


def oracle_proposal_approved(observed: dict[str, Any]) -> tuple[bool, str]:
    status = observed.get("status")
    return status in {"Succeeded", "Approved", "Applied"}, f"approval returned proposal status {status!r}"


def oracle_proposal_resolved(observed: dict[str, Any], principals: dict[str, Any]) -> tuple[bool, str]:
    """The proposal names the proposer as requester and the separate approver as resolver."""
    proposer, approver = principals.get("proposerId"), principals.get("approverId")
    requested, resolved = str(observed.get("requestedBy") or ""), str(observed.get("resolvedBy") or "")
    checks = {
        "status Succeeded": observed.get("status") == "Succeeded",
        "kind ServicePublish": observed.get("kind") == "ServicePublish",
        "requested by the proposer": bool(proposer) and requested.endswith(str(proposer)),
        "resolved by the approver": bool(approver) and resolved.endswith(str(approver)),
        "two principals": bool(requested) and bool(resolved) and requested != resolved,
    }
    failed = [name for name, ok in checks.items() if not ok]
    return not failed, ("proposal succeeded, requested by the proposer and resolved by the separate approver" if not failed
                        else f"proposal status {observed.get('status')!r} kind {observed.get('kind')!r}; failed: {', '.join(failed)}")


def oracle_mcp_initialized(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    version = observed.get("protocolVersion")
    named = all(isinstance(observed.get(key), str) and observed[key] for key in ("serverName", "serverVersion"))
    want = fixture["mcp"]["protocolVersion"]
    return version == want and named, f"protocol {version!r} (fixture {want!r}), server identity present: {named}"


def oracle_mcp_view(observed: dict[str, Any], fixture: dict[str, Any], view: str,
                    pinned: dict[str, Any] | None = None) -> tuple[bool, str]:
    """The complete view with the fixture's count; with a pinned roster, exactly its revision and tool names."""
    count = fixture["mcp"]["views"][view]["toolCount"]
    names = observed.get("names") if isinstance(observed.get("names"), list) else []
    got_view, revision, meta_count = observed.get("view"), observed.get("revision"), observed.get("toolCount")
    ok = (got_view == view and isinstance(revision, str) and bool(revision) and meta_count == count
          and len(names) == count == len(set(names)) and observed.get("nextCursor") is None)
    detail = (f"selector-free tools/list returned view {got_view!r} ({revision}) with {len(names)} tools "
              f"(metadata {meta_count}); the fixture expects the complete {view!r} view with {count}")
    if pinned is None:
        return ok, detail
    missing, extra = sorted(set(pinned["tools"]) - set(names)), sorted(set(names) - set(pinned["tools"]))
    if ok and revision == pinned["revision"] and not missing and not extra:
        return True, f"{detail}, equal to the pinned {pinned['revision']} roster"
    drift = "".join(f"; {label} {values}" for label, values in (("missing", missing), ("not pinned", extra)) if values)
    return False, f"{detail}; the pinned roster is {pinned['revision']} with {len(pinned['tools'])} tools{drift}"


def oracle_mcp_permission_denied(observation: dict[str, Any]) -> tuple[bool, str]:
    if "error" not in observation:
        return False, "an anonymous session received the full catalog"
    kind = observation["error"].get("type")
    return kind == "permission_denied", f"anonymous full-view request refused with {kind!r} (expected 'permission_denied')"


def oracle_mcp_full_catalog(observed: dict[str, Any], roster: list[str], default_names: list[str] | None) -> tuple[bool, str]:
    """The drained full view is exactly the pinned roster (names), with no duplicates, and restores the default view."""
    names = observed.get("names") if isinstance(observed.get("names"), list) else []
    missing, extra = sorted(set(roster) - set(names)), sorted(set(names) - set(roster))
    checks = {
        "unique": len(names) == len(set(names)),
        f"exactly the {len(roster)}-tool pinned roster": bool(roster) and not missing and not extra,
        "includes the default view": bool(default_names) and not set(default_names) - set(names),
        "selector-free list restores the default view": observed.get("restoredView") == "default",
    }
    failed = [name for name, ok in checks.items() if not ok]
    drift = "".join(f"; {label} {values}" for label, values in (("missing", missing), ("not pinned", extra)) if values)
    return not failed, (f"full catalog drained over {observed.get('pages')} pages: {len(names)} tools"
                        + (f"; failed: {', '.join(failed)}{drift}" if failed
                           else f"; equals the {len(roster)}-tool pinned roster, includes the default view, view restored"))


def oracle_mcp_job_accepted(observed: dict[str, Any]) -> tuple[bool, str]:
    job_id, status = observed.get("jobId"), observed.get("status")
    ok = isinstance(job_id, str) and bool(job_id) and status in {"Queued", "Provisioning", "Running", "Succeeded"}
    return ok, f"job submitted with status {status!r}" if ok else f"no job (status {status!r})"


def oracle_mcp_job_succeeded(observed: dict[str, Any]) -> tuple[bool, str]:
    status = observed.get("status")
    return status == "Succeeded", f"terminal job status {status!r}"


def oracle_mcp_process_catalog(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    processes = observed.get("processes")
    if not isinstance(processes, list) or not processes or not all(isinstance(p, dict) for p in processes):
        return False, "missing process catalog"
    ids = [p.get("processId") for p in processes]
    if not all(isinstance(pid, str) and pid for pid in ids) or len(set(ids)) != len(ids):
        return False, "duplicate or missing process identifiers"
    selected = next((p for p in processes if p["processId"] == fixture["processes"]["processId"]), {})
    parameters = selected.get("parameters") or []
    names = {p.get("name") for p in parameters if isinstance(p, dict)}
    ok = (selected.get("executionKind") == "Job" and "Async" in (selected.get("supportedExecutionModes") or [])
          and {"wkb", "srid", "distance", "geodesic"} <= names)
    return ok, "catalog must describe the fixture buffer's asynchronous entry point and inputs"


def oracle_mcp_dry_run(observed: dict[str, Any]) -> tuple[bool, str]:
    ok = ("FeatureLayer" in (observed.get("estimatedArtifacts") or [])
          and isinstance(observed.get("sideEffects"), list) and not observed.get("jobId"))
    return ok, "dry run must preview a FeatureLayer without reporting an executed job"


def oracle_mcp_bound_job(observed: dict[str, Any], submitted: dict[str, Any], *, terminal: bool = False) -> tuple[bool, str]:
    job = submitted.get("jobId")
    ok = isinstance(job, str) and bool(job) and observed.get("jobId") == job
    accepted, _ = oracle_mcp_job_accepted(observed)
    ok = ok and accepted and (not terminal or observed.get("status") == "Succeeded")
    return ok, "job identity must match submission and status must be " + ("Succeeded" if terminal else "accepted")


def oracle_mcp_buffer_result(observed: dict[str, Any], fixture: dict[str, Any], submitted: dict[str, Any]) -> tuple[bool, str]:
    job, artifact = submitted.get("jobId"), observed.get("artifactId")
    if not (isinstance(job, str) and job and observed.get("jobId") == job
            and observed.get("status") == "Completed" and observed.get("errors") == []
            and observed.get("artifactsTruncated") is False and isinstance(artifact, str) and artifact):
        return False, "result must be complete, error-free, identified and bound to the submitted job"
    feature = observed.get("geometry") or {}
    properties = feature.get("properties") or {}
    if (properties.get("inputSrid") != fixture["processes"]["srid"]
            or properties.get("processId") != fixture["processes"]["processId"]):
        return False, "buffer metadata must identify the fixture process and CRS"
    return oracle_buffer(observed, fixture)
