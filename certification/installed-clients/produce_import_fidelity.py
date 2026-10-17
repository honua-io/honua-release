#!/usr/bin/env python3
"""Execute a clean public-NuGet consumer; retain the complete frozen scorecard.

No test report, old source-client receipt, or user-supplied verdict is accepted as
input. Unimplemented qualification remains blocked in the emitted receipt. The
existing consumer remains the authority and rejects every incomplete receipt.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
import zipfile
import xml.etree.ElementTree as ET
from functools import cache
from fractions import Fraction

import yaml
import dotnet_import_fidelity as gate

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


@cache
def installed_module():
    spec = importlib.util.spec_from_file_location("installed_client_producer", HERE / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProducerError(RuntimeError):
    pass


def install_consumer(manifest: dict, work: Path) -> tuple[list[str], dict[str, str], dict]:
    pin = manifest["clientArtifacts"][gate.PIN_ARTIFACT]
    if (pin.get("package") != "Honua.Sdk" or pin.get("registry") != "nuget.org"
            or pin.get("publicationState") not in {"published", "promoted"}
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", str(pin.get("version")))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(pin.get("digest")))):
        raise ProducerError("requires the manifest-pinned published Honua.Sdk version and sha256 on nuget.org")
    if work.exists():
        raise ProducerError("consumer workspace must not exist before the clean install")
    project = work / "consumer"
    shutil.copytree(HERE / "import-fidelity-consumer", project)
    installed = installed_module()
    framework = installed._nuget_framework(pin)
    if not framework:
        raise ProducerError("manifest pin has no valid target framework")
    ok, detail, env = installed._restore_nuget(pin, work, project, [
        f"-p:ImportFidelityTargetFramework={framework}", f"-p:ImportFidelityPackageVersion={pin['version']}"])
    if not ok:
        raise ProducerError(detail)
    pinned_archive = work / "packages" / pin["package"].lower() / pin["version"].lower() / f"{pin['package'].lower()}.{pin['version'].lower()}.nupkg"
    with zipfile.ZipFile(pinned_archive) as archive:
        nuspec = ET.fromstring(archive.read(next(name for name in archive.namelist() if name.endswith(".nuspec"))))
        repository = nuspec.find(".//{*}repository")
        if repository is None or repository.attrib.get("commit") != pin.get("sourceSha"):
            raise ProducerError("published package repository commit differs from the manifest source SHA")
    assets = json.loads((project / "obj/project.assets.json").read_text())
    if any(value.get("type") == "project" for value in assets["libraries"].values()):
        raise ProducerError("consumer resolves a ProjectReference")
    assemblies = verify_runtime_assets(assets, work / "packages", work / "out", framework)
    consumer = {
        "kind": "published-nuget", "packageId": pin["package"], "version": pin["version"],
        "digest": pin["digest"], "sourceSha": pin["sourceSha"], "registry": gate.PUBLIC_REGISTRIES[0], "sourceBuilt": False,
        "clean": True, "projectReference": False, "assemblyMatchesPackage": False,
        "assemblies": assemblies,
    }
    return ["dotnet", str(work / "out/Consumer.dll")], env, consumer


def verify_runtime_assets(assets: dict, packages: Path, output: Path, framework: str) -> list[dict]:
    """Compare only the runtime member NuGet selected for this consumer's TFM.

    Other TFMs and reference assemblies may legitimately have different bytes with
    the same basename. They are not the assembly this executable loads.
    """
    candidates = (framework, f".NETCoreApp,Version=v{framework[3:]}")
    targets = assets.get("targets", {})
    selected = [name for name in candidates if name in targets]
    if len(selected) != 1:
        raise ProducerError("consumer assets do not identify exactly one target framework")
    assemblies = []
    for key, value in assets["libraries"].items():
        if not key.lower().startswith("honua."):
            continue
        package_path = packages / value["path"]
        metadata = json.loads((package_path / ".nupkg.metadata").read_text())
        if metadata.get("source") != gate.PUBLIC_REGISTRIES[0]:
            raise ProducerError(f"{key} was not restored from public nuget.org")
        dependency = targets[selected[0]].get(key)
        if dependency is None:
            raise ProducerError(f"{key} has no selected target-framework assets")
        archive_path = package_path / (key.lower().replace("/", ".") + ".nupkg")
        with zipfile.ZipFile(archive_path) as archive:
            for member in dependency.get("runtime", {}):
                if not member.endswith(".dll"):
                    continue
                destination = output / Path(member).name
                if not destination.is_file():
                    raise ProducerError(f"selected runtime assembly from {key} is absent from the consumer output")
                try:
                    packaged = archive.read(member)
                except KeyError as exc:
                    raise ProducerError(f"selected runtime asset is absent from restored public {key}") from exc
                digest = hashlib.sha256(packaged).hexdigest()
                if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    raise ProducerError(f"consumer assembly differs from selected runtime asset in public {key}")
                assemblies.append({"name": destination.stem, "package": key, "asset": member, "sha256": digest})
    if not {"Honua.Sdk.Admin", "Honua.Sdk.GeoServices"} <= {item["name"] for item in assemblies}:
        raise ProducerError("public package did not supply the required runtime assemblies")
    return assemblies


class Sdk:
    def __init__(self, command: list[str], env: dict[str, str], consumer: dict):
        self.process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
        identity = self.call("identity")
        packaged = {item["name"]: item["sha256"] for item in consumer["assemblies"]}
        if {item["name"] for item in identity} != {"Honua.Sdk.Admin", "Honua.Sdk.GeoServices"} or any(
                item["sha256"] != packaged.get(item["name"]) for item in identity):
            self.close()
            raise ProducerError("loaded SDK assemblies differ from the restored public package bytes")
        consumer["assemblyMatchesPackage"] = True
        consumer["loadedAssemblies"] = identity

    def call(self, operation: str, **arguments):
        request = {"operation": operation, **arguments}
        remaining = min(310, max(0, getattr(self, "deadline", time.monotonic() + 310) - time.monotonic()))
        if remaining <= 0:
            raise ProducerError("live SDK execution exceeded its overall time bound")
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            if not select.select([self.process.stdout], [], [], remaining)[0]:
                raise ProducerError("SDK operation exceeded its bounded execution time")
            response = json.loads(self.process.stdout.readline())
        except (BrokenPipeError, json.JSONDecodeError) as exc:
            raise ProducerError("SDK consumer exited or returned invalid JSON") from exc
        if not response.get("ok"):
            raise ProducerError(f"{operation}: {response.get('errorType')} httpStatus={response.get('httpStatus')} "
                                f"problemType={response.get('problemType')} code={response.get('code')}")
        return response["result"]

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


# Public source definitions frozen by the server's import-fidelity baseline. Compare fields
# are source identifiers, not invented fixture equivalents. Each named case really imports.
CASES = {
    "esri_census_states": ("https://sampleserver6.arcgisonline.com/arcgis/rest/services/Census/MapServer", 3, "STATE_NAME", "POP2000"),
    "esri_military_ops_area_z": ("https://sampleserver6.arcgisonline.com/arcgis/rest/services/Military/FeatureServer", 5, "uniquedesignation", "Shape__Area"),
    "esri_military_ops_line_zm": ("https://sampleserver6.arcgisonline.com/arcgis/rest/services/Military/FeatureServer", 4, "uniquedesignation", "Shape__Length"),
    "esri_usa_cities": ("https://sampleserver6.arcgisonline.com/arcgis/rest/services/USA/MapServer", 0, "areaname", "pop2000"),
    "esri_usa_highways": ("https://sampleserver6.arcgisonline.com/arcgis/rest/services/USA/MapServer", 1, "type", "length"),
    "esri_wildfire_lines": ("https://sampleserver6.arcgisonline.com/arcgis/rest/services/Wildfire/FeatureServer", 1, "description", "Shape__Length"),
    "hawaii_historiccultural_moku": ("https://geodata.hawaii.gov/arcgis/rest/services/HistoricCultural/MapServer", 3, "moku", "gisacres"),
    "hawaii_infra_dams": ("https://geodata.hawaii.gov/arcgis/rest/services/Infrastructure/MapServer", 10, "dam_name", "longitude"),
    "hawaii_infra_marine_sewerlines": ("https://geodata.hawaii.gov/arcgis/rest/services/Infrastructure/MapServer", 12, "island", "id"),
    "kauai_bridges": ("https://maps.kauai.gov/server/rest/services/Bridges_with_condition_where_available/FeatureServer", 2, "NBI_Condition_Rating", "Length"),
}


def equal(expected: Any, actual: Any, tolerance: float = 0) -> bool:
    """Preserve exact integral and null semantics; tolerate only floating coordinates/statistics."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, int) and isinstance(actual, int):
        return expected == actual
    if isinstance(expected, int) and isinstance(actual, float):
        return math.isfinite(actual) and abs(Fraction(expected) - Fraction(actual)) <= Fraction(tolerance)
    if isinstance(expected, float) and isinstance(actual, int):
        return math.isfinite(expected) and abs(Fraction(expected) - Fraction(actual)) <= Fraction(tolerance)
    if isinstance(expected, (float, int)) and isinstance(actual, (float, int)):
        return math.isfinite(expected) and math.isfinite(actual) and math.isclose(expected, actual, rel_tol=0, abs_tol=tolerance)
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(equal(value, actual[key], tolerance) for key, value in expected.items())
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(equal(left, right, tolerance) for left, right in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


def feature_rows(document: dict, fields: list[str], oid: str) -> list:
    if not isinstance(document.get("features"), list) or document.get("exceededTransferLimit") is True:
        raise ProducerError("query omitted features or exceeded its transfer limit")
    rows = []
    for feature in document["features"]:
        attributes = {key.lower(): value for key, value in feature["attributes"].items()}
        if any(field.lower() not in attributes for field in fields):
            raise ProducerError("query dropped source fields")
        rows.append({field.lower(): attributes[field.lower()] for field in fields})
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def require_equal(expected: Any, actual: Any, message: str, tolerance: float = 0) -> str:
    if not equal(expected, actual, tolerance):
        raise ProducerError(message)
    return message + ": equal"


class Scorecard:
    def __init__(self, manifest: dict, requirements: dict, evidence: str):
        self.manifest, self.requirements, self.evidence = manifest, requirements, evidence
        self.cells = {cell["id"]: {
            "id": cell["id"], "verdict": "not-applicable" if cell["kind"] == "not-applicable" else "blocked",
            "executed": False, "stale": False, "sourceBuilt": False,
            "reason": "frozen baseline excludes time queries" if cell["kind"] == "not-applicable" else "qualification not implemented by this producer",
        } for cell in requirements["cells"]}
        self.differences = []
        self.postgis = False
        self.consumer = {}
        self.provenance = {}
        self.capture_directory = None

    def record(self, requirement_id: str, action):
        cell = self.cells[requirement_id]
        try:
            detail = action()
            cell.update(verdict="pass", executed=True, evidenceUri=self.evidence, reason=detail)
        except (ProducerError, ValueError, KeyError, TypeError) as exc:
            cell.update(verdict="fail", executed=True, evidenceUri=self.evidence, reason=str(exc))

    def receipt(self) -> dict:
        cells = list(self.cells.values())
        supported = [cell for cell in cells if cell["verdict"] != "not-applicable"]
        passed = sum(cell["verdict"] == "pass" for cell in supported)
        sha, image = gate.server_image_ref(self.manifest)
        return {
            "schema": gate.RECEIPT_SCHEMA, "requirementsRevision": self.requirements["revision"],
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "evidenceUri": self.evidence,
            "consumer": self.consumer, "server": {"sourceSha": sha, "image": image},
            "postgis": self.postgis, "sdkSeamMocks": False,
            "rollbackRepoint": "pass" if self.cells["evidence.rollback-repoint"]["verdict"] == "pass" else "blocked",
            "differences": self.differences, "provenance": self.provenance,
            "cells": cells, "summary": {"denominator": len(supported), "passed": passed,
                "failed": len(supported) - passed, "skipped": 0, "notApplicable": len(cells) - len(supported)},
        }


def sql(compose: list[str], statement: str) -> str:
    result = subprocess.run([*compose, "exec", "-T", "db", "psql", "-U", "honua", "-d", "honua",
                             "-v", "ON_ERROR_STOP=1", "-At"], input=statement, text=True,
                            capture_output=True, check=False, timeout=60)
    if result.returncode:
        raise ProducerError("independent PostGIS oracle failed")
    return result.stdout.strip()


def candidate_identity(manifest: dict, compose: list[str]) -> dict:
    container = subprocess.run([*compose, "ps", "-q", "server"], text=True, capture_output=True, check=True).stdout.strip()
    info = json.loads(subprocess.run(["docker", "inspect", container], text=True, capture_output=True, check=True).stdout)[0]
    sha, image = gate.server_image_ref(manifest)
    if info["Config"]["Image"] != image:
        raise ProducerError("running candidate image differs from manifest")
    image_info = json.loads(subprocess.run(["docker", "image", "inspect", info["Image"]], text=True, capture_output=True, check=True).stdout)[0]
    revision = (image_info.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
    if revision != sha:
        raise ProducerError("running image source revision differs from manifest")
    if not any(value.endswith("@" + manifest["components"]["honua-server"]["digest"]) for value in image_info["RepoDigests"]):
        raise ProducerError("running image has no matching immutable registry digest")
    return {"imageId": info["Image"], "sourceSha": revision, "image": image}


def object_id_field(metadata: dict) -> str:
    """Resolve the single declared identifier, including MapServer metadata without a pointer."""
    fields = metadata.get("fields")
    if not isinstance(fields, list):
        raise ProducerError("layer metadata omitted declared fields")
    identifiers = [field.get("name") for field in fields if field.get("type") == "esriFieldTypeOID"]
    if len(identifiers) != 1 or not isinstance(identifiers[0], str) or not identifiers[0]:
        raise ProducerError("layer metadata must declare exactly one unambiguous OID field")
    identifier = identifiers[0]
    if "objectIdField" in metadata:
        explicit = metadata["objectIdField"]
        if not isinstance(explicit, str) or explicit.casefold() != identifier.casefold():
            raise ProducerError("explicit objectIdField conflicts with the declared OID field")
    return identifier


def read_snapshot(sdk: Sdk, service: str, layer: int, *, candidate: bool = False, limit: int = 50000) -> dict:
    metadata = sdk.call("layer", baseUrl=service, layerId=layer, candidate=candidate)
    oid = object_id_field(metadata)
    count = sdk.call("count", baseUrl=service, layerId=layer, candidate=candidate, query={"where": "1=1"})
    ids = sdk.call("ids", baseUrl=service, layerId=layer, candidate=candidate, query={"where": "1=1"})
    if not 0 < count <= limit or len(ids) != count or len(set(ids)) != count:
        raise ProducerError("source/target empty, exceeds the explicit 50000-row bound, or has incomplete/duplicate IDs")
    features = []
    for offset in range(0, count, 200):
        batch = ids[offset:offset + 200]
        document = sdk.call("query", baseUrl=service, layerId=layer, candidate=candidate,
                            query={"objectIds": batch, "outFields": "*", "outSR": 4326,
                                   "returnZ": True, "returnM": True, "returnGeometry": True})
        if document.get("exceededTransferLimit") is True or len(document.get("features", [])) != len(batch):
            raise ProducerError("ID-batch query truncated, omitted, or duplicated records")
        found = [feature["attributes"][oid] for feature in document["features"]]
        if set(found) != set(batch) or len(found) != len(set(found)):
            raise ProducerError("ID-batch query did not return exactly the requested feature identities")
        features.extend(document["features"])
    return {"metadata": metadata, "count": count, "ids": ids, "objectIdField": oid, "features": features}


def query_rows(sdk: Sdk, service: str, layer: int, query: dict, *, candidate: bool = False) -> list:
    result = sdk.call("query", baseUrl=service, layerId=layer, query=query, candidate=candidate)
    if "error" in result or result.get("exceededTransferLimit") is True or "features" not in result:
        raise ProducerError("parity query failed, omitted features, or exceeded the transfer limit")
    return sorted(({key.lower(): value for key, value in feature["attributes"].items()}
                   for feature in result["features"]), key=lambda row: json.dumps(row, sort_keys=True))


def geojson_snapshot(sdk: Sdk, service: str, layer: int, snapshot: dict, *, candidate: bool = False) -> list:
    """Read bounded GeoJSON ID batches; a source transfer cap cannot omit later rows."""
    oid = snapshot["objectIdField"].lower()
    field_names = [field["name"].lower() for field in snapshot["metadata"]["fields"] if field["type"] != "esriFieldTypeGeometry"]
    result = []
    for offset in range(0, len(snapshot["ids"]), 200):
        batch = snapshot["ids"][offset:offset + 200]
        document = sdk.call("query", baseUrl=service, layerId=layer, candidate=candidate,
                            query={"objectIds": batch, "outFields": "*", "format": "GeoJson", "outSR": 4326})
        if (document.get("type") != "FeatureCollection" or document.get("exceededTransferLimit") is True
                or len(document.get("features", [])) != len(batch)):
            raise ProducerError("GeoJSON ID batch missing, truncated or wrong feature count")
        found = []
        for feature in document["features"]:
            properties = {key.lower(): value for key, value in feature["properties"].items()}
            if oid not in properties and "id" in feature:
                properties[oid] = feature["id"]
            if any(name not in properties for name in field_names):
                raise ProducerError("GeoJSON dropped a source property")
            identity = properties[oid]
            found.append(identity)
            result.append({"id": identity, "properties": {name: properties[name] for name in field_names},
                           "geometry": {"present": "geometry" in feature, "value": feature.get("geometry")}})
        if set(found) != set(batch) or len(found) != len(set(found)):
            raise ProducerError("GeoJSON ID batch repeated or omitted source identities")
    return sorted(result, key=lambda feature: feature["id"])


def baseline_case(score: Scorecard, sdk: Sdk, compose: list[str], name: str, base: str, tag: str) -> None:
    source, source_layer, compare_field, numeric_field = CASES[name]
    prefix = name + "/"
    state = {}

    def prepare():
        snapshot = read_snapshot(sdk, source, source_layer)
        table = f"f_{name}_{tag}"
        service = f"fidelity-{name.replace('_', '-')}-{tag}"
        result = sdk.call("StartGeoservicesImportAsync", baseUrl=base, candidate=True, arguments=[{
            "serviceUrl": source, "layerId": source_layer, "tableName": table,
            "targetSchema": "import_fidelity", "targetSrid": 4326, "autoPublish": True,
            "serviceName": service, "batchSize": 200, "requestTimeoutSeconds": 60, "maxRetries": 0,
        }])
        job_id = result["jobId"]
        deadline = time.monotonic() + 240
        while True:
            progress = sdk.call("GetGeoservicesImportJobStatusAsync", baseUrl=base, candidate=True, arguments=[job_id])
            status = str(progress["status"]).lower()
            if status in {"completed", "failed", "cancelled", "needsreview", "needs-review", "completedwitherrors"}:
                break
            if time.monotonic() >= deadline:
                raise ProducerError("import job did not terminate within 240 seconds")
            time.sleep(1)
        if status != "completed" or progress.get("failedFeatures", 0) or progress.get("publishedLayerId") is None:
            raise ProducerError(f"import not losslessly published: {status}, failedFeatures={progress.get('failedFeatures')}")
        count = int(sql(compose, f'SELECT count(*) FROM import_fidelity."{table}";'))
        require_equal(snapshot["count"], count, "independent PostGIS/source row count")
        target = f"{base.rstrip('/')}/rest/services/{service}/FeatureServer"
        target_snapshot = read_snapshot(sdk, target, progress["publishedLayerId"], candidate=True)
        # A moving public source cannot qualify: neither an old receipt nor a source mutated
        # during import may become the oracle for the selected candidate.
        after = read_snapshot(sdk, source, source_layer)
        require_equal(snapshot, after, "public source stability during import")
        if score.capture_directory:
            score.capture_directory.mkdir(parents=True, exist_ok=True)
            (score.capture_directory / f"{name}.json").write_text(json.dumps({
                "sourceBefore": snapshot, "sourceAfter": after, "target": target_snapshot,
                "postgisCount": count, "jobId": job_id,
            }, indent=2) + "\n")
        state.update(source=snapshot, target=target_snapshot, targetUrl=target,
                     targetLayer=progress["publishedLayerId"], progress=progress, table=table)
        score.provenance.setdefault("imports", []).append({
            "serviceCase": name, "jobId": job_id, "sourceUrl": source, "sourceLayerId": source_layer,
            "targetServiceUrl": target, "targetLayerId": progress["publishedLayerId"],
            "sourceCount": snapshot["count"], "postgisCount": count,
            "sourceSnapshotSha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        })

    try:
        prepare()
    except (ProducerError, KeyError, ValueError, TypeError) as exc:
        for check in gate.FROZEN_CHECKS:
            if check not in gate.NOT_APPLICABLE_CHECKS:
                score.record(prefix + check, lambda exc=exc: (_ for _ in ()).throw(ProducerError(str(exc))))
        return
    source_snapshot, target_snapshot = state["source"], state["target"]
    fields = [field["name"] for field in source_snapshot["metadata"]["fields"] if field["type"] != "esriFieldTypeGeometry"]
    source_doc, target_doc = {"features": source_snapshot["features"]}, {"features": target_snapshot["features"]}

    def parity(query: dict, tolerance: float = 0):
        return require_equal(query_rows(sdk, source, source_layer, query),
                             query_rows(sdk, state["targetUrl"], state["targetLayer"], query, candidate=True), "source/target query values", tolerance)

    def schema():
        target_fields = {field["name"].lower(): field for field in target_snapshot["metadata"]["fields"]}
        for field in source_snapshot["metadata"]["fields"]:
            if field["type"] == "esriFieldTypeGeometry":
                continue
            other = target_fields.get(field["name"].lower())
            if other is None:
                raise ProducerError("target schema dropped source field " + field["name"])
            for member in ("type", "alias", "length", "domain", "defaultValue", "nullable"):
                if member in field and (member not in other or not equal(field[member], other[member])):
                    raise ProducerError(f"target metadata drift: {field['name']}.{member}")
        return "all source field types, aliases, lengths, domains, defaults and nullability preserved"

    score.record(prefix + "all_fields_diff", schema)
    score.record(prefix + "core_query_parity", lambda: require_equal(
        feature_rows(source_doc, fields, source_snapshot["objectIdField"]),
        feature_rows(target_doc, fields, source_snapshot["objectIdField"]), "every source field value and exact integer/null semantics"))
    def geometry():
        oid = source_snapshot["objectIdField"].lower()
        def geometries(snapshot):
            return sorted([[next(value for key, value in feature["attributes"].items() if key.lower() == oid), {"present": "geometry" in feature, "value": feature.get("geometry")}]
                           for feature in snapshot["features"]], key=lambda item: item[0])
        return require_equal(geometries(source_snapshot), geometries(target_snapshot), "geometry identity and every XY/Z/M ordinate at 1e-8 tolerance", 1e-8)
    score.record(prefix + "geometry_parity", geometry)
    score.record(prefix + "distinct_parity", lambda: parity({"where": "1=1", "outFields": compare_field,
                 "returnGeometry": False, "returnDistinctValues": True, "resultRecordCount": 10000}))
    score.record(prefix + "edge_case_query_parity", lambda: parity({"where": f"{compare_field} IS NULL OR {numeric_field} = 0",
                 "outFields": ",".join(fields), "returnGeometry": False, "resultRecordCount": 10000}))
    statistics = json.dumps([{"statisticType": kind, "onStatisticField": numeric_field, "outStatisticFieldName": "f_" + kind}
                             for kind in ("count", "min", "max", "sum", "avg")])
    score.record(prefix + "statistics_parity", lambda: parity({"where": "1=1", "outStatistics": statistics, "returnGeometry": False}, 1e-8))
    score.record(prefix + "grouped_statistics_parity", lambda: parity({"where": "1=1", "outStatistics": statistics,
                 "groupByFieldsForStatistics": compare_field, "returnGeometry": False, "resultRecordCount": 10000}, 1e-8))
    score.record(prefix + "spatial_envelope_parity", lambda: require_equal(
        sdk.call("extent", baseUrl=source, layerId=source_layer, query={"where": "1=1", "outSR": 4326}),
        sdk.call("extent", baseUrl=state["targetUrl"], layerId=state["targetLayer"], candidate=True, query={"where": "1=1", "outSR": 4326}), "source/target envelope", 1e-8))
    def errors():
        documents = []
        for service, layer, candidate in ((source, source_layer, False), (state["targetUrl"], state["targetLayer"], True)):
            try:
                document = sdk.call("query", baseUrl=service, layerId=layer, candidate=candidate, query={"where": "__missing_fidelity_column__ = 1"})
                error = document.get("error")
                if not isinstance(error, dict) or not isinstance(error.get("code"), int) or not error.get("message"):
                    raise ProducerError("invalid-field query did not return an explicit GeoServices error envelope")
                documents.append(error["code"])
            except ProducerError as exc:
                # Typed SDK exceptions preserve GeoServices error codes, including HTTP-200 envelopes.
                match = re.search(r"HonuaFeatureServerException code=(\d+)$", str(exc))
                if not match:
                    raise
                documents.append(int(match[1]))
        return require_equal(documents[0], documents[1], "invalid-field GeoServices error codes")
    score.record(prefix + "error_shape_parity", errors)
    # GeoJSON and MapServer qualification require separate reads, never a renamed core-query pass.
    def geojson():
        source_features = geojson_snapshot(sdk, source, source_layer, source_snapshot)
        target_features = geojson_snapshot(sdk, state["targetUrl"], state["targetLayer"], source_snapshot, candidate=True)
        for expected, observed in zip(source_features, target_features):
            require_equal(expected["id"], observed["id"], "GeoJSON feature identity")
            require_equal(expected["properties"], observed["properties"], "GeoJSON exact property values")
            require_equal(expected["geometry"], observed["geometry"], "GeoJSON geometry", 1e-8)
        return "every source GeoJSON property and geometry preserved across bounded, identity-checked ID batches"
    score.record(prefix + "geojson_query_parity", geojson)
    def mapserver():
        map_url = state["targetUrl"].replace("/FeatureServer", "/MapServer")
        map_snapshot = read_snapshot(sdk, map_url, state["targetLayer"], candidate=True)
        return require_equal(feature_rows(target_doc, fields, source_snapshot["objectIdField"]),
                             feature_rows({"features": map_snapshot["features"]}, fields, source_snapshot["objectIdField"]), "target MapServer/FeatureServer values")
    score.record(prefix + "mapserver_featureserver_parity", mapserver)


def service_journey(score: Scorecard, sdk: Sdk, base: str, tag: str) -> None:
    # USA is a real external service with three small layers. Select the entire discovered
    # service; omitting a table/layer or treating needs-review as success is forbidden.
    source = "https://sampleserver6.arcgisonline.com/arcgis/rest/services/USA/MapServer"
    state = {}
    def inventory():
        value = sdk.call("ScanMigrationSourceAsync", baseUrl=base, candidate=True, arguments=[{
            "sourceKind": "arcgis-geoservices-rest", "sourceUrl": source, "timeoutSeconds": 60}])
        raw = sdk.call("service", baseUrl=source)
        discovered = list(raw.get("layers", [])) + list(raw.get("tables", []))
        resources = value.get("resources", [])
        if len(resources) != len(discovered) or not resources:
            raise ProducerError("inventory omitted a source layer/table")
        state["inventory"] = value
        state["resources"] = resources
        return "SDK scan accounted for every live source layer and table"
    score.record("journey.inventory-discovery", inventory)
    def select_service():
        layers = []
        for index, resource in enumerate(state["resources"]):
            # The inventory's own source layer id is authoritative. Do not infer ids from order.
            metadata = resource.get("metadata") or {}
            source_id = metadata.get("layerId")
            if source_id is None:
                match = re.search(r":(?:layer|table):(\d+)$", resource["id"])
                if match:
                    source_id = int(match[1])
            if source_id is None:
                raise ProducerError("inventory has no unambiguous source layer id")
            layers.append({"sourceResourceId": resource["id"], "serviceUrl": source,
                "layerId": source_id, "tableName": f"journey_{tag}_{index}", "targetSchema": "import_fidelity",
                "serviceName": f"fidelity-journey-{tag}"})
        state["layers"] = layers
        return "selected all discovered resources with stable source identifiers"
    score.record("journey.service-selection", select_service)
    def apply():
        if "layers" not in state:
            raise ProducerError("complete service selection did not execute")
        started = sdk.call("StartMigrationBatchAsync", baseUrl=base, candidate=True, arguments=[{
            "sourceKind": "arcgis-geoservices-rest", "sourceUrl": source,
            "sourceDisplayName": "published-SDK fidelity journey", "layers": state["layers"]}])
        if started.get("totalChildren") != len(state["layers"]) or not started.get("batchId"):
            raise ProducerError("batch did not retain the complete selected service")
        state["batchId"] = started["batchId"]
        score.provenance["serviceJourney"] = {"batchId": started["batchId"], "sourceUrl": source,
            "sourceResourceIds": [layer["sourceResourceId"] for layer in state["layers"]]}
        return "SDK queued the complete selected service in a real migration batch"
    score.record("journey.apply", apply)
    def wait():
        if "batchId" not in state:
            raise ProducerError("no real migration batch was queued")
        deadline = time.monotonic() + 300
        while True:
            batch = sdk.call("GetMigrationBatchAsync", baseUrl=base, candidate=True, arguments=[state["batchId"]])
            if batch["status"] not in {"pending", "running", "queued"}:
                break
            if time.monotonic() > deadline:
                raise ProducerError("service migration exceeded 300-second bound")
            time.sleep(1)
        state["batch"] = batch
        if batch["status"] != "succeeded" or len(batch.get("children", [])) != len(state["layers"]) or any(
                child["status"] != "succeeded" or child.get("publishedLayerId") is None for child in batch.get("children", [])):
            raise ProducerError("complete service was not losslessly published: " + batch["status"])
        return "SDK polled all service children to succeeded, with published target mappings"
    score.record("journey.wait", wait)
    def reconcile():
        batch = state.get("batch")
        if not batch or batch["status"] != "succeeded":
            raise ProducerError("service did not succeed; reconciliation cannot pass")
        for child in batch["children"]:
            job = sdk.call("GetGeoservicesImportJobStatusAsync", baseUrl=base, candidate=True, arguments=[child["jobId"]])
            artifact = job.get("reconciliationArtifact")
            catalog = job.get("catalogReconciliationReport")
            if not artifact or not catalog:
                raise ProducerError("missing data or catalog reconciliation; absent evidence is not a pass")
            if artifact.get("classification") != "pass" or catalog.get("classification") != "pass":
                raise ProducerError("server reports non-passing data/catalog reconciliation")
        return "all selected service layers carry passing data and catalog reconciliation"
    score.record("journey.reconciliation", reconcile)


def run_live(score: Scorecard, sdk: Sdk, compose: list[str], selected: list[str]) -> None:
    score.provenance["candidate"] = candidate_identity(score.manifest, compose)
    score.provenance["producer"] = {
        "scriptSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "consumerSha256": hashlib.sha256((HERE / "import-fidelity-consumer/Program.cs").read_bytes()).hexdigest(),
        "requirementsSha256": hashlib.sha256(gate.DEFAULT_REQUIREMENTS.read_bytes()).hexdigest(),
    }
    postgis = sql(compose, "SELECT PostGIS_Full_Version();")
    if "POSTGIS=" not in postgis:
        raise ProducerError("candidate oracle is not real PostGIS")
    score.postgis = True
    score.provenance["postgisVersion"] = postgis
    sql(compose, "CREATE SCHEMA import_fidelity;")
    base = os.environ["HONUA_SERVER_URL"]
    tag = secrets.token_hex(4)
    if set(selected) == set(CASES):
        service_journey(score, sdk, base, tag)
    for name in selected:
        print(f"executing {name} against manifest-pinned candidate", file=sys.stderr, flush=True)
        baseline_case(score, sdk, compose, name, base, tag)
    def immutable_identities():
        imports = score.provenance.get("imports", [])
        if set(selected) != set(CASES) or {item["serviceCase"] for item in imports} != set(CASES):
            raise ProducerError("not every frozen source has a stable snapshot and imported target identity")
        return "manifest package/image/source pins, loaded assemblies, producer bytes and all source snapshots recorded"
    score.record("evidence.immutable-identities", immutable_identities)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-uri", required=True)
    parser.add_argument("--live", action="store_true", help="boot a disposable candidate and real PostGIS; never use a shared server")
    parser.add_argument("--case", action="append", choices=sorted(CASES), help="diagnostic subset only; omitted requirements remain blocked")
    parser.add_argument("--timeout-seconds", type=int, default=1800,
                        help="bound live SDK execution after install/startup (60..3600 seconds)")
    parser.add_argument("--package-only", action="store_true", help="exercise the clean public install only; never qualification")
    args = parser.parse_args(argv)
    if not gate._valid_evidence(args.evidence_uri):
        parser.error("--evidence-uri must be a durable honua-io Actions/evidence URL")
    if not 60 <= args.timeout_seconds <= 3600:
        parser.error("--timeout-seconds must be between 60 and 3600")
    if not args.live and not args.package_only:
        parser.error("use --live or --package-only; shared candidate mutation is not supported")
    installed = installed_module()
    manifest = yaml.safe_load(args.manifest.read_text())
    requirements = gate.load_requirements()
    score = Scorecard(manifest, requirements, args.evidence_uri)
    score.capture_directory = args.output.parent / "import-fidelity-observations"
    compose = None
    sdk = None
    try:
        with tempfile.TemporaryDirectory(prefix="honua-import-fidelity-") as temporary:
            command, env, score.consumer = install_consumer(manifest, Path(temporary) / "published")
            env["IMPORT_FIDELITY_API_KEY"] = os.environ.get("E2E_API_KEY", "honua-console-dev-key")
            sdk = Sdk(command, env, score.consumer)
            score.record("journey.published-package-install", lambda: "clean public NuGet pin and loaded assembly hashes verified")
            if args.live:
                installed = installed_module()
                os.environ["COMPOSE_PROJECT_NAME"] = "honua-import-fidelity-" + secrets.token_hex(5)
                with socket.socket() as port_probe:
                    port_probe.bind(("127.0.0.1", 0))
                    port = port_probe.getsockname()[1]
                os.environ["E2E_SERVER_PORT"] = str(port)
                candidate_url = f"http://127.0.0.1:{port}"
                for key in ("HONUA_SERVER_URL", "E2E_BASE", "HONUA_PUBLIC_BASE_URL"):
                    os.environ[key] = candidate_url
                compose = installed.boot_candidate(manifest, overlay=HERE / "compose.import-fidelity.yml")
                sdk.deadline = time.monotonic() + args.timeout_seconds
                run_live(score, sdk, compose, args.case or sorted(CASES))
    except (ProducerError, OSError, subprocess.SubprocessError, installed.CertificationError) as exc:
        score.differences.append({"code": "producer-execution-incomplete", "severity": "blocking", "reason": str(exc)})
    finally:
        if sdk:
            sdk.close()
        if compose:
            subprocess.run([*compose, "down", "-v"], cwd=ROOT, capture_output=True, check=False)
    receipt = score.receipt()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    verdict = gate.evaluate(manifest, receipt, requirements)
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
