"""Independent deterministic GIS predicates; no model calls or workflow interpreter.

The live observer must seed this immutable fixture in an isolated namespace and
retain raw API responses. Predicate unit tests are not live qualification.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from agent_e2e_pack import PackError, canonical_digest

CASES = (
    "select-strict", "select-inclusive", "select-inclusive-null", "paging-small",
    "paging-large", "reordered-source", "same-name-isolation", "buffer-metric",
    "transform-coordinates", "contains-target", "contains-join", "boundary-hole",
    "join-multiplicity", "source-write-denied", "geodesic-unsupported",
)


def require(condition, message):
    if not condition:
        raise PackError(message)


def fixture():
    return {"srid": 3857, "features": [
        {"type": "Feature", "id": f"s{i}", "properties": {"source_id": f"s{i}", "capacity": capacity},
         "geometry": {"type": "Point", "coordinates": [i * 10, i * 10]}}
        for i, capacity in enumerate((49, 50, 51, None), 1)]}


def expected_ids(case):
    if case in ("select-strict", "same-name-isolation"):
        return ["s3"]
    if case == "select-inclusive":
        return ["s2", "s3"]
    return ["s2", "s3", "s4"]


def verify(case, observed):
    require(case in CASES, "unknown semantic assertion")
    require(canonical_digest(observed.get("sourceBefore")) == canonical_digest(fixture()) and
            canonical_digest(observed.get("sourceAfter")) == canonical_digest(fixture()),
            "immutable semantic source differs or changed")
    result = observed["result"]
    if case in CASES[:7]:
        rows = result["features"]
        require(isinstance(rows, list), "missing selected features")
        ids = [r["properties"]["source_id"] for r in rows]
        require(len(ids) == len(set(ids)) and sorted(ids) == expected_ids(case), "selection membership/multiplicity differs")
        source = {f["id"]: f for f in fixture()["features"]}
        require(all(canonical_digest(row) == canonical_digest(source[row["properties"]["source_id"]]) for row in rows), "selected attributes or geometry changed")
        if case.startswith("paging-"):
            pages = result["pages"]
            require(isinstance(pages, list) and bool(pages), "missing pages")
            page_ids = [item for page in pages for item in page["sourceIds"]]
            require(page_ids == ids and all(page["responseDigest"] for page in pages), "unbound/incomplete paging")
            require(result["terminalPageObserved"] is True, "pagination did not reach terminal page")
            if case == "paging-small":
                require(len(pages) >= 3, "small-page case did not traverse pages")
    elif case == "buffer-metric":
        # Import the same independently geometric predicate used by the pack.
        import agent_e2e_gate
        agent_e2e_gate.oracle_predicates.metric_buffer(result, {
            "sourceId": "s1", "center3857": [10, 10], "radiusMetres": 500,
            "radialToleranceMetres": 1, "expectedArea": math.pi * 500 ** 2,
            "areaRelativeTolerance": 0.007})
    elif case == "transform-coordinates":
        require(result["srid"] == 4326, "wrong transformed CRS")
        rows = result["features"]
        require(len(rows) == 4, "transformation lost features")
        by_id = {r["properties"]["source_id"]: r for r in rows}
        require(set(by_id) == {"s1", "s2", "s3", "s4"}, "transformation changed identities")
        for source in fixture()["features"]:
            row = by_id[source["id"]]
            x, y = source["geometry"]["coordinates"]
            wanted = [math.degrees(x / 6378137), math.degrees(2 * math.atan(math.exp(y / 6378137)) - math.pi / 2)]
            require(row["properties"] == source["properties"] and row["geometry"]["type"] == "Point", "transformation changed attributes/type")
            actual = row["geometry"]["coordinates"]
            require(len(actual) == 2 and all(type(a) in (int, float) and math.isfinite(a) and abs(a-b) <= 1e-8 for a,b in zip(actual,wanted)), "coordinates were relabeled or rounded beyond tolerance")
    elif case in ("contains-target", "contains-join", "boundary-hole", "join-multiplicity"):
        # Spatial fixture: polygon outer square (0,0)-(100,100), hole
        # (40,40)-(60,60); points inside=(20,20), boundary=(0,20),
        # hole=(50,50), outside=(110,20). A second polygon overlaps inside.
        expected = {
            "contains-target": [["inside", "outer"], ["inside", "overlap"]],
            "contains-join": [["outer", "inside"], ["overlap", "inside"]],
            "boundary-hole": ["inside"],
            "join-multiplicity": [{"id": "inside", "matches": 2}, {"id": "outside", "matches": 0}],
        }[case]
        require(canonical_digest(result["values"]) == canonical_digest(expected), "spatial roles, boundaries or cardinality differ")
    else:
        require(type(result.get("httpStatus")) is int and result["httpStatus"] in (400, 403, 405, 422), "operation was not refused")
        require(isinstance(result.get("errorCode"), str) and bool(result["errorCode"]), "refusal lacks typed error")
        require(result["createdResourceIds"] == [], "unsupported operation created resources")


def validate_report(report, campaign, targets, evidence_root, resolve):
    require(set(report) == {"schemaVersion", "producer", "componentBindingDigest", "knowledge", "configurations"},
            "invalid semantic report fields")
    require(type(report["schemaVersion"]) is int and report["schemaVersion"] == 1 and
            report["producer"] == "independent-semantic-suite", "not an independent semantic report")
    require(report["componentBindingDigest"] == campaign["componentBindingDigest"] and
            report["knowledge"] == campaign["knowledge"], "semantic report has wrong candidate/knowledge")
    configurations = report["configurations"]
    require(isinstance(configurations, list) and len(configurations) == len(targets) and
            {c["target"] for c in configurations} == set(targets), "semantic target coverage incomplete")
    for configuration in configurations:
        require(set(configuration) == {"target", "runtimeInterfaces", "checks"}, "invalid semantic configuration")
        resolve(evidence_root, configuration["runtimeInterfaces"])
        checks = configuration["checks"]
        require(isinstance(checks, list) and len(checks) == len(CASES) and {c["id"] for c in checks} == set(CASES),
                "semantic assertion coverage incomplete")
        for check in checks:
            require(set(check) == {"id", "observation", "rawResponses"}, "invalid semantic check")
            require(isinstance(check["rawResponses"], list) and bool(check["rawResponses"]), "missing raw semantic responses")
            for ref in check["rawResponses"]:
                resolve(evidence_root, ref)
            observed = json.loads(Path(resolve(evidence_root, check["observation"])).read_text(encoding="utf-8"))
            verify(check["id"], observed)
