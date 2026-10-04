"""Contracts, oracles and evaluation for the cross-client interop scenarios (``interop-*``).

An interop scenario hands one piece of work from client to client: each step names the client
artifact that performs it, the exact SDK API, command or MCP request it goes through, and the
earlier steps whose state it consumes. Every expectation is computed from ``fixture.v1.json`` in
this directory, never from a server response. When a consuming step fails, its oracle summary
names the seam: the producing client and API, and the consuming client and API.
"""
from __future__ import annotations

import base64
import json
import math
import re
import struct
from pathlib import Path
from typing import Any, Callable

import oracles

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture.v1.json"
# The client artifacts an interop step may name.
CLIENTS = ("honua-sdk-python-wheel", "honua-sdk-js", "honua-sdk-dotnet", "honua-mcp-server")
RECEIPT_FIELDS = ["client", "version", "integrity", "scenario", "step", "api", "status", "oracle"]
# Oracles that judge the whole observation, because the expected outcome is an error.
ERROR_ORACLES = {"self-approval-refused"}
REFUSED_STATUSES = oracles.REFUSED_STATUSES
# MCP refusal codes: an authentication or authorization denial, as a JSON-RPC error or a tool error.
MCP_REFUSALS = {"permission_denied", "unauthenticated", "unauthorized", "forbidden"}


class InteropError(RuntimeError):
    pass


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(path.read_text())


# ── contract ─────────────────────────────────────────────────────────────────────────────────


def validate_scenario(scenario: dict[str, Any], name: str) -> None:
    """An interop step names its client, its API and the earlier steps it consumes."""
    if scenario.get("schemaVersion") != 1 or not re.fullmatch(r"interop-[a-z0-9-]+", str(scenario.get("id", ""))):
        raise InteropError(f"{name}: interop scenario needs schemaVersion 1 and an interop-* id")
    if "clients" in scenario:
        raise InteropError(f"{name}: an interop scenario names a client per step, not a clients map")
    if scenario.get("receiptFields") != RECEIPT_FIELDS:
        raise InteropError(f"{name}: receiptFields must be the allowlist")
    steps = scenario.get("steps")
    if not isinstance(steps, list) or len(steps) < 2:
        raise InteropError(f"{name}: an interop scenario hands work between at least two steps")
    seen: list[str] = []
    for step in steps:
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id or step_id in seen:
            raise InteropError(f"{name}: steps need unique ids")
        if step.get("oracle") not in ORACLES:
            raise InteropError(f"{name}: step {step_id} needs a known oracle")
        if step.get("client") not in CLIENTS or not isinstance(step.get("api"), str) or not step["api"]:
            raise InteropError(f"{name}: step {step_id} must name its client artifact and API")
        consumes = step.get("consumes")
        if not isinstance(consumes, list) or any(
                not isinstance(item, dict) or set(item) != {"step", "handoff"} or item["step"] not in seen
                or not isinstance(item["handoff"], str) or not item["handoff"] for item in consumes):
            raise InteropError(f"{name}: step {step_id} may only consume earlier steps, each with a named handoff")
        seen.append(step_id)
    if len({step["client"] for step in steps}) < 2:
        raise InteropError(f"{name}: an interop scenario must cross at least two clients")
    if not any(step["consumes"] and any(scenario_step(scenario, item["step"])["client"] != step["client"]
                                        for item in step["consumes"]) for step in steps):
        raise InteropError(f"{name}: no step consumes state another client produced")


