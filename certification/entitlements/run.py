#!/usr/bin/env python3
"""Entitlement certification: exact positive and negative assertions per gated capability.

`observe` drives one candidate stack booted with one fixture license (fixtures.py):

  - the entitled stack must answer every probe with the capability's documented success
    (status, media type and body shape) — a 404, 500 or validation error is a failure, not "allowed";
  - a negative stack (a license without the probed key) must answer with exactly the documented
    denial: HTTP 402 problem+json, or the GeoServices 200-with-error.code-402 envelope, naming the
    probed entitlement key and its required edition.

`verdict` joins the observations into one report. Every gated key in the catalog must have exactly
one probe or a documented no-HTTP-surface reason, and the live candidate catalog must equal the
committed one, so a new entitlement without a row fails the lane. A probe whose success needs
infrastructure the lane does not provision reports blocked, never pass. Receipts carry license
fingerprints only, never license material.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
PROBES_PATH = HERE / "probes.v1.json"
FIXTURES_PATH = HERE / "fixtures.v1.json"
EXCERPT = 240
STREAM_LIMIT = 64 * 1024
STREAM_SECONDS = 15.0


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)


Transport = Callable[[str, str, dict, bytes | None, tuple[str, ...]], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_transport(method: str, url: str, headers: dict, body: bytes | None,
                   stream_markers: tuple[str, ...] = ()) -> Response:
    """One request; an event stream is read only until its markers appear (it never ends)."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=60)
    except urllib.error.HTTPError as error:
        return Response(error.code, error.headers.get("Content-Type") or "", error.read())
    with response:
        content_type = response.headers.get("Content-Type") or ""
        if not content_type.startswith("text/event-stream"):
            return Response(response.status, content_type, response.read())
        chunks, deadline = b"", time.monotonic() + STREAM_SECONDS
        while len(chunks) < STREAM_LIMIT and time.monotonic() < deadline:
            try:
                chunk = response.read1(4096)
            except (TimeoutError, socket.timeout):
                break
            if not chunk:
                break
            chunks += chunk
            if all(marker in chunks.decode("utf-8", "replace") for marker in stream_markers):
                break
        return Response(response.status, content_type, chunks)


# ── request generators ────────────────────────────────────────────────────────────────────

def citygml_building() -> bytes:
    """One two-surface building in EPSG:4326 (GML lat/lon axis order) over Kahului."""
    def surface(kind: str, ident: str, z: int) -> str:
        ring = " ".join(f"{lat} {lon} {z}" for lat, lon in (
            ("20.8900", "-156.4700"), ("20.8900", "-156.4690"), ("20.8910", "-156.4690"),
            ("20.8910", "-156.4700"), ("20.8900", "-156.4700")))
        return (f'<bldg:boundedBy><bldg:{kind} gml:id="{ident}"><bldg:lod2MultiSurface><gml:MultiSurface>'
                f'<gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing><gml:posList srsDimension="3">'
                f'{ring}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>'
                f'</gml:MultiSurface></bldg:lod2MultiSurface></bldg:{kind}></bldg:boundedBy>')
    return ('<?xml version="1.0" encoding="UTF-8"?><core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0" '
            'xmlns:bldg="http://www.opengis.net/citygml/building/2.0" xmlns:gml="http://www.opengis.net/gml">'
            '<gml:boundedBy><gml:Envelope srsName="urn:ogc:def:crs:EPSG::4326" srsDimension="3">'
            '<gml:lowerCorner>20.89 -156.47 0</gml:lowerCorner><gml:upperCorner>20.891 -156.469 10</gml:upperCorner>'
            '</gml:Envelope></gml:boundedBy><core:cityObjectMember><bldg:Building gml:id="BLDG_1">'
            + surface("GroundSurface", "GND_1", 0) + surface("RoofSurface", "ROOF_1", 10)
            + "</bldg:Building></core:cityObjectMember></core:CityModel>").encode("utf-8")


def las_grid() -> bytes:
    """An uncompressed LAS 1.2 point-data-format-3 file: a 4x4 coloured grid in EPSG:4326."""
    points = [(-156.47 + i * 0.0001, 20.89 + j * 0.0001, 5.0 + i) for i in range(4) for j in range(4)]
    scale_xy, scale_z = 1e-7, 0.01
    header = bytearray(227)
    header[0:4] = b"LASF"
    header[24], header[25] = 1, 2
    struct.pack_into("<HI", header, 94, 227, 227)
    header[104] = 3
    struct.pack_into("<HI", header, 105, 34, len(points))
    struct.pack_into("<ddd", header, 131, scale_xy, scale_xy, scale_z)
    xs, ys, zs = zip(*points)
    struct.pack_into("<dddddd", header, 179, max(xs), min(xs), max(ys), min(ys), max(zs), min(zs))
    records = bytearray()
    for x, y, z in points:
        record = bytearray(34)
        struct.pack_into("<iiiH", record, 0, round(x / scale_xy), round(y / scale_xy), round(z / scale_z), 100)
        record[15] = 2
        struct.pack_into("<HHH", record, 28, 100, 200, 300)
        records += record
    return bytes(header + records)


