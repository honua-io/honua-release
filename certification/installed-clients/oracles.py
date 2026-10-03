"""Oracles for the SDK regression scenarios.

Every expected value is derived from ``fixture.v1.json``. A driver reports what the SDK returned
(an observation); an oracle compares it with the fixture and returns ``(passed, summary)``. The
summary is built here from fixture-derived numbers and ids only, so it is safe to retain in a
receipt: it never carries a response body, a credential or a message the server wrote.
"""
from __future__ import annotations

import base64
import math
import struct
import zlib
from typing import Any, Callable

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
    vertices = ring[:-1] if ring[0] == ring[-1] else ring
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


def decode_png(data: bytes) -> tuple[int, int, Callable[[int, int], tuple[int, int, int, int]]]:
    """Decode an 8-bit, non-interlaced PNG into an RGBA pixel accessor (stdlib only)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    offset, idat, palette, transparency, header = 8, b"", b"", b"", None
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        kind, chunk = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"PLTE":
            palette = chunk
        elif kind == b"tRNS":
            transparency = chunk
    if header is None:
        raise ValueError("PNG has no header")
    width, height, depth, color, _, _, interlace = header
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if depth != 8 or interlace or channels is None:
        raise ValueError(f"unsupported PNG (depth {depth}, color type {color}, interlace {interlace})")
    raw, stride, rows, previous, position = zlib.decompress(idat), width * channels, [], bytearray(width * channels), 0
    for _ in range(height):
        kind, line = raw[position], bytearray(raw[position + 1:position + 1 + stride])
        position += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif kind == 4:
                estimate = left + up - corner
                pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 0xFF
        rows.append(bytes(line))
        previous = line

    def pixel(x: int, y: int) -> tuple[int, int, int, int]:
        row, start = rows[y], x * channels
        if color == 6:
            return tuple(row[start:start + 4])  # type: ignore[return-value]
        if color == 2:
            return (*row[start:start + 3], 255)  # type: ignore[return-value]
        if color == 3:
            index = row[start]
            alpha = transparency[index] if index < len(transparency) else 255
            return (*palette[index * 3:index * 3 + 3], alpha)  # type: ignore[return-value]
        if color == 0:
            return (row[start],) * 3 + (255,)  # type: ignore[return-value]
        return (row[start],) * 3 + (row[start + 1],)  # type: ignore[return-value]

    return width, height, pixel


def oracle_raster_tile(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    try:
        data = base64.b64decode(observed.get("bytes") or "", validate=True)
        width, height, pixel = decode_png(data)
    except (ValueError, zlib.error, struct.error) as exc:
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
    return ok, (f"publication recorded as a proposal awaiting approval (status {status!r})" if ok
                else f"status {status!r}, requiresApproval {observed.get('requiresApproval')!r}, proposal id present: {bool(proposal)}; "
                     "the fixture's approval policy requires a proposal")


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


def oracle_mcp_view(observed: dict[str, Any], fixture: dict[str, Any], view: str) -> tuple[bool, str]:
    count = fixture["mcp"]["views"][view]["toolCount"]
    names = observed.get("names") if isinstance(observed.get("names"), list) else []
    got_view, revision, meta_count = observed.get("view"), observed.get("revision"), observed.get("toolCount")
    ok = (got_view == view and isinstance(revision, str) and bool(revision) and meta_count == count
          and len(names) == count == len(set(names)) and observed.get("nextCursor") is None)
    return ok, (f"selector-free tools/list returned view {got_view!r} ({revision}) with {len(names)} tools "
                f"(metadata {meta_count}); the fixture expects the complete {view!r} view with {count}")


def oracle_mcp_permission_denied(observation: dict[str, Any]) -> tuple[bool, str]:
    if "error" not in observation:
        return False, "an anonymous session received the full catalog"
    kind = observation["error"].get("type")
    return kind == "permission_denied", f"anonymous full-view request refused with {kind!r} (expected 'permission_denied')"


def oracle_mcp_full_catalog(observed: dict[str, Any], fixture: dict[str, Any], default_names: list[str] | None) -> tuple[bool, str]:
    names = observed.get("names") if isinstance(observed.get("names"), list) else []
    setup = fixture["mcp"]["views"]["setup"]["toolCount"]
    missing = sorted(set(default_names or []) - set(names))
    checks = {
        "unique": len(names) == len(set(names)),
        f"more than the {setup} setup tools": len(names) > setup,
        "includes the default view": bool(default_names) and not missing,
        "selector-free list restores the default view": observed.get("restoredView") == "default",
    }
    failed = [name for name, ok in checks.items() if not ok]
    return not failed, (f"full catalog drained over {observed.get('pages')} pages: {len(names)} tools"
                        + (f"; failed: {', '.join(failed)}" if failed else "; includes the default view, unique, view restored"))


def render_expectations(fixture: dict[str, Any]) -> list[tuple[int, int, bool]]:
    """(x, y, painted) per render sample, from the area envelopes; pixels near an edge are skipped."""
    spec = fixture["mcp"]["render"]
    minx, miny, maxx, maxy = spec["bbox"]
    sx, sy = (maxx - minx) / spec["width"], (maxy - miny) / spec["height"]
    margin = spec["edgeMarginPixels"]
    boxes = [((e[0] - minx) / sx, (maxy - e[3]) / sy, (e[2] - minx) / sx, (maxy - e[1]) / sy)
             for e in (feature["envelope"] for feature in fixture["area"]["features"])]
    result = []
    for x, y in spec["samples"]:
        cx, cy = x + 0.5, y + 0.5
        inside = any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in boxes)
        near = any(x0 - margin <= cx <= x1 + margin and y0 - margin <= cy <= y1 + margin
                   and not (x0 + margin <= cx <= x1 - margin and y0 + margin <= cy <= y1 - margin) for x0, y0, x1, y1 in boxes)
        if not near:
            result.append((x, y, inside))
    return result


def oracle_map_render(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    spec = fixture["mcp"]["render"]
    try:
        width, height, pixel = decode_png(base64.b64decode(observed.get("png") or "", validate=True))
    except (ValueError, zlib.error, struct.error) as exc:
        return False, f"the rendered map is not a decodable PNG ({type(exc).__name__}); mime type {observed.get('mimeType')!r}"
    if (width, height) != (spec["width"], spec["height"]):
        return False, f"rendered map is {width}x{height}, the fixture requested {spec['width']}x{spec['height']}"
    samples = render_expectations(fixture)
    mismatches = [f"({x},{y}) alpha {pixel(x, y)[3]} expected {'painted' if painted else 'transparent'}"
                  for x, y, painted in samples if (pixel(x, y)[3] > 0) != painted]
    painted = sum(1 for *_, inside in samples if inside)
    if not samples or not painted or painted == len(samples):
        return False, "the fixture needs painted and transparent sample pixels away from the polygon edge"
    if mismatches:
        return False, f"{len(mismatches)} of {len(samples)} sample pixels disagree with the fixture polygon: {mismatches}"
    return True, f"{painted} painted and {len(samples) - painted} transparent sample pixels match the fixture polygon"


def oracle_mcp_job_accepted(observed: dict[str, Any]) -> tuple[bool, str]:
    job_id, status = observed.get("jobId"), observed.get("status")
    ok = isinstance(job_id, str) and bool(job_id) and status in {"Queued", "Provisioning", "Running", "Succeeded"}
    return ok, f"job submitted with status {status!r}" if ok else f"no job (status {status!r})"


def oracle_mcp_job_succeeded(observed: dict[str, Any]) -> tuple[bool, str]:
    status = observed.get("status")
    return status == "Succeeded", f"terminal job status {status!r}"