def scenario_step(scenario: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(step for step in scenario["steps"] if step["id"] == step_id)


def first_client(scenario: dict[str, Any]) -> str:
    """The client that starts the hand-off: the matrix cell's artifact."""
    return scenario["steps"][0]["client"]


# ── fixture-derived expectations ─────────────────────────────────────────────────────────────


def edited_handoff(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """The handoff rows after the JS SDK's edit (attributes and geometry)."""
    handoff = fixture["handoff"]
    rows = {row["gid"]: dict(row) for row in handoff["features"]}
    edit = handoff["edit"]
    rows[edit["gid"]].update(edit["attributes"], x=edit["x"], y=edit["y"])
    return [rows[gid] for gid in sorted(rows)]


def envelope_ring(envelope: list[float]) -> list[tuple[float, float]]:
    minx, miny, maxx, maxy = envelope
    return [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]


def bound_map_body(fixture: dict[str, Any], sites_collection_id: str) -> Any:
    """The proposal's map body with the fixture's sites collection bound in."""
    def bind(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: bind(item) for key, item in value.items()}
        if isinstance(value, list):
            return [bind(item) for item in value]
        if isinstance(value, str):
            return value.replace("{sitesCollectionId}", sites_collection_id)
        return value
    return bind(fixture["proposal"]["body"])


def distance_to_envelope(x: float, y: float, envelope: list[float]) -> float:
    minx, miny, maxx, maxy = envelope
    dx = max(minx - x, 0.0, x - maxx)
    dy = max(miny - y, 0.0, y - maxy)
    return math.hypot(dx, dy)


def _coordinate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _closed_ring(ring: Any) -> list[tuple[float, float]] | None:
    """Exterior vertices of a closed ring, or None when the ring is not a closed coordinate sequence."""
    if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
        return None
    points: list[tuple[float, float]] = []
    for vertex in ring[:-1]:
        if not isinstance(vertex, (list, tuple)) or len(vertex) < 2:
            return None
        x, y = _coordinate(vertex[0]), _coordinate(vertex[1])
        if x is None or y is None:
            return None
        points.append((x, y))
    return points


def _same_ring(observed: list[tuple[float, float]], expected: list[tuple[float, float]]) -> bool:
    """The fixture ring, modulo rotation and reversal. Any other order is a different polygon."""
    count = len(expected)
    if len(observed) != count or count == 0:
        return False

    def matches(seq: list[tuple[float, float]]) -> bool:
        for shift in range(count):
            rotated = seq[shift:] + seq[:shift]
            if all(oracles._close(gx, wx) and oracles._close(gy, wy) for (gx, gy), (wx, wy) in zip(rotated, expected)):
                return True
        return False

    return matches(observed) or matches(list(reversed(observed)))


def _orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_cross(ax: float, ay: float, bx: float, by: float, cx: float, cy: float, dx: float, dy: float) -> bool:
    o1, o2 = _orient(ax, ay, bx, by, cx, cy), _orient(ax, ay, bx, by, dx, dy)
    o3, o4 = _orient(cx, cy, dx, dy, ax, ay), _orient(cx, cy, dx, dy, bx, by)

    def on_segment(px: float, py: float, qx: float, qy: float, rx: float, ry: float) -> bool:
        return (min(px, qx) <= rx <= max(px, qx) and min(py, qy) <= ry <= max(py, qy)
                and abs(_orient(px, py, qx, qy, rx, ry)) <= 1e-9)

    proper = (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0) and 0 not in (o1, o2, o3, o4)
    return proper or ((o1 == 0 and on_segment(ax, ay, bx, by, cx, cy)) or (o2 == 0 and on_segment(ax, ay, bx, by, dx, dy))
                      or (o3 == 0 and on_segment(cx, cy, dx, dy, ax, ay)) or (o4 == 0 and on_segment(cx, cy, dx, dy, bx, by)))


def _segment_hits_rectangle(x1: float, y1: float, x2: float, y2: float, envelope: list[float]) -> bool:
    minx, miny, maxx, maxy = envelope
    if (minx <= x1 <= maxx and miny <= y1 <= maxy) or (minx <= x2 <= maxx and miny <= y2 <= maxy):
        return True
    edges = ((minx, miny, maxx, miny), (maxx, miny, maxx, maxy), (maxx, maxy, minx, maxy), (minx, maxy, minx, miny))
    return any(_segments_cross(x1, y1, x2, y2, *edge) for edge in edges)


def _point_to_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _segment_min_distance(x1: float, y1: float, x2: float, y2: float, envelope: list[float]) -> float:
    """Minimum distance from a straight edge to an axis-aligned rectangle."""
    if _segment_hits_rectangle(x1, y1, x2, y2, envelope):
        return 0.0
    minx, miny, maxx, maxy = envelope
    gap = min(distance_to_envelope(x1, y1, envelope), distance_to_envelope(x2, y2, envelope))
    for cx, cy in ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)):
        gap = min(gap, _point_to_segment(cx, cy, x1, y1, x2, y2))
    return gap