GENERATORS = {"citygml-building": citygml_building, "las-grid": las_grid}
BOUNDARY = "honua-entitlement-certification"


def multipart(spec: dict) -> bytes:
    parts = [f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
             for name, value in spec.get("fields", {}).items()]
    upload = spec["file"]
    parts.append((f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="{upload["filename"]}"\r\n'
                  f'Content-Type: {upload["content_type"]}\r\n\r\n').encode()
                 + GENERATORS[upload["generator"]]() + b"\r\n")
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


# ── placeholders ──────────────────────────────────────────────────────────────────────────

PLACEHOLDER = re.compile(r"\{([a-zA-Z_][\w:.-]*)\}")


def substitute(value, context: dict):
    """Replace {name} placeholders; a value that is exactly one placeholder keeps its type."""
    if isinstance(value, str):
        whole = PLACEHOLDER.fullmatch(value)
        if whole:
            return context[whole.group(1)]
        return PLACEHOLDER.sub(lambda match: str(context[match.group(1)]), value)
    if isinstance(value, list):
        return [substitute(item, context) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, context) for key, item in value.items()}
    return value


def seed_context(seed_manifest: dict) -> dict:
    """{layer:<service>} for every service the harness seeder published."""
    return {f"layer:{name}": entry["layerId"] for name, entry in (seed_manifest.get("demo") or {}).items()}


# ── assertions ────────────────────────────────────────────────────────────────────────────

_MISSING = object()


def resolve(document, path: str):
    current = document
    for name, index in re.findall(r"([^.\[\]]+)|\[(\d+)\]", path):
        if index:
            if not isinstance(current, list) or int(index) >= len(current):
                return _MISSING
            current = current[int(index)]
        else:
            if not isinstance(current, dict) or name not in current:
                return _MISSING
            current = current[name]
    return current


JSON_TYPES = {"array": list, "object": dict, "string": str, "boolean": bool}


def check_json(document, check: dict) -> str | None:
    path = check["path"]
    value = resolve(document, path)
    if check.get("absent"):
        return None if value is _MISSING else f"{path} must be absent"
    if value is _MISSING:
        return f"{path} is missing"
    if "equals" in check and value != check["equals"]:
        return f"{path} is {value!r}, expected {check['equals']!r}"
    if "type" in check:
        expected = check["type"]
        ok = (isinstance(value, (int, float)) and not isinstance(value, bool)) if expected == "number" \
            else isinstance(value, JSON_TYPES[expected])
        if not ok:
            return f"{path} is not a JSON {expected}"
    if "min_items" in check and (not isinstance(value, list) or len(value) < check["min_items"]):
        return f"{path} has fewer than {check['min_items']} item(s)"
    if "contains" in check and (not isinstance(value, list) or check["contains"] not in value):
        return f"{path} does not contain {check['contains']!r}"
    if "endswith" in check and (not isinstance(value, str) or not value.endswith(check["endswith"])):
        return f"{path} does not end with {check['endswith']!r}"
    return None


def media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def evaluate_positive(expect: dict, response: Response) -> list[str]:
    """Reasons the response is NOT the capability's documented success (empty when it is)."""
    problems = []
    if response.status != expect["status"]:
        problems.append(f"HTTP {response.status}, expected {expect['status']}")
    if media_type(response.content_type) != expect["content_type"]:
        problems.append(f"media type {media_type(response.content_type) or '<none>'}, expected {expect['content_type']}")
    if problems:
        return problems
    if "magic" in expect and not response.body.startswith(bytes.fromhex(expect["magic"])):
        problems.append(f"body does not start with {expect['magic']}")
    if len(response.body) < expect.get("min_bytes", 0):
        problems.append(f"body is shorter than {expect['min_bytes']} byte(s)")
    for needle in expect.get("text_contains", []):
        if needle not in response.text():
            problems.append(f"body does not contain {needle!r}")
    if expect.get("checks"):
        try:
            document = response.json()
        except ValueError:
            return problems + ["body is not JSON"]
        problems += [reason for check in expect["checks"] if (reason := check_json(document, check))]
    return problems


