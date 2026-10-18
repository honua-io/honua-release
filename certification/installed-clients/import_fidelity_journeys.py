"""Live authored-source journeys. Unit substitutions never qualify a release.

The authored fixture is an input contract, not a verdict. Each role is inspected
before import and compared with fresh candidate reads and independent PostGIS.
Missing external prerequisites remain blocked in the unchanged frozen scorecard.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import jsonschema
from urllib.parse import urlsplit

FIXTURE_SCHEMA = "honua.import-fidelity-source/v1"
ROLES = ("points", "lines", "polygons", "tables", "domains-subtypes", "related-records",
         "attachments", "zm-curves", "nulls-int64-time", "renderer-time-metadata",
         "filters-crs", "auth", "pagination-limits", "bounded-large-data")


FIXTURES = Path(__file__).with_name("import-fidelity-fixtures.v1.json")


def fixture_revision():
    return "sha256:" + hashlib.sha256(FIXTURES.read_bytes()).hexdigest()


def fixture_features(role):
    definition = json.loads(FIXTURES.read_text())["roles"][role]
    if "features" in definition:
        return definition["features"]
    sequence = definition["sequence"]
    return [{"attributes": {"OBJECTID": i, "label": sequence["labelPrefix"] + str(i),
                            "nullable_value": None if i % 3 == 0 else "retained", "kind": 1 if i % 2 else 2,
                            "observed_at": 1700000000000 + i * 1000},
             "geometry": {"x": sequence["xOrigin"] + i * sequence["step"],
                          "y": sequence["yOrigin"] + i * sequence["step"], "spatialReference": {"wkid": 4326}}}
            for i in range(sequence["firstObjectId"], sequence["firstObjectId"] + sequence["recordCount"])]


def validate_authored_data(role, snapshot, equal):
    """Compare actual source data with our authored inputs, never another target read."""
    expected = fixture_features(role)
    observed = {feature["attributes"]["OBJECTID"]: feature for feature in snapshot["features"]}
    equal([feature["attributes"]["OBJECTID"] for feature in expected], sorted(observed), "fixed authored object identities")
    for feature in expected:
        actual = observed[feature["attributes"]["OBJECTID"]]
        equal(feature["attributes"], {key: actual["attributes"][key] for key in feature["attributes"]}, "fixed authored values")
        if "geometry" in feature:
            geometry = {key: value for key, value in feature["geometry"].items() if key != "spatialReference"}
            equal(geometry, {key: actual.get("geometry", {})[key] for key in geometry}, "fixed authored geometry", 1e-8)
    document = json.loads(FIXTURES.read_text())
    definition = document["roles"][role]
    metadata = snapshot["metadata"]
    fields = {field["name"]: field for field in metadata["fields"]}
    names = set(expected[0]["attributes"])
    for field in document["fieldDefinitions"]:
        if field["name"] in names:
            actual = fields[field["name"]]
            equal(field, {key: actual[key] for key in field if key in actual}, "fixed authored field metadata")
    if role == "domains-subtypes":
        expected = definition["metadata"]
        field = next(field for field in metadata["fields"] if field["name"] == expected["domainField"])
        equal(expected["domain"], field.get("domain"), "fixed authored domain")
        equal(expected["typeIdField"], metadata.get("typeIdField"), "fixed authored subtype field")
        equal(expected["subtypeIds"], sorted(item["id"] for item in metadata.get("types", [])), "fixed authored subtype identities")
    if role == "renderer-time-metadata":
        equal(definition["metadata"]["timeInfo"]["startTimeField"], metadata.get("timeInfo", {}).get("startTimeField"), "fixed authored time field")
        renderer = metadata.get("drawingInfo", {}).get("renderer", {})
        for member in ("type", "symbol"):
            expected_member = definition["metadata"]["renderer"][member]
            actual_member = renderer.get(member)
            if isinstance(expected_member, dict) and isinstance(actual_member, dict):
                actual_member = {key: actual_member[key] for key in expected_member if key in actual_member}
            equal(expected_member, actual_member, "fixed authored renderer " + member)
    if "maximumSourceRecordCount" in definition:
        require(0 < metadata.get("maxRecordCount", 0) <= definition["maximumSourceRecordCount"], "authored paging fixture transfer cap is too large")


class PrerequisiteMissing(RuntimeError):
    """A required input or observation is absent; the requirement cannot qualify."""


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                    allow_nan=False).encode()).hexdigest()


def source_spec(path: Path | None):
    if path is None:
        raise PrerequisiteMissing("authored source manifest is required; public baseline samples do not cover these roles")
    value = json.loads(path.read_text())
    contract = json.loads((Path(__file__).with_name("import-fidelity-source.schema.json")).read_text())
    try:
        jsonschema.Draft202012Validator(contract).validate(value)
    except jsonschema.ValidationError as exc:
        raise ValueError("authored source input violates its closed schema") from exc
    if value.get("schema") != FIXTURE_SCHEMA or set(value.get("fixtures", {})) != set(ROLES):
        raise ValueError("authored source manifest must identify every frozen fixture role")
    require(value.get("fixtureRevision") == fixture_revision(), "authored source uses different fixture seed bytes")
    for role, spec in value["fixtures"].items():
        url = urlsplit(spec["serviceUrl"])
        if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment
                or not re.fullmatch(r".*/(?:FeatureServer|MapServer)", url.path)
                or type(spec.get("layerId")) is not int or spec["layerId"] < 0
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", spec.get("expectedSnapshotDigest", ""))):
            raise ValueError("authored source must pin HTTPS service/layer and the authored snapshot digest")
        if role == "auth" and spec["serviceUrl"] != os.environ.get("IMPORT_FIDELITY_AUTH_SOURCE_URL"):
            raise PrerequisiteMissing("authenticated source must match the separately configured credential destination")
    return value


def require(condition, message):
    if not condition:
        raise ValueError(message)


def rows(snapshot):
    return sorted((feature["attributes"] for feature in snapshot["features"]),
                  key=lambda value: json.dumps(value, sort_keys=True))


def shapes(snapshot):
    oid = snapshot["objectIdField"]
    return sorted([[feature["attributes"][oid], {"present": "geometry" in feature, "value": feature.get("geometry")}]
                   for feature in snapshot["features"]], key=lambda value: value[0])


def schema(snapshot):
    metadata = snapshot["metadata"]
    return {"fields": sorted(metadata["fields"], key=lambda value: value["name"].casefold()),
            **{key: metadata[key] for key in ("geometryType", "hasZ", "hasM", "typeIdField", "types",
                                             "drawingInfo", "timeInfo", "relationships", "hasAttachments") if key in metadata}}


def validate_role(role, snapshot):
    """Refuse vacuous coverage: rich roles must contain the corresponding source data."""
    metadata, features = snapshot["metadata"], snapshot["features"]
    values = [value for feature in features for value in feature["attributes"].values()]
    fields = metadata["fields"]
    require(snapshot["count"] >= 2, "authored fixtures require at least two records")
    if role in {"points", "lines", "polygons"}:
        expected = {"points": "esriGeometryPoint", "lines": "esriGeometryPolyline", "polygons": "esriGeometryPolygon"}[role]
        require(metadata.get("geometryType") == expected and all(feature.get("geometry") for feature in features),
                "geometry fixture has the wrong type or missing shapes")
    elif role == "tables":
        require(not metadata.get("geometryType") and all(not feature.get("geometry") for feature in features),
                "table fixture must be nonspatial")
    elif role == "domains-subtypes":
        require(any(field.get("domain") for field in fields) and metadata.get("typeIdField") and metadata.get("types"),
                "fixture must contain a real coded/range domain and subtype definitions")
    elif role == "related-records":
        require(metadata.get("relationships"), "fixture must advertise actual relationships")
    elif role == "attachments":
        require(metadata.get("hasAttachments") is True, "fixture must support real attachment bytes")
    elif role == "zm-curves":
        require(metadata.get("hasZ") is True and metadata.get("hasM") is True
                and any("curvePaths" in feature.get("geometry", {}) or "curveRings" in feature.get("geometry", {}) for feature in features),
                "fixture must expose Z, M and true curves; a densified query cannot qualify")
    elif role == "nulls-int64-time":
        require(None in values and "" in values and any(type(value) is int and abs(value) > 2**53 for value in values)
                and any(field["type"] == "esriFieldTypeDate" for field in fields),
                "fixture must distinguish null/empty, exact 64-bit integers and dates")
    elif role == "renderer-time-metadata":
        require(metadata.get("drawingInfo", {}).get("renderer") and metadata.get("timeInfo"),
                "fixture must contain renderer and time metadata")
    elif role == "filters-crs":
        require(metadata.get("extent", {}).get("spatialReference", {}).get("wkid") not in {None, 4326},
                "CRS fixture must exercise a real transformation to 4326")
    elif role in {"pagination-limits", "bounded-large-data"}:
        cap = metadata.get("maxRecordCount")
        require(type(cap) is int and cap > 0 and snapshot["count"] > cap,
                "fixture must exceed the advertised source transfer cap")
        if role == "bounded-large-data":
            require(2000 < snapshot["count"] <= 50000, "bounded large fixture must contain 2001..50000 records")


class SourceSdk:
    def __init__(self, sdk, authenticated_url):
        self.sdk, self.authenticated_url = sdk, authenticated_url

    def call(self, operation, **arguments):
        if self.authenticated_url and arguments.get("baseUrl") == self.authenticated_url and not arguments.get("candidate"):
            arguments["sourceAuth"] = True
        return self.sdk.call(operation, **arguments)


class ConfiguredSdk:
    """One named configuration in the actual persistent public-package consumer.

    Snapshot helpers still state their expected endpoint, but that address is only
    checked here; configured-read never sends an endpoint override to the child.
    """
    def __init__(self, sdk, service, layer):
        self.sdk, self.original = sdk, self.target(sdk, service, layer, False)
        self.configuration = sdk.call("configure-readback", revision=0, **self.original)
        self.require_configuration(self.configuration, self.original, 1)
        self.configuration_id = self.configuration["configurationId"]
        self.observations = []

    @staticmethod
    def target(sdk, service, layer, candidate):
        return {"baseUrl": service.rstrip("/"), "layerId": layer, "candidate": candidate,
                "sourceAuth": not candidate and service == getattr(sdk, "authenticated_url", None)}

    @staticmethod
    def require_configuration(value, target, revision, configuration_id=None):
        require(isinstance(value, dict) and set(value) == {*target, "configurationId", "revision"}
                and isinstance(value.get("configurationId"), str) and re.fullmatch(r"[0-9a-f]{32}", value["configurationId"])
                and type(value.get("revision")) is int and value["revision"] == revision
                and all(type(value.get(key)) is type(expected) and value[key] == expected for key, expected in target.items())
                and (configuration_id is None or value["configurationId"] == configuration_id),
                "consumer did not retain the exact named application configuration")

    def repoint(self, service, layer):
        target = self.target(self.sdk, service, layer, True)
        changed = self.sdk.call("repoint-readback", configurationId=self.configuration_id,
                                revision=self.configuration["revision"], **target)
        self.require_configuration(changed, target, self.configuration["revision"] + 1, self.configuration_id)
        self.configuration = changed

    def restore(self):
        restored = self.sdk.call("rollback-readback", configurationId=self.configuration_id,
                                 revision=self.configuration["revision"])
        self.require_configuration(restored, self.original, self.configuration["revision"] + 1, self.configuration_id)
        self.configuration = restored

    def call(self, operation, **arguments):
        expected = self.target(self.sdk, arguments.pop("baseUrl"), arguments.pop("layerId"), arguments.pop("candidate", False))
        self.require_configuration(self.configuration, expected, self.configuration["revision"], self.configuration_id)
        require(operation in {"layer", "count", "ids", "query"} and set(arguments) <= {"query"},
                "configured read is outside the frozen public SDK readback surface")
        observed = self.sdk.call("configured-read", configurationId=self.configuration_id,
                                 revision=self.configuration["revision"], readOperation=operation, **arguments)
        require(isinstance(observed, dict) and set(observed) == {"configuration", "observedRequests", "value"}
                and observed["configuration"] == self.configuration, "read used another application configuration")
        paths = observed["observedRequests"]
        root = expected["baseUrl"] + "/" + str(expected["layerId"])
        path = root if operation == "layer" else root + "/query"
        require(isinstance(paths, list) and 0 < len(paths) <= 32 and all(value == path for value in paths),
                "actual SDK request did not use the configured service and layer")
        self.observations.append({"revision": self.configuration["revision"], "operation": operation, "requestPaths": paths})
        return observed["value"]


class Journeys:
    def __init__(self, producer, score, sdk, compose, base, tag, fixture_path=None):
        self.p, self.score, self.sdk, self.compose, self.base, self.tag = producer, score, sdk, compose, base, tag
        self.fixture_path = fixture_path
        self.states = {}

    def wait(self, job_id):
        deadline = time.monotonic() + 240
        while True:
            progress = self.sdk.call("GetGeoservicesImportJobStatusAsync", baseUrl=self.base,
                                     candidate=True, arguments=[job_id])
            status = str(progress["status"]).lower().replace("-", "")
            if status in {"completed", "failed", "cancelled", "needsreview", "completedwitherrors"}:
                return progress
            require(time.monotonic() < deadline, "authored import exceeded its 240-second bound")
            time.sleep(0.2)

    def request(self, spec, table, service, **overrides):
        request = {"serviceUrl": spec["serviceUrl"], "layerId": spec["layerId"], "tableName": table,
                   "targetSchema": "import_fidelity", "targetSrid": 4326, "autoPublish": True,
                   "serviceName": service, "batchSize": 100, "requestTimeoutSeconds": 60, "maxRetries": 0}
        if spec.get("authenticated"):
            request["credentials"] = {"mode": "token", "accessTokenSecretReference": "env:IMPORT_FIDELITY_SOURCE_TOKEN"}
        request.update(overrides)
        return request

    def start(self, request):
        return self.sdk.call("StartGeoservicesImportAsync", baseUrl=self.base, candidate=True,
                             arguments=[request])["jobId"]

    def complete(self, request):
        job_id = self.start(request)
        progress = self.wait(job_id)
        require(str(progress["status"]).lower() == "completed" and not progress.get("failedFeatures")
                and not progress.get("failedAttachments") and progress.get("publishedLayerId") is not None,
                "authored import did not complete without data/attachment loss: " + str(progress["status"]))
        target_url = self.base.rstrip("/") + "/rest/services/" + request["serviceName"] + "/FeatureServer"
        target = self.p.read_snapshot(self.sdk, target_url, progress["publishedLayerId"], candidate=True)
        return {"jobId": job_id, "progress": progress, "targetUrl": target_url,
                "targetLayer": progress["publishedLayerId"], "target": target, "request": request}

    def attachments(self, service, layer, snapshot, candidate=False):
        result = []
        for oid in snapshot["ids"]:
            infos = self.sdk.call("attachments", baseUrl=service, layerId=layer, objectId=oid, candidate=candidate)
            for info in infos:
                require(type(info.get("attachmentId")) is int, "attachment metadata omitted identity")
                content = self.sdk.call("attachment-hash", baseUrl=service, layerId=layer, objectId=oid,
                                        attachmentId=info["attachmentId"], candidate=candidate)
                require(info.get("size") == content["size"], "attachment content length differs from metadata")
                result.append({"objectId": oid, "name": info.get("name"), "contentType": info.get("contentType"), **content})
        return sorted(result, key=lambda value: json.dumps(value, sort_keys=True))

    def parity(self, state):
        source, target = state["source"], state["target"]
        self.p.require_equal(source["count"], target["count"], "authored counts")
        self.p.require_equal(rows(source), rows(target), "authored exact identities/attributes")
        self.p.require_equal(shapes(source), shapes(target), "authored geometries including Z/M/curves", 1e-8)
        # Compare declared source members only; target capabilities may include additional members.
        target_fields = {field["name"].casefold(): field for field in target["metadata"]["fields"]}
        for field in source["metadata"]["fields"]:
            if field["type"] == "esriFieldTypeGeometry":
                continue
            other = target_fields.get(field["name"].casefold(), {})
            self.p.require_equal(field, {key: other[key] for key in field if key in other}, "authored field metadata")
        for key in ("hasZ", "hasM", "typeIdField", "types", "drawingInfo", "timeInfo", "hasAttachments"):
            if key in source["metadata"]:
                self.p.require_equal(source["metadata"][key], target["metadata"].get(key), "authored " + key)
        count = int(self.p.sql(self.compose, 'SELECT count(*) FROM import_fidelity."' + state["request"]["tableName"] + '";'))
        self.p.require_equal(source["count"], count, "independent authored PostGIS count")
        return "fresh authored source/target values, schema, metadata, geometry and physical count agree"

    def fixture(self, role, spec):
        if role == "related-records":
            return self.relationship_fixture(spec)
        source = self.p.read_snapshot(self.sdk, spec["serviceUrl"], spec["layerId"])
        validate_role(role, source)
        validate_authored_data(role, source, self.p.require_equal)
        self.p.require_equal(spec["expectedSnapshotDigest"], digest(source), "authored source snapshot pin")
        table = "authored_" + role.replace("-", "_") + "_" + self.tag
        request = self.request(spec, table, "authored-" + role + "-" + self.tag)
        full_source = source
        if role == "filters-crs":
            query = spec.get("filter")
            require(isinstance(query, dict) and query.get("where") not in {None, "", "1=1"} and query.get("spatialFilter"),
                    "filter fixture requires both an attribute predicate and spatial predicate")
            request["whereClause"] = query["where"]
            source = self.p.read_snapshot(self.sdk, spec["serviceUrl"], spec["layerId"], where=query["where"])
            self.p.require_equal(spec["expectedFilteredSnapshotDigest"], digest(source), "authored import filter snapshot pin")
            require(source["count"] < full_source["count"], "import predicate must exclude actual source records")
        state = self.complete(request)
        state.update(source=source, spec=spec)
        self.states[role] = state
        detail = self.parity(state)
        self.p.require_equal(full_source, self.p.read_snapshot(self.sdk, spec["serviceUrl"], spec["layerId"]), "authored source stability")
        if role == "attachments":
            state["sourceAttachments"] = self.attachments(spec["serviceUrl"], spec["layerId"], source)
            state["targetAttachments"] = self.attachments(state["targetUrl"], state["targetLayer"], state["target"], True)
            require(len(state["sourceAttachments"]) >= 2, "attachment fixture must contain at least two actual attachments")
            definitions = json.loads(FIXTURES.read_text())["roles"]["attachments"]["attachments"]
            expected_attachments = sorted([{key: value for key, value in item.items() if key != "contentUtf8"} for item in definitions],
                                          key=lambda value: json.dumps(value, sort_keys=True))
            self.p.require_equal(expected_attachments, state["sourceAttachments"], "fixed authored attachment bytes")
            self.p.require_equal(state["sourceAttachments"], state["targetAttachments"], "attachment names, MIME, lengths and byte hashes")
        if role == "filters-crs":
            query = spec.get("filter")
            require(isinstance(query, dict) and query.get("where") not in {None, "", "1=1"} and query.get("spatialFilter"),
                    "filter fixture requires both an attribute predicate and spatial predicate")
            a = self.sdk.call("query", baseUrl=spec["serviceUrl"], layerId=spec["layerId"], query={**query, "outSR": 4326, "outFields": "*"})
            b = self.sdk.call("query", baseUrl=state["targetUrl"], layerId=state["targetLayer"], candidate=True,
                              query={**query, "outSR": 4326, "outFields": "*"})
            require(0 < len(a["features"]) < full_source["count"], "filter must actually exclude some source rows")
            self.p.require_equal(rows({"features": a["features"]}), rows({"features": b["features"]}), "attribute/spatial filter values")
            self.p.require_equal(shapes({"features": a["features"], "objectIdField": source["objectIdField"]}),
                                 shapes({"features": b["features"], "objectIdField": source["objectIdField"]}),
                                 "spatial filter and transformed CRS", 1e-8)
        if role == "auth":
            # The same source must refuse anonymous access; an open source cannot qualify auth.
            try:
                self.sdk.sdk.call("layer", baseUrl=spec["serviceUrl"], layerId=spec["layerId"])
            except self.p.ProducerError as exc:
                require(re.search(r"(?:httpStatus=(?:401|403)|code=(?:401|403|498|499))\b", str(exc)),
                        "anonymous source failed without an authentication refusal")
            else:
                raise ValueError("authenticated source also allowed anonymous access")
        if self.score.capture_directory:
            self.score.capture_directory.mkdir(parents=True, exist_ok=True)
            (self.score.capture_directory / ("authored-" + role + ".json")).write_text(json.dumps(state, indent=2) + "\n")
        self.score.provenance.setdefault("authoredImports", {})[role] = {
            "jobId": state["jobId"], "sourceUrl": spec["serviceUrl"], "sourceLayerId": spec["layerId"],
            "targetUrl": state["targetUrl"], "targetLayer": state["targetLayer"],
            "sourceDigest": digest(source), "targetDigest": digest(state["target"])}
        return detail

    def relationship_fixture(self, spec):
        source_url = spec["serviceUrl"]
        inventory = self.sdk.call("ScanMigrationSourceAsync", baseUrl=self.base, candidate=True,
                                  arguments=[{"sourceKind": "arcgis-geoservices-rest", "sourceUrl": source_url}])
        self.p.require_equal(spec["expectedInventoryDigest"], digest(inventory), "authored relationship inventory pin")
        resources = inventory["resources"]
        specifications = spec["relatedLayers"]
        require(len(specifications) >= 2 and len(specifications) == len(resources),
                "relationship fixture must select the complete service, including both ends")
        require({item["sourceResourceId"] for item in specifications} == {item["id"] for item in resources},
                "relationship fixture resource IDs differ from live inventory")
        manifest = spec["migrationManifest"]
        require(manifest["source"]["baseUrl"] == source_url, "relationship manifest source identity differs")
        snapshots, layers = {}, []
        service = "relationships-" + self.tag
        for index, item in enumerate(specifications):
            source = self.p.read_snapshot(self.sdk, source_url, item["layerId"])
            self.p.require_equal(item["expectedSnapshotDigest"], digest(source), "authored related layer pin")
            snapshots[item["layerId"]] = source
            layers.append({"sourceResourceId": item["sourceResourceId"], "serviceUrl": source_url,
                           "layerId": item["layerId"], "tableName": f"relation_{self.tag}_{index}",
                           "targetSchema": "import_fidelity", "serviceName": service})
        root = snapshots[spec["layerId"]]
        validate_role("related-records", root)
        validate_authored_data("related-records", root, self.p.require_equal)
        self.p.require_equal(spec["expectedSnapshotDigest"], digest(root), "relationship root snapshot pin")
        started = self.sdk.call("StartMigrationBatchAsync", baseUrl=self.base, candidate=True, arguments=[{
            "sourceKind": "arcgis-geoservices-rest", "sourceUrl": source_url, "layers": layers,
            "manifestBody": json.dumps(manifest), "applyRelationships": True}])
        deadline = time.monotonic() + 300
        while True:
            batch = self.sdk.call("GetMigrationBatchAsync", baseUrl=self.base, candidate=True, arguments=[started["batchId"]])
            if batch["status"] not in {"running", "pending", "queued"}:
                break
            require(time.monotonic() < deadline, "related service batch exceeded 300 seconds")
            time.sleep(0.2)
        require(batch["status"] == "succeeded" and batch.get("relationshipsApplied") is True
                and len(batch["children"]) == len(layers), "related service did not publish and apply every relationship")
        states = {}
        for child in batch["children"]:
            require(child["status"] == "succeeded" and child.get("publishedLayerId") is not None,
                    "related service omitted a published child")
            layer = next(item for item in layers if item["sourceResourceId"] == child["sourceResourceId"])
            url = self.base.rstrip("/") + "/rest/services/" + service + "/FeatureServer"
            state = {"jobId": child["jobId"], "targetUrl": url, "targetLayer": child["publishedLayerId"],
                     "source": snapshots[layer["layerId"]], "request": layer,
                     "target": self.p.read_snapshot(self.sdk, url, child["publishedLayerId"], candidate=True)}
            self.parity(state)
            states[layer["layerId"]] = state
        declared = root["metadata"]["relationships"]
        relationships = spec["relationships"]
        require(relationships and {item["relationshipId"] for item in relationships} == {item["id"] for item in declared},
                "authored edge checks must account for every declared root relationship")
        edges = []
        for relation in relationships:
            require(relation["originLayerId"] == spec["layerId"], "edge origin must match root relationship layer")
            original = next(item for item in declared if item["id"] == relation["relationshipId"])
            a, b = states[relation["originLayerId"]], states[relation["destinationLayerId"]]
            key, foreign = relation["keyField"], relation["foreignKey"]
            require(all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in (key, foreign)), "unsafe relationship field")
            require(original["relatedTableId"] == relation["destinationLayerId"] and original["keyField"] == key,
                    "authored edge keys disagree with declared source relationship")
            target_relations = a["target"]["metadata"].get("relationships", [])
            matching = [item for item in target_relations if item.get("name") == original.get("name")]
            require(len(matching) == 1, "target omitted or duplicated relationship metadata")
            target = matching[0]
            for member in ("name", "role", "cardinality", "keyField", "composite"):
                if member in original:
                    self.p.require_equal(original[member], target.get(member), "relationship metadata " + member)
            self.p.require_equal(b["targetLayer"], target.get("relatedTableId"), "relationship published destination")
            ao, bo = a["source"]["objectIdField"], b["source"]["objectIdField"]
            require(all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in (ao, bo)), "unsafe relationship OID")
            expected = sorted([[x[ao], y[bo]] for x in rows(a["source"]) for y in rows(b["source"])
                               if x[key] is not None and x[key] == y[foreign]])
            require(expected, "relationship fixture contains no related records")
            authored = json.loads(FIXTURES.read_text())["roles"]["related-records"]
            self.p.require_equal(authored["relationship"]["name"], original["name"], "fixed authored relationship name")
            self.p.require_equal(authored["relationship"]["pairs"], expected, "fixed authored relationship pairs")
            expected_children = [feature["attributes"] for feature in authored["relatedFeatures"]]
            actual_children = [{key: row[key] for key in expected_children[0]} for row in rows(b["source"])]
            self.p.require_equal(expected_children, actual_children, "fixed authored child values")
            statement = (f"SELECT coalesce(json_agg(json_build_array(a.\"{ao}\",b.\"{bo}\") "
                         f"ORDER BY a.\"{ao}\",b.\"{bo}\"),'[]'::json) "
                         f"FROM import_fidelity.\"{a['request']['tableName']}\" a "
                         f"JOIN import_fidelity.\"{b['request']['tableName']}\" b ON a.\"{key}\"=b.\"{foreign}\";")
            measured = json.loads(self.p.sql(self.compose, statement))
            self.p.require_equal(expected, measured, "independent physical relationship edges")
            edges.append({"name": original["name"], "pairs": measured})
        state = states[spec["layerId"]]
        state.update(spec=spec, relationships=edges)
        self.states["related-records"] = state
        for item in specifications:
            self.p.require_equal(snapshots[item["layerId"]], self.p.read_snapshot(self.sdk, source_url, item["layerId"]),
                                 "related source stability")
        self.score.provenance["relatedService"] = {"batchId": batch["batchId"], "sourceUrl": source_url,
                                                   "relationships": edges, "publishedLayers": [s["targetLayer"] for s in states.values()]}
        if self.score.capture_directory:
            self.score.capture_directory.mkdir(parents=True, exist_ok=True)
            (self.score.capture_directory / "authored-related-records.json").write_text(json.dumps({"layers": states, "batch": batch}, indent=2) + "\n")
        return "complete service applied every relationship; metadata and all independent PostGIS join edges agree"

    def ready(self, role):
        if self.score.cells["fixture." + role]["verdict"] != "pass":
            raise PrerequisiteMissing("requires a passing live authored " + role + " import")
        return self.states[role]

    def fingerprint(self, table):
        # Tables are generated locally, never interpolated from an external fixture or receipt.
        require(re.fullmatch(r"[a-z0-9_]+", table), "invalid generated target table")
        return self.p.sql(self.compose, 'SELECT count(*)::text || \':\' || md5(coalesce(string_agg(row_to_json(t)::text,\'\' ORDER BY row_to_json(t)::text),\'\')) FROM import_fidelity."' + table + '" t;')

    def lease_free(self, table):
        require(re.fullmatch(r"[a-z0-9_]+", table), "invalid generated import-lease target")
        # The real importer takes this session lease before source transfer and releases
        # it in finally, after commit/rollback, publication, attachments and reconciliation.
        # psql's short-lived session releases a successful probe automatically on exit.
        key = f'honua.geoservices-import:"import_fidelity"."{table}"'
        result = self.p.sql(self.compose, f"SELECT pg_try_advisory_lock(hashtextextended('{key}',0));")
        require(result in {"t", "f"}, "independent import lease probe returned an unknown result")
        return result == "t"

    def wait_for_lease_release(self, table):
        deadline = time.monotonic() + 30
        while not self.lease_free(table):
            require(time.monotonic() < deadline, "cancel/failure acknowledgement arrived before worker lease release")
            time.sleep(0.1)

    def cancellation(self):
        prior, large = self.ready("points"), self.ready("bounded-large-data")
        table = prior["request"]["tableName"]
        before = self.fingerprint(table)
        request = self.request(large["spec"], table, prior["request"]["serviceName"], overwriteExisting=True, batchSize=1)
        require(self.lease_free(table), "prior import worker still owns the target before cancellation test")
        job_id = self.start(request)
        deadline = time.monotonic() + 30
        while True:
            observed = self.sdk.call("GetGeoservicesImportJobStatusAsync", baseUrl=self.base, candidate=True, arguments=[job_id])
            require(observed.get("jobId") == job_id, "cancellation progress identity differs from the started job")
            status = str(observed["status"]).lower()
            require(status not in {"completed", "failed", "cancelled", "needsreview"},
                    "cancellation requires observing partial real transfer before termination")
            processed = observed.get("featuresProcessed", 0)
            if status in {"retrievingfeatures", "insertingfeatures"} and 0 < processed < large["source"]["count"] and not self.lease_free(table):
                break
            require(time.monotonic() < deadline, "did not observe actual partial transfer while the import worker held its lease")
            time.sleep(0.1)
        self.sdk.call("CancelGeoservicesImportJobAsync", baseUrl=self.base, candidate=True, arguments=[job_id])
        # Cancelled is an endpoint acknowledgement until the real session lease ends.
        cancelled = self.wait(job_id)
        require(str(cancelled["status"]).lower() == "cancelled", "cancel request did not produce the expected cancelled outcome")
        self.wait_for_lease_release(table)
        cancelled = self.sdk.call("GetGeoservicesImportJobStatusAsync", baseUrl=self.base, candidate=True, arguments=[job_id])
        require(cancelled.get("jobId") == job_id and str(cancelled["status"]).lower() == "cancelled",
                "worker completion changed the cancellation into a non-cancelled outcome")
        require(cancelled.get("dataCommitted") is not True, "cancelled worker explicitly reported committed data")
        self.p.require_equal(before, self.fingerprint(table), "cancelled overwrite preserves every prior physical row after worker cessation")
        retained = self.p.read_snapshot(self.sdk, prior["targetUrl"], prior["targetLayer"], candidate=True)
        self.parity({**prior, "target": retained})
        recovered = self.complete({**request, "batchSize": 100})
        recovered.update(source=large["source"])
        self.parity(recovered)
        self.score.provenance["cancellationRecovery"] = {"cancelledJobId": job_id, "recoveryJobId": recovered["jobId"],
                                                         "priorFingerprint": before, "cancelled": cancelled, "partialTransferObserved": observed,
                                                         "workerLeaseHeldBeforeCancel": True, "workerLeaseReleasedAfterCancel": True}
        # Subsequent cases use the original points table; restore it through the same SDK path.
        restored = self.complete({**prior["request"], "overwriteExisting": True})
        restored.update(source=prior["source"], spec=prior["spec"])
        self.parity(restored)
        self.states["points"] = restored
        return "actual partial transfer cancelled; worker lease ended with every prior row/publication retained; fresh retry imported all rows without duplicates"

    def injected_failure(self):
        state = self.ready("points")
        request = {**state["request"], "overwriteExisting": True, "whereClause": "__missing_fidelity_column__ = 1"}
        before = self.fingerprint(request["tableName"])
        failed = self.wait(self.start(request))
        require(str(failed["status"]).lower() in {"failed", "needsreview"} and failed.get("dataCommitted") is not True,
                "injected invalid source predicate must fail explicitly without committing")
        self.wait_for_lease_release(request["tableName"])
        self.p.require_equal(before, self.fingerprint(request["tableName"]), "failed overwrite retained every prior row")
        fresh = self.p.read_snapshot(self.sdk, state["targetUrl"], state["targetLayer"], candidate=True)
        self.p.require_equal(rows(state["source"]), rows(fresh), "failed overwrite retained published values")
        self.score.provenance["injectedFailure"] = {"progress": failed, "priorFingerprint": before}
        return "a real source query fault failed without false publication or loss of the complete prior target"

    def rollback(self):
        state = self.ready("points")
        source_url, source_layer = state["spec"]["serviceUrl"], state["spec"]["layerId"]
        client = ConfiguredSdk(self.sdk, source_url, source_layer)
        original = self.p.read_snapshot(client, source_url, source_layer)
        self.p.require_equal(state["source"], original, "configured original source snapshot")
        # A discriminating target value makes a no-op switch or cached source response fail.
        # Mutate only the owned imported fixture, never the external original source.
        table, oid = state["request"]["tableName"], original["objectIdField"]
        require(all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in (table, oid)), "unsafe rollback fixture identifier")
        require(re.fullmatch(r"[A-Za-z0-9_]+", self.tag), "unsafe rollback fixture marker")
        require(all(isinstance(feature["attributes"].get("label"), str) for feature in original["features"]),
                "rollback requires the authored nonempty text-label fixture")
        marker = "repoint_" + self.tag
        expected = deepcopy(state["target"])
        for feature in expected["features"]:
            feature["attributes"]["label"] = marker
        require(rows(expected) != rows(original), "rollback target must discriminate source from candidate")
        switched = False
        restored_client = False
        try:
            self.p.sql(self.compose, f"""UPDATE import_fidelity."{table}" SET "label"='{marker}';""")
            client.repoint(state["targetUrl"], state["targetLayer"])
            switched = True
            fresh = self.p.read_snapshot(client, state["targetUrl"], state["targetLayer"], candidate=True)
            self.p.require_equal(expected, fresh, "configured candidate exact readback after repoint")
            require(rows(fresh) != rows(original), "configured candidate returned the original cached source")
            client.restore()
            restored_client = True
            reverted = self.p.read_snapshot(client, source_url, source_layer)
            self.p.require_equal(original, reverted, "same configured client readback after rollback")
            self.p.require_equal(state["source"], self.p.read_snapshot(self.sdk, source_url, source_layer), "original source remained unchanged")
        finally:
            try:
                if switched and not restored_client:
                    client.restore()
            finally:
                restored = self.complete({**state["request"], "overwriteExisting": True})
                restored.update(source=state["source"], spec=state["spec"])
                self.parity(restored)
                self.states["points"] = restored
        self.score.provenance["rollbackRepoint"] = {
            "configurationId": client.configuration_id, "configurationRevisions": [1, 2, 3],
            "sourceUrl": source_url, "sourceLayer": source_layer,
            "candidateUrl": state["targetUrl"], "candidateLayer": state["targetLayer"],
            "originalDigest": digest(original), "switchedDigest": digest(fresh), "restoredDigest": digest(reverted),
            "actualSdkRequests": client.observations, "ownedFixtureRestored": True}
        return "one persistent published-SDK application configuration switched to discriminating imported data and restored exact original readback; fixture restored"

    def independent_geometry(self):
        state = self.ready("points")
        oid = state["source"]["objectIdField"]
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", oid), "independent geometry reader needs a safe declared OID")
        table = state["request"]["tableName"]
        physical = json.loads(self.p.sql(self.compose, 'SELECT json_agg(json_build_object(\'id\',"' + oid + '\",\'geometry\',ST_AsGeoJSON(ST_Transform(geom,4326),15)::json) ORDER BY "' + oid + '\") FROM import_fidelity."' + table + '";'))
        expected = []
        for feature in state["source"]["features"]:
            geometry = feature["geometry"]
            require(set(geometry) >= {"x", "y"}, "independent point geometry oracle requires real XY source")
            expected.append({"id": feature["attributes"][oid], "geometry": {"type": "Point", "coordinates": [geometry["x"], geometry["y"]]}})
        expected.sort(key=lambda value: value["id"])
        self.p.require_equal(expected, physical, "independent PostGIS vector readback versus source XY", 1e-8)
        self.score.provenance["independentGeometry"] = {"reader": "PostGIS ST_AsGeoJSON/ST_Transform", "measuredCount": len(physical),
                                                       "measuredDigest": digest(physical), "sourceDigest": digest(expected)}
        return "real measured PostGIS vector envelope/count/coordinates agree with the independent source; no baseline self-comparison"

    def geojson_equal(self, expected, actual, message):
        self.p.require_equal(len(expected), len(actual), message + " record count")
        for left, right in zip(expected, actual):
            self.p.require_equal(left["id"], right["id"], message + " identity")
            self.p.require_equal(left["properties"], right["properties"], message + " exact attributes")
            self.p.require_equal(left["geometry"], right["geometry"], message + " geometry", 1e-8)

    def dependency_evidence(self):
        self.independent_geometry()
        if not shutil.which("ogr2ogr"):
            raise PrerequisiteMissing("independent GDAL ogr2ogr reader is required for the server-4420 output check")
        state = self.ready("points")
        observed = self.p.geojson_snapshot(self.sdk, state["targetUrl"], state["targetLayer"], state["target"], candidate=True)
        expected = self.p.geojson_snapshot(self.sdk, state["spec"]["serviceUrl"], state["spec"]["layerId"], state["source"])
        self.geojson_equal(expected, observed, "live source/target SDK GeoJSON before independent vector decode")
        directory = self.score.capture_directory
        require(directory is not None, "independent vector outputs must be retained")
        directory.mkdir(parents=True, exist_ok=True)
        source_path, output_path = directory / "measured-target.geojson", directory / "gdal-readback.geojson"
        require(not output_path.exists(), "GDAL output must not exist before independent reader execution")
        source_path.write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": feature["id"], "properties": feature["properties"], "geometry": feature["geometry"]["value"]}
            for feature in observed]}) + "\n")
        process = subprocess.run(["ogr2ogr", "-f", "GeoJSON", "-lco", "COORDINATE_PRECISION=15", "-preserve_fid",
                                  str(output_path), str(source_path)], capture_output=True, text=True, timeout=60, check=False)
        require(process.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0,
                "independent GDAL output is absent, empty or failed; no baseline substitution is permitted")
        decoded = json.loads(output_path.read_text())
        require(decoded.get("type") == "FeatureCollection" and len(decoded.get("features", [])) == len(expected),
                "GDAL measured output did not account for every imported feature")
        oid = state["source"]["objectIdField"].lower()
        measured = []
        fields = set(expected[0]["properties"])
        for feature in decoded["features"]:
            properties = {key.lower(): value for key, value in feature["properties"].items()}
            require(fields <= properties.keys(), "GDAL output dropped an imported field")
            measured.append({"id": properties[oid], "properties": {field: properties[field] for field in fields},
                             "geometry": {"present": "geometry" in feature, "value": feature.get("geometry")}})
        measured.sort(key=lambda feature: feature["id"])
        self.geojson_equal(expected, measured, "independent measured GDAL values and vector coordinates")
        version = subprocess.run(["ogr2ogr", "--version"], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        self.score.provenance["independentVectorOutput"] = {"reader": version, "measuredCount": len(measured),
            "inputSha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "outputSha256": hashlib.sha256(output_path.read_bytes()).hexdigest()}
        return "real PostGIS vector readback and fresh, existing GDAL-decoded SDK output match independent source values/geometry/count"

    def rejects_data_loss(self):
        state = self.ready("points")
        temporal = self.ready("renderer-time-metadata")
        table = state["request"]["tableName"]
        # A committed geometry fault must be visible through the real SDK and the
        # independent vector reader, then the original publication is restored by SDK import.
        self.p.sql(self.compose, 'UPDATE import_fidelity."' + table + '" SET geom=ST_Translate(geom,1,1);')
        try:
            fresh = self.p.read_snapshot(self.sdk, state["targetUrl"], state["targetLayer"], candidate=True)
            require(not self.p.equal(shapes(state["source"]), shapes(fresh), 1e-8),
                    "published SDK did not expose the deliberately corrupted geometry")
            try:
                self.independent_geometry()
            except self.p.ProducerError:
                pass
            else:
                raise ValueError("independent vector reader accepted seeded geometry loss")
        finally:
            restored = self.complete({**state["request"], "overwriteExisting": True})
            restored.update(source=state["source"], spec=state["spec"])
            self.parity(restored)
            self.states["points"] = restored
        state = self.states["points"]
        candidates = [field["name"] for field in state["source"]["metadata"]["fields"]
                      if field["type"] not in {"esriFieldTypeOID", "esriFieldTypeGeometry"} and field.get("nullable") is True
                      and any(feature["attributes"].get(field["name"]) is not None for feature in state["source"]["features"])]
        require(candidates, "data-loss control requires a populated nullable source attribute")
        field = candidates[0]
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field), "unsafe attribute fault field")
        self.p.sql(self.compose, f'UPDATE import_fidelity."{table}" SET "{field}"=NULL;')
        try:
            fresh = self.p.read_snapshot(self.sdk, state["targetUrl"], state["targetLayer"], candidate=True)
            require(not self.p.equal(rows(state["source"]), rows(fresh)), "SDK accepted deliberately lost attribute values")
        finally:
            restored = self.complete({**state["request"], "overwriteExisting": True})
            restored.update(source=state["source"], spec=state["spec"])
            self.parity(restored)
            self.states["points"] = restored
        service = temporal["request"]["serviceName"]
        original = temporal["source"]["metadata"]["timeInfo"]
        require(original.get("startTimeField"), "metadata fault control requires a populated start-time field")
        try:
            self.sdk.call("UpdateLayerMetadataAsync", baseUrl=self.base, candidate=True,
                          arguments=[service, temporal["targetLayer"], {"timeInfo": {"startTimeField": ""}}])
            metadata = self.sdk.call("layer", baseUrl=temporal["targetUrl"], layerId=temporal["targetLayer"], candidate=True)
            require(not self.p.equal(original, metadata.get("timeInfo")), "SDK accepted deliberately lost time metadata")
        finally:
            self.sdk.call("UpdateLayerMetadataAsync", baseUrl=self.base, candidate=True, arguments=[service, temporal["targetLayer"], {
                "timeInfo": {key: original.get(key) or "" for key in ("startTimeField", "endTimeField", "trackIdField")}}])
            temporal["target"] = self.p.read_snapshot(self.sdk, temporal["targetUrl"], temporal["targetLayer"], candidate=True)
            self.parity(temporal)
        self.score.provenance["lossControls"] = {"geometry": "committed-translation-rejected", "values": "committed-null-rejected",
                                                "metadata": "cleared-time-field-rejected", "restoration": "fresh-SDK-parity"}
        return "real SDK/physical oracles rejected committed geometry, attribute and time-metadata faults; SDK restore re-established full parity"

    def run(self):
        try:
            document = source_spec(self.fixture_path)
        except (PrerequisiteMissing, ValueError, KeyError, TypeError) as exc:
            for role in ROLES:
                self.score.block("fixture." + role, str(exc))
        else:
            self.score.provenance["authoredFixtureRevision"] = fixture_revision()
            self.score.provenance["authoredSourceManifestDigest"] = digest(document)
            for role in ROLES:
                spec = {**document["fixtures"][role], "authenticated": role == "auth"}
                self.score.record("fixture." + role, lambda role=role, spec=spec: self.fixture(role, spec))
        for cell, action in (("journey.cancellation-recovery", self.cancellation),
                             ("fixture.injected-failure", self.injected_failure),
                             ("evidence.rollback-repoint", self.rollback),
                             ("evidence.rejects-data-loss", self.rejects_data_loss),
                             ("dependency.server-4420", self.dependency_evidence)):
            self.score.record(cell, action)
        return self.states