def _edge_follows_buffer(x1: float, y1: float, x2: float, y2: float, envelope: list[float],
                         distance: float, tolerance: float) -> bool:
    """A buffer edge stays on the offset, or is a chord of one corner's quarter-circle fillet.

    Straight sides sit at ``distance``. A fillet chord may bow inward by at most the sagitta of a
    circle of radius ``distance``, and only across a quarter turn (the corner of a rectangle).
    A longer edge that cuts inside the offset is not a buffer boundary.
    """
    gap = _segment_min_distance(x1, y1, x2, y2, envelope)
    if gap >= distance - tolerance:
        return True
    length = math.hypot(x2 - x1, y2 - y1)
    half = length / 2
    if half >= distance or length > distance * math.sqrt(2) + tolerance:
        return False
    sagitta = distance - math.sqrt(distance * distance - half * half)
    return gap >= distance - sagitta - tolerance


def _auth_refusal(status: Any) -> bool:
    """401, 403, 499, or an MCP authentication/authorization denial. A bare boolean is not a status."""
    return status in REFUSED_STATUSES or status in MCP_REFUSALS


def polygon_wkb(rings: list[list[list[float]]]) -> str:
    """Little-endian WKB for a polygon, base64-encoded (the geometry.buffer input encoding)."""
    out = struct.pack("<BII", 1, 3, len(rings))
    for ring in rings:
        out += struct.pack("<I", len(ring)) + b"".join(struct.pack("<dd", float(x), float(y)) for x, y, *_ in ring)
    return base64.b64encode(out).decode()


# ── oracles ──────────────────────────────────────────────────────────────────────────────────


def _layer_published(observed: dict[str, Any], want: dict[str, Any], geometry: str | None = None) -> tuple[bool, str]:
    got = (observed.get("serviceName"), observed.get("layerName"), observed.get("enabled"))
    expected = (want["service"], want["layerName"], True)
    ok = got == expected and type(observed.get("layerId")) is int
    if geometry is not None:
        ok = ok and observed.get("geometryType") == geometry
    return ok, (f"published service/layer/enabled {got} (integer id: {type(observed.get('layerId')) is int}"
                + (f", geometry {observed.get('geometryType')!r}" if geometry else "") + f"), fixture expects {expected}")


def _key_count(observed: dict[str, Any], fixture: dict[str, Any], plan: dict[str, Any]) -> tuple[bool, str]:
    return oracles.oracle_count(observed, plan["identity"]["siteCount"])