def evaluate_denial(wire: str, key: str, edition: str, response: Response) -> list[str]:
    """Reasons the response is NOT exactly the documented 402 denial for `key` (empty when it is)."""
    named, tier = f"entitlement: {key}", f"requires an active {edition} entitlement"
    try:
        document = response.json()
    except ValueError:
        document = None
    if wire == "problem":
        problems = []
        if response.status != 402:
            problems.append(f"HTTP {response.status}, expected 402")
        if media_type(response.content_type) != "application/problem+json":
            problems.append(f"media type {media_type(response.content_type) or '<none>'}, expected application/problem+json")
        if not isinstance(document, dict):
            return problems + ["body is not a JSON problem document"]
        if document.get("status") != 402:
            problems.append(f"problem status {document.get('status')!r}, expected 402")
        if document.get("title") != "Payment Required":
            problems.append(f"problem title {document.get('title')!r}, expected 'Payment Required'")
        detail = document.get("detail") or ""
        if named not in detail:
            problems.append(f"problem detail does not name {named!r}")
        if tier not in detail:
            problems.append(f"problem detail does not name the required edition ({tier!r})")
        return problems
    if wire == "geoservices":
        problems = []
        if response.status != 200:
            problems.append(f"HTTP {response.status}, expected the GeoServices 200 envelope")
        error = document.get("error") if isinstance(document, dict) else None
        if not isinstance(error, dict):
            return problems + ["body is not a GeoServices error envelope"]
        if error.get("code") != 402:
            problems.append(f"error.code {error.get('code')!r}, expected 402")
        if error.get("message") != "Payment Required":
            problems.append(f"error.message {error.get('message')!r}, expected 'Payment Required'")
        details = [str(item) for item in error.get("details") or []]
        if named not in details:
            problems.append(f"error.details does not name {named!r}")
        if not any(tier in item for item in details):
            problems.append(f"error.details does not name the required edition ({tier!r})")
        return problems
    raise ValueError(f"unknown denial wire {wire!r}")


# ── observe one stack ─────────────────────────────────────────────────────────────────────

@dataclass
class Stack:
    base_url: str
    api_key: str
    transport: Transport = http_transport
    context: dict = field(default_factory=dict)

    def send(self, spec: dict, markers: tuple[str, ...] = ()) -> Response:
        spec = substitute(spec, self.context)
        headers = {"X-API-Key": self.api_key, **spec.get("headers", {})}
        body = None
        if "json" in spec:
            body, headers["Content-Type"] = json.dumps(spec["json"]).encode(), "application/json"
        elif "form" in spec:
            body = urllib.parse.urlencode(spec["form"]).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif "multipart" in spec:
            body, headers["Content-Type"] = multipart(spec["multipart"]), f"multipart/form-data; boundary={BOUNDARY}"
        return self.transport(spec["method"], self.base_url.rstrip("/") + spec["path"], headers, body, markers)


def excerpt(response: Response) -> str:
    if media_type(response.content_type).startswith(("image/", "application/vnd.mapbox")):
        return f"<{len(response.body)} bytes>"
    return response.text()[:EXCERPT]


def check_license(stack: Stack, fixture: dict, granted: set[str]) -> list[str]:
    """The stack must actually run this fixture: valid, enabled, and exactly its grant active."""
    response = stack.send({"method": "GET", "path": "/api/v1/admin/license/status"})
    if response.status != 200:
        return [f"license status returned HTTP {response.status}"]
    try:
        data = response.json().get("data") or {}
    except (ValueError, AttributeError):
        return ["license status is not JSON"]
    problems = []
    for name, expected in (("mode", "enabled"), ("validationState", "Valid"), ("licenseId", fixture["license_id"]),
                           ("edition", fixture["edition"])):
        if data.get(name) != expected:
            problems.append(f"license {name} is {data.get(name)!r}, expected {expected!r}")
    active = {item.get("key") for item in data.get("entitlements") or [] if item.get("isActive")}
    if active != granted:
        problems.append(f"active entitlements differ from the fixture grant (extra {sorted(active - granted)}, "
                        f"missing {sorted(granted - active)})")
    return problems


def live_catalog(stack: Stack) -> list[str] | None:
    response = stack.send({"method": "GET", "path": "/api/v1/admin/license/entitlements"})
    try:
        return sorted(item["key"] for item in response.json()["data"]) if response.status == 200 else None
    except (ValueError, KeyError, TypeError):
        return None


