"""Live authored-source journeys. Unit substitutions never qualify a release.

The authored fixture is an input contract, not a verdict. Each role is inspected
before import and compared with fresh candidate reads and independent PostGIS.
Missing external prerequisites remain blocked in the unchanged frozen scorecard.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

FIXTURE_SCHEMA = "honua.import-fidelity-source/v1"
ROLES = ("points", "lines", "polygons", "tables", "domains-subtypes", "related-records",
         "attachments", "zm-curves", "nulls-int64-time", "renderer-time-metadata",
         "filters-crs", "auth", "pagination-limits", "bounded-large-data")


class PrerequisiteMissing(RuntimeError):
    """No execution occurred; this must not become a passing or executed cell."""


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                    allow_nan=False).encode()).hexdigest()


def source_spec(path: Path | None):
    if path is None:
        raise PrerequisiteMissing("authored source manifest is required; public baseline samples do not cover these roles")
    value = json.loads(path.read_text())
    if value.get("schema") != FIXTURE_SCHEMA or set(value.get("fixtures", {})) != set(ROLES):
        raise ValueError("authored source manifest must identify every frozen fixture role")
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
        self.p.require_equal(spec["expectedSnapshotDigest"], digest(source), "authored source snapshot pin")
        table = "authored_" + role.replace("-", "_") + "_" + self.tag
        request = self.request(spec, table, "authored-" + role + "-" + self.tag)
        state = self.complete(request)
        state.update(source=source, spec=spec)
        self.states[role] = state
        detail = self.parity(state)
        self.p.require_equal(source, self.p.read_snapshot(self.sdk, spec["serviceUrl"], spec["layerId"]), "authored source stability")
        if role == "attachments":
            state["sourceAttachments"] = self.attachments(spec["serviceUrl"], spec["layerId"], source)
            state["targetAttachments"] = self.attachments(state["targetUrl"], state["targetLayer"], state["target"], True)
            require(len(state["sourceAttachments"]) >= 2, "attachment fixture must contain at least two actual attachments")
            self.p.require_equal(state["sourceAttachments"], state["targetAttachments"], "attachment names, MIME, lengths and byte hashes")
        if role == "filters-crs":
            query = spec.get("filter")
            require(isinstance(query, dict) and query.get("where") not in {None, "", "1=1"} and query.get("spatialFilter"),
                    "filter fixture requires both an attribute predicate and spatial predicate")
            a = self.sdk.call("query", baseUrl=spec["serviceUrl"], layerId=spec["layerId"], query={**query, "outSR": 4326, "outFields": "*"})
            b = self.sdk.call("query", baseUrl=state["targetUrl"], layerId=state["targetLayer"], candidate=True,
                              query={**query, "outSR": 4326, "outFields": "*"})
            require(0 < len(a["features"]) < source["count"], "filter must actually exclude some source rows")
            self.p.require_equal(a["features"], b["features"], "attribute/spatial filter and transformed CRS", 1e-8)
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

    def cancellation(self):
        prior, large = self.ready("points"), self.ready("bounded-large-data")
        table = prior["request"]["tableName"]
        before = self.fingerprint(table)
        request = self.request(large["spec"], table, "recovered-" + self.tag, overwriteExisting=True, batchSize=1)
        job_id = self.start(request)
        observed = self.sdk.call("GetGeoservicesImportJobStatusAsync", baseUrl=self.base, candidate=True, arguments=[job_id])
        require(str(observed["status"]).lower() not in {"completed", "failed", "cancelled", "needsreview"},
                "cancellation requires observing a nonterminal real job")
        self.sdk.call("CancelGeoservicesImportJobAsync", baseUrl=self.base, candidate=True, arguments=[job_id])
        cancelled = self.wait(job_id)
        if "dataCommitted" not in cancelled:
            raise PrerequisiteMissing("published SDK progress model does not expose explicit DataCommitted; cancellation cannot qualify")
        require(str(cancelled["status"]).lower() == "cancelled" and cancelled.get("dataCommitted") is False,
                "cancellation did not confirm cancelled before commit")
        self.p.require_equal(before, self.fingerprint(table), "cancelled overwrite preserves every prior physical row")
        recovered = self.complete({**request, "batchSize": 100})
        recovered.update(source=large["source"])
        self.parity(recovered)
        self.score.provenance["cancellationRecovery"] = {"cancelledJobId": job_id, "recoveryJobId": recovered["jobId"],
                                                         "priorFingerprint": before, "cancelled": cancelled}
        # Subsequent cases use the original points table; restore it through the same SDK path.
        restored = self.complete({**prior["request"], "overwriteExisting": True})
        restored.update(source=prior["source"], spec=prior["spec"])
        self.parity(restored)
        self.states["points"] = restored
        return "observed cancellation before commit preserved prior data; explicit fresh retry imported every row without duplicates"

    def injected_failure(self):
        state = self.ready("points")
        request = {**state["request"], "overwriteExisting": True, "whereClause": "__missing_fidelity_column__ = 1"}
        before = self.fingerprint(request["tableName"])
        failed = self.wait(self.start(request))
        if "dataCommitted" not in failed:
            raise PrerequisiteMissing("published SDK progress model does not expose explicit DataCommitted; injected failure cannot qualify")
        require(str(failed["status"]).lower() in {"failed", "needsreview"} and failed.get("dataCommitted") is False,
                "injected invalid source predicate must fail explicitly without committing")
        self.p.require_equal(before, self.fingerprint(request["tableName"]), "failed overwrite retained every prior row")
        fresh = self.p.read_snapshot(self.sdk, state["targetUrl"], state["targetLayer"], candidate=True)
        self.p.require_equal(rows(state["source"]), rows(fresh), "failed overwrite retained published values")
        self.score.provenance["injectedFailure"] = {"progress": failed, "priorFingerprint": before}
        return "a real source query fault failed without false publication or loss of the complete prior target"

    def rollback(self):
        prior = self.ready("points")
        # Retain both immutable publications. Repoint the client configuration to v2 and
        # roll it back to v1, reading all records after each switch. No route alias is claimed.
        second = self.complete(self.request(prior["spec"], "repoint_" + self.tag, "repoint-" + self.tag))
        before = self.fingerprint(prior["request"]["tableName"])
        configuration = {"serviceUrl": second["targetUrl"], "layerId": second["targetLayer"]}
        for target in (configuration, {"serviceUrl": prior["targetUrl"], "layerId": prior["targetLayer"]}):
            fresh = self.p.read_snapshot(self.sdk, target["serviceUrl"], target["layerId"], candidate=True)
            self.p.require_equal(rows(prior["source"]), rows(fresh), "client repoint/rollback attributes")
            self.p.require_equal(shapes(prior["source"]), shapes(fresh), "client repoint/rollback geometry", 1e-8)
        self.p.require_equal(before, self.fingerprint(prior["request"]["tableName"]), "rollback preserved prior physical publication")
        self.score.provenance["rollbackRepoint"] = {"forward": configuration,
            "rollback": {"serviceUrl": prior["targetUrl"], "layerId": prior["targetLayer"]}, "priorFingerprint": before}
        return "client URL/layer repointed to a fresh publication and rolled back to retained v1 with exact data and geometry"

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

    def rejects_data_loss(self):
        state = self.ready("points")
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
        return "real SDK and independent vector oracle rejected a committed seeded geometry fault; clean SDK reimport restored parity"

    def run(self):
        try:
            document = source_spec(self.fixture_path)
        except (PrerequisiteMissing, ValueError, KeyError, TypeError) as exc:
            for role in ROLES:
                self.score.block("fixture." + role, str(exc))
        else:
            self.score.provenance["authoredSourceManifestDigest"] = digest(document)
            for role in ROLES:
                spec = {**document["fixtures"][role], "authenticated": role == "auth"}
                self.score.record("fixture." + role, lambda role=role, spec=spec: self.fixture(role, spec))
        for cell, action in (("journey.cancellation-recovery", self.cancellation),
                             ("fixture.injected-failure", self.injected_failure),
                             ("evidence.rollback-repoint", self.rollback),
                             ("evidence.rejects-data-loss", self.rejects_data_loss),
                             ("dependency.server-4420", self.independent_geometry)):
            self.score.record(cell, action)
        return self.states