def oracle_imported(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    count, table = observed.get("featureCount"), observed.get("physicalTableName")
    want = len(fixture["area"]["features"])
    ok = count == want and observed.get("success") is True and isinstance(table, str) and bool(table)
    return ok, f"import reported success={observed.get('success')!r}, {count!r} features into a named table: {bool(table)}; fixture has {want}"


def _area_attributes(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    got = observed.get("attributes")
    want = [{"gid": feature["gid"], "name": feature["name"]} for feature in fixture["area"]["features"]]
    if not isinstance(got, list) or len(got) != len(want):
        return False, f"{len(got) if isinstance(got, list) else 'no'} attribute rows, fixture expects {len(want)}"
    for index, (row, expected) in enumerate(zip(got, want)):
        if not isinstance(row, dict) or any(row.get(field) != expected[field] for field in ("gid", "name")):
            return False, f"feature {index} attributes {row!r} differ from the fixture {expected}"
    return True, "gid and name equal the fixture (" + ", ".join(str(row["name"]) for row in want) + ")"


def oracle_area_features(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    rings = observed.get("rings")
    want = [envelope_ring(feature["envelope"]) for feature in fixture["area"]["features"]]
    if not isinstance(rings, list) or len(rings) != len(want):
        return False, f"{len(rings) if isinstance(rings, list) else 'no'} polygons, fixture expects {len(want)}"
    for ring, expected in zip(rings, want):
        points = _closed_ring(ring)
        if points is None:
            return False, "a polygon has no closed exterior ring"
        if not _same_ring(points, expected):
            return False, (f"exterior ring {points} does not preserve the fixture envelope {expected} "
                           "(only a rotation or reversal of that ring matches)")
    attributes_ok, attributes = _area_attributes(observed, fixture)
    if not attributes_ok:
        return False, attributes
    return True, f"{len(want)} polygon(s) preserve the fixture envelope ring; {attributes}"


def oracle_area_render(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    area = fixture["area"]
    return oracles.oracle_map_render(observed, {"area": {"features": area["features"]}, "mcp": {"render": area["render"]}})


def oracle_area_buffer(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    spec = fixture["area"]["buffer"]
    envelope = fixture["area"]["features"][0]["envelope"]
    distance, tolerance = spec["distance"], spec["tolerance"]
    ring = oracles._exterior_ring(observed.get("geometry"))
    if ring is None:
        return False, "the job result holds no polygon geometry"
    if ring[0] != ring[-1]:
        return False, "the job result polygon is not closed"
    points = _closed_ring(ring)
    if points is None:
        return False, "the job result polygon has a vertex that is not a coordinate"
    worst = max(abs(distance_to_envelope(x, y, envelope) - distance) for x, y in points)
    xs, ys = [x for x, _ in points], [y for _, y in points]
    bounds = (min(xs), min(ys), max(xs), max(ys))
    want = (envelope[0] - distance, envelope[1] - distance, envelope[2] + distance, envelope[3] + distance)
    bounded = all(oracles._close(got, expected, tolerance) for got, expected in zip(bounds, want))
    edges = all(_edge_follows_buffer(x1, y1, x2, y2, envelope, distance, tolerance)
                for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]))
    ok = worst <= tolerance and bounded and edges
    return ok, (f"{len(points)}-vertex closed ring: max offset error {worst:.2e} from the fixture polygon at distance "
                f"{distance} (tolerance {tolerance:.0e}); bounds {'equal' if bounded else 'differ from'} the polygon expanded by {distance}; "
                f"edges {'follow the offset' if edges else 'leave the offset (a vertex check is not a buffer)'}")


def oracle_draft_valid(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    family, status = observed.get("family"), observed.get("validation")
    ids = all(isinstance(observed.get(key), str) and observed[key] for key in ("draftId", "itemId"))
    want = fixture["proposal"]["envelope"]["family"]
    return str(family or "").lower() == want and status == "valid" and ids, f"draft family {family!r}, validation {status!r}, ids present: {ids}"


def oracle_version_saved(observed: dict[str, Any], plan_draft: dict[str, Any] | None) -> tuple[bool, str]:
    version, digest = observed.get("versionId"), observed.get("contentHash")
    same_item = bool(plan_draft) and observed.get("itemId") == plan_draft.get("itemId")
    ok = isinstance(version, str) and bool(version) and isinstance(digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", digest)) and same_item
    return ok, f"immutable version saved: version id present {bool(version)}, sha-256 content hash {bool(digest)}, same item as the draft {same_item}"


def oracle_publication_proposed(observed: dict[str, Any]) -> tuple[bool, str]:
    proposal = observed.get("proposalId")
    ok = isinstance(proposal, str) and proposal.startswith("proposal-") and observed.get("published") is not True
    return ok, f"publication recorded as proposal (id present: {bool(proposal)}), published before approval: {observed.get('published')!r}"


def oracle_proposal_resolved(observed: dict[str, Any], principals: dict[str, Any]) -> tuple[bool, str]:
    """The Studio publication proposal names the proposer as requester and the separate approver as resolver."""
    proposer, approver = principals.get("proposerId"), principals.get("approverId")
    requested, resolved = str(observed.get("requestedBy") or ""), str(observed.get("resolvedBy") or "")
    checks = {
        "status Succeeded": observed.get("status") == "Succeeded",
        "kind StudioDraftMutation": observed.get("kind") == "StudioDraftMutation",
        "requested by the proposer": bool(proposer) and requested.endswith(str(proposer)),
        "resolved by the approver": bool(approver) and resolved.endswith(str(approver)),
        "two principals": bool(requested) and bool(resolved) and requested != resolved,
    }
    failed = [name for name, ok in checks.items() if not ok]
    return not failed, ("proposal succeeded, requested by the proposer and resolved by the separate approver" if not failed
                        else f"proposal status {observed.get('status')!r} kind {observed.get('kind')!r}; failed: {', '.join(failed)}")


def oracle_publication_active(observed: dict[str, Any], plan: dict[str, Any]) -> tuple[bool, str]:
    if not observed.get("requestId"):
        return False, "the publication request id is absent from the create response, so the request cannot be polled"
    state, url = observed.get("state"), observed.get("publicationUrl")
    route = plan["proposal"]["route"]
    ok = state == "Active" and isinstance(url, str) and url.rstrip("/").endswith(route)
    return ok, f"publication state {state!r}, URL at the fixture route: {isinstance(url, str) and url.rstrip('/').endswith(route)}"


def oracle_published_pointer(observed: dict[str, Any], saved: dict[str, Any] | None) -> tuple[bool, str]:
    want = (saved or {}).get("versionId")
    got = observed.get("publishedVersionId")
    return bool(want) and got == want, f"published pointer {'is' if got == want and want else 'is not'} the version the JS SDK saved"


def oracle_published_content(observed: dict[str, Any], fixture: dict[str, Any], plan: dict[str, Any],
                             saved: dict[str, Any] | None) -> tuple[bool, str]:
    body = bound_map_body(fixture, plan["sites"]["collectionId"])
    checks = {
        "family map": str(observed.get("family") or "").lower() == fixture["proposal"]["envelope"]["family"],
        "content hash equals the saved version's": bool(saved) and observed.get("contentHash") == (saved or {}).get("contentHash"),
        "map body equals the fixture": observed.get("body") == body,
    }
    failed = [name for name, ok in checks.items() if not ok]
    return not failed, ("published version's family, content hash and map body equal the fixture and the saved version"
                        if not failed else f"published version differs: failed {', '.join(failed)}")


def oracle_published_url(observed: dict[str, Any], fixture: dict[str, Any], plan: dict[str, Any],
                         saved: dict[str, Any] | None) -> tuple[bool, str]:
    ok, detail = oracle_published_content(observed, fixture, plan, saved)
    return ok, f"final publication URL: {detail}"


def oracle_key_minted(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    want = sorted(fixture["identity"]["permissions"])
    checks = {
        "key id": isinstance(observed.get("keyId"), str) and bool(observed.get("keyId")),
        "active": observed.get("status") == "active",
        "fixture permissions": sorted(observed.get("permissions") or []) == want,
        "secret written to a private file": observed.get("secretWritten") is True and observed.get("secretPrivate") is True,
    }
    failed = [name for name, ok in checks.items() if not ok]
    return not failed, (f"key minted with permissions {want}, secret only in a private file" if not failed
                        else f"minted key failed: {', '.join(failed)}")


def oracle_key_revoked(observed: dict[str, Any]) -> tuple[bool, str]:
    ok = observed.get("status") == "revoked" and bool(observed.get("revokedAt"))
    return ok, f"key status {observed.get('status')!r} after revoke, revokedAt present: {bool(observed.get('revokedAt'))}"


def oracle_revocation_observed(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    """Window 0: the first call after the revoke command returns is refused, and the client stays refused."""
    revocation = fixture["identity"]["revocation"]
    window = revocation["windowSeconds"]
    successes = observed.get("succeededAfterRevocation")
    if observed.get("timedOut"):
        return False, (f"the client did not answer within {observed.get('timeoutSeconds')}s of its first call after revocation "
                       f"(no refusal surfaced; {successes!r} calls succeeded first); documented window {window}s")
    refused, status = observed.get("refused") is True, observed.get("status")
    confirmations = observed.get("confirmations") if isinstance(observed.get("confirmations"), list) else []
    after = observed.get("refusedAfterSeconds")
    if not refused:
        return False, (f"still authenticated {observed.get('observationSeconds')}s after revocation "
                       f"({successes!r} calls succeeded); documented window {window}s")
    bound = revocation["observationSeconds"]
    # The fixture bound, not the value the client reports: a late 401 is a hang, not a refusal in window.
    on_time = type(after) in (int, float) and 0 <= after <= bound
    in_window = successes == 0 and on_time
    held = len(confirmations) == revocation["confirmations"] and all(_auth_refusal(item) for item in confirmations)
    refusal = _auth_refusal(status)
    ok = in_window and held and refusal
    refused_n = sum(1 for item in confirmations if _auth_refusal(item))
    when = f"within the {bound}s observation bound" if on_time else f"outside the {bound}s observation bound"
    return ok, (f"refused ({status!r}) after {successes!r} successful call(s) at {after!r}s ({when}; documented window "
                f"{window}s: the first call must be refused before the observation bound); {refused_n} of "
                f"{revocation['confirmations']} confirmation calls refused with an authentication status")


def _saved(observations: dict[tuple[str, str], dict[str, Any]], scenario: str) -> dict[str, Any] | None:
    observed = observations.get((scenario, "save-version"), {}).get("observed")
    return observed if isinstance(observed, dict) else None


def _draft(observations: dict[tuple[str, str], dict[str, Any]], scenario: str) -> dict[str, Any] | None:
    observed = observations.get((scenario, "create-draft"), {}).get("observed")
    return observed if isinstance(observed, dict) else None


ORACLES: dict[str, Callable[..., tuple[bool, str]]] = {
    "interop-layer-published": lambda o, f, p, obs, s: _layer_published(o, p["handoff"]),
    "interop-handoff-features": lambda o, f, p, obs, s: oracles._compare_features(o, f["handoff"]["features"], ["gid", "name", "rank"], "published layer"),
    "interop-handoff-ids": lambda o, f, p, obs, s: oracles.oracle_object_ids(o, [row["gid"] for row in f["handoff"]["features"]]),
    "interop-handoff-count": lambda o, f, p, obs, s: oracles.oracle_count(o, len(f["handoff"]["features"])),
    "interop-edit-success": lambda o, f, p, obs, s: oracles.oracle_edit_success(o),
    "interop-edited-features": lambda o, f, p, obs, s: oracles._compare_features(o, edited_handoff(f), ["gid", "name", "rank"], "edited layer"),
    "interop-imported": lambda o, f, p, obs, s: oracle_imported(o, f),
    "interop-area-published": lambda o, f, p, obs, s: _layer_published(o, p["area"], f["area"]["geometryType"]),
    "interop-area-features": lambda o, f, p, obs, s: oracle_area_features(o, f),
    "interop-area-render": lambda o, f, p, obs, s: oracle_area_render(o, f),
    "interop-mcp-job-accepted": lambda o, f, p, obs, s: oracles.oracle_mcp_job_accepted(o),
    "interop-job-succeeded": lambda o, f, p, obs, s: oracles.oracle_job_succeeded(o),
    "interop-area-buffer": lambda o, f, p, obs, s: oracle_area_buffer(o, f),
    "interop-draft-valid": lambda o, f, p, obs, s: oracle_draft_valid(o, f),
    "interop-version-saved": lambda o, f, p, obs, s: oracle_version_saved(o, _draft(obs, s)),
    "interop-publication-proposed": lambda o, f, p, obs, s: oracle_publication_proposed(o),
    "self-approval-refused": lambda o, f, p, obs, s: oracles.oracle_self_approval_refused(o),
    "proposal-approved": lambda o, f, p, obs, s: oracles.oracle_proposal_approved(o),
    "interop-proposal-resolved": lambda o, f, p, obs, s: oracle_proposal_resolved(o, p.get("principals") or {}),
    "interop-publication-active": lambda o, f, p, obs, s: oracle_publication_active(o, p),
    "interop-published-pointer": lambda o, f, p, obs, s: oracle_published_pointer(o, _saved(obs, s)),
    "interop-published-content": lambda o, f, p, obs, s: oracle_published_content(o, f, p, _saved(obs, s)),
    "interop-published-url": lambda o, f, p, obs, s: oracle_published_url(o, f, p, _saved(obs, s)),
    "interop-key-minted": lambda o, f, p, obs, s: oracle_key_minted(o, f),
    "interop-key-count": lambda o, f, p, obs, s: _key_count(o, f, p),
    "interop-key-revoked": lambda o, f, p, obs, s: oracle_key_revoked(o),
    "interop-revocation-observed": lambda o, f, p, obs, s: oracle_revocation_observed(o, f),
}


# ── evaluation ───────────────────────────────────────────────────────────────────────────────


def seam(scenario: dict[str, Any], step: dict[str, Any], labels: dict[str, str]) -> str:
    """The hand-offs into a step whose producer is a different client, named client and API on both sides."""
    parts = []
    for item in step["consumes"]:
        producer = scenario_step(scenario, item["step"])
        if producer["client"] == step["client"]:
            continue
        parts.append(f"{item['handoff']} from {labels[producer['client']]} `{producer['api']}` ({producer['id']})")
    if not parts:
        return ""
    return f"seam {' and '.join(parts)} -> {labels[step['client']]} `{step['api']}`: "


def judge_step(scenario_id: str, step: dict[str, Any], observation: dict[str, Any] | None, fixture: dict[str, Any],
               plan: dict[str, Any], observations: dict[tuple[str, str], dict[str, Any]]) -> tuple[bool, str]:
    if observation is None:
        return False, "the driver produced no observation for this step (missing scenario step)"
    if "unsupported" in observation:
        return False, f"unsupported: the published client has no API for this step ({observation['unsupported']})"
    if "skipped" in observation:
        return False, f"not run: {observation['skipped']}"
    kind = step["oracle"]
    if kind in ERROR_ORACLES:
        return ORACLES[kind](observation, fixture, plan, observations, scenario_id)
    if "error" in observation:
        return False, oracles._error_summary(observation)
    observed = observation.get("observed")
    if not isinstance(observed, dict):
        return False, "the driver reported no observed value"
    return ORACLES[kind](observed, fixture, plan, observations, scenario_id)


def evaluate_cell(cell: dict[str, Any], scenario: dict[str, Any], observations: dict[tuple[str, str], dict[str, Any]],
                  fixture: dict[str, Any], plan: dict[str, Any], labels: dict[str, str],
                  scrub: Callable[[str], str]) -> tuple[str, str, list[dict[str, Any]]]:
    """Judge every step against the matrix; returns (status, detail, step rows).

    Each row carries the client that performed the step. A failing step that consumed another
    client's state is summarised as a seam: producer client and API, consumer client and API.
    """
    blocked = cell.get("blockedSteps") or {}
    rows: list[dict[str, Any]] = []
    for step in scenario["steps"]:
        observation = observations.get((scenario["id"], step["id"]))
        passed, summary = judge_step(scenario["id"], step, observation, fixture, plan, observations)
        if observation is not None and observation.get("api") != step["api"]:
            passed, summary = False, f"driver used {observation.get('api')!r}, the contract names {step['api']!r}"
        if not passed:
            summary = seam(scenario, step, labels) + summary
        status = "pass" if passed else "fail"
        row = {"step": step["id"], "client": labels[step["client"]], "api": step["api"], "status": status}
        entry = blocked.get(step["id"])
        if entry:
            row["blockedBy"] = entry["blockedBy"]
            if passed:
                status = "fail"
                summary = f"{summary}; the matrix marks this step blocked by {entry['blockedBy']}: set it active"
            elif re.search(entry["signature"], summary):
                status = "blocked"
            else:
                summary = f"{summary}; the declared blocker signature /{entry['signature']}/ was not observed"
        row.update(status=status, oracle=scrub(summary))
        rows.append(row)
    failed = [row for row in rows if row["status"] == "fail"]
    if failed:
        return "fail", "; ".join(f"{row['client']} `{row['api']}` {row['step']}: {row['oracle']}" for row in failed), rows
    blocked_rows = [row for row in rows if row["status"] == "blocked"]
    if blocked_rows:
        return "blocked", (f"{len(rows) - len(blocked_rows)} steps pass; blocked as declared: "
                           + ", ".join(f"{row['step']} ({row['blockedBy']})" for row in blocked_rows)), rows
    clients = sorted({row["client"] for row in rows})
    return "pass", f"{len(rows)} steps pass across {', '.join(clients)} against the fixture oracles", rows