def applicable(probes: dict, fixture_id: str) -> list[tuple[dict, str]]:
    """(probe, role) pairs this fixture's stack answers."""
    gated = probes["catalog"]["gated"]
    pairs = []
    for probe in probes["probes"]:
        if fixture_id == "entitled":
            pairs.append((probe, "positive"))
        elif probes["negative_fixture"][gated[probe["key"]]] == fixture_id:
            pairs.append((probe, "negative"))
    return pairs


def observe(stack: Stack, probes: dict, fixture: dict, granted: set[str], license_fingerprint: str) -> dict:
    gated = probes["catalog"]["gated"]
    record = {"fixture": fixture["id"], "edition": fixture["edition"], "license_fingerprint": license_fingerprint,
              "license_check": check_license(stack, fixture, granted), "live_catalog": live_catalog(stack),
              "observations": []}
    for probe, role in applicable(probes, fixture["id"]):
        key = probe["key"]
        observation = {"key": key, "role": role}
        record["observations"].append(observation)
        if role == "positive" and "positive_blocked" in probe:
            observation.update(status="blocked", reasons=[probe["positive_blocked"]])
        context = dict(stack.context)
        setup_problems = []
        for step in probe.get("setup", []):
            response = Stack(stack.base_url, stack.api_key, stack.transport, context).send(step)
            if response.status != step["expect_status"]:
                setup_problems.append(f"setup '{step['name']}' returned HTTP {response.status}: {excerpt(response)}")
                continue
            for name, path in (step.get("capture") or {}).items():
                value = resolve(response.json(), path)
                if value is not _MISSING:
                    context[name] = value
        if role == "positive" and setup_problems and "status" not in observation:
            observation.update(status="fail", reasons=setup_problems)
            continue
        missing = sorted({match for text in [json.dumps(probe["request"])] for match in PLACEHOLDER.findall(text)} - set(context))
        for name in missing:  # a negative stack may refuse a setup; the gate must still fire first
            context[name] = 0
        expect = probe.get("positive") or {}
        markers = tuple(expect.get("text_contains", ())) if role == "positive" else ()
        try:
            response = Stack(stack.base_url, stack.api_key, stack.transport, context).send(probe["request"], markers)
        except (OSError, urllib.error.URLError) as error:
            observation.setdefault("status", "fail")
            observation.setdefault("reasons", [f"request failed: {error}"])
            continue
        observation.update(http_status=response.status, media_type=media_type(response.content_type))
        if observation.get("status") == "blocked":
            observation["observed"] = excerpt(response)
            continue
        if role == "positive":
            reasons = evaluate_positive(expect, response)
        else:
            reasons = evaluate_denial(probe["denial"], key, gated[key], response)
        observation.update(status="fail" if reasons else "pass", reasons=reasons + setup_problems if reasons else [])
        if reasons:
            observation["observed"] = excerpt(response)
    return record


# ── verdict ───────────────────────────────────────────────────────────────────────────────

def coverage_problems(probes: dict) -> list[str]:
    gated = set(probes["catalog"]["gated"])
    rows = [probe["key"] for probe in probes["probes"]] + [row["key"] for row in probes["no_http_surface"]]
    problems = [f"gated key {key} has no probe or no-HTTP-surface row" for key in sorted(gated - set(rows))]
    problems += [f"row {key} is not a gated catalog key" for key in sorted(set(rows) - gated)]
    problems += [f"gated key {key} has more than one row" for key in sorted({k for k in rows if rows.count(k) > 1})]
    for probe in probes["probes"]:
        if ("positive" in probe) == ("positive_blocked" in probe):
            problems.append(f"probe {probe['key']} must declare exactly one of positive / positive_blocked")
        if probe["denial"] not in ("problem", "geoservices"):
            problems.append(f"probe {probe['key']} has unknown denial wire {probe['denial']!r}")
    editions = set(probes["catalog"]["gated"].values())
    problems += [f"no negative fixture for {edition} keys" for edition in sorted(editions - set(probes["negative_fixture"]))]
    return problems


def verdict(probes: dict, fixtures: dict, records: dict[str, dict]) -> dict:
    problems = coverage_problems(probes)
    blocked = []
    committed = sorted(set(probes["catalog"]["gated"]) | set(probes["catalog"]["community"]))
    for fixture in fixtures["fixtures"]:
        record = records.get(fixture["id"])
        if record is None:
            blocked.append(f"no observations for the {fixture['id']} fixture stack")
            continue
        problems += [f"{fixture['id']}: {reason}" for reason in record["license_check"]]
        if record["live_catalog"] is None:
            problems.append(f"{fixture['id']}: the candidate entitlement catalog could not be read")
        elif record["live_catalog"] != committed:
            live = set(record["live_catalog"])
            problems.append(f"{fixture['id']}: candidate catalog drift (unlisted {sorted(live - set(committed))}, "
                            f"retired {sorted(set(committed) - live)}); every entitlement needs a row")
    gated = probes["catalog"]["gated"]
    keys = []
    for probe in probes["probes"]:
        key = probe["key"]
        row = {"key": key, "required_edition": gated[key], "denial": probe["denial"]}
        for role, fixture_id in (("positive", "entitled"), ("negative", probes["negative_fixture"][gated[key]])):
            found = [obs for obs in (records.get(fixture_id) or {}).get("observations", []) if obs["key"] == key and obs["role"] == role]
            row[role] = found[0] if found else {"status": "blocked", "reasons": [f"{fixture_id} stack not observed"]}
            row[role]["fixture"] = fixture_id
        statuses = {row["positive"]["status"], row["negative"]["status"]}
        row["status"] = "fail" if "fail" in statuses else "blocked" if "blocked" in statuses else "pass"
        keys.append(row)
    for row in probes["no_http_surface"]:
        keys.append({"key": row["key"], "required_edition": gated[row["key"]], "status": "no-http-surface", "reason": row["reason"]})
    failing = [row["key"] for row in keys if row["status"] == "fail"]
    pending = [row["key"] for row in keys if row["status"] == "blocked"]
    if problems or failing:
        status = "fail"
        why = "; ".join(problems[:3] + [f"{len(failing)} capability assertion(s) failed: {', '.join(failing[:8])}"] * bool(failing))
    elif blocked or pending:
        status = "blocked"
        why = "; ".join(blocked[:3] + [f"positive proof blocked for {', '.join(pending)}"] * bool(pending))
    else:
        status = "pass"
        why = f"{sum(row['status'] == 'pass' for row in keys)} gated capabilities proven positive and negative"
    return {
        "schema": "honua-release/entitlement-certification-report/v1",
        "status": status,
        "why": why,
        "server_catalog_source": probes["source"],
        "fixtures": [{"id": f["id"], "edition": f["edition"], "grants": f["grants"],
                      "license_fingerprint": (records.get(f["id"]) or {}).get("license_fingerprint")} for f in fixtures["fixtures"]],
        "trust_anchor_fingerprint": fixtures["trust_anchor"]["public_key_fingerprint"],
        "problems": problems,
        "summary": {state: sum(row["status"] == state for row in keys) for state in ("pass", "fail", "blocked", "no-http-surface")},
        "capabilities": keys,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────────────────

def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    obs = sub.add_parser("observe", help="drive one fixture stack")
    obs.add_argument("--fixture", required=True)
    obs.add_argument("--base-url", required=True)
    obs.add_argument("--license", type=Path, required=True, help="the minted envelope; only its fingerprint is recorded")
    obs.add_argument("--seed-manifest", type=Path, required=True)
    obs.add_argument("--out", type=Path, required=True)
    ver = sub.add_parser("verdict", help="join fixture observations into one report")
    ver.add_argument("--observations", type=Path, required=True)
    ver.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    probes, fixtures = load(PROBES_PATH), load(FIXTURES_PATH)
    if args.command == "observe":
        sys.path.insert(0, str(HERE))
        from fixtures import granted_keys
        fixture = next((f for f in fixtures["fixtures"] if f["id"] == args.fixture), None)
        if fixture is None:
            parser.error(f"unknown fixture {args.fixture}")
        stack = Stack(args.base_url, os.environ.get("HONUA_ADMIN_PASSWORD", "honua-console-dev-key"))
        stack.context.update(seed_context(load(args.seed_manifest)))
        stack.context["scim_token"] = os.environ.get("HONUA_ENTITLEMENT_SCIM_TOKEN", "entitlement-certification-scim")
        fingerprint = "sha256:" + hashlib.sha256(args.license.read_bytes()).hexdigest()
        record = observe(stack, probes, fixture, set(granted_keys(fixture, probes)), fingerprint)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        failed = [o["key"] for o in record["observations"] if o["status"] == "fail"]
        print(f"{args.fixture}: {len(record['observations'])} probe(s), {len(failed)} failing"
              + (f": {', '.join(failed)}" if failed else "") + (f"; license: {record['license_check']}" if record["license_check"] else ""))
        return 0
    records = {}
    for path in sorted(args.observations.glob("*.json")):
        record = load(path)
        records[record["fixture"]] = record
    report = verdict(probes, fixtures, records)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"status={report['status']}")
    print(f"why={report['why']}")
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
