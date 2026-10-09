"""Shared deterministic/model action execution for the owned terminal journey.

No action plan is supplied to the model. The deterministic run has a fixed plan;
the model selects calls from freshly observed, initialize-bound descriptors.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

import oracles
import probes
import sdk
import stages
import discovery
from transport import ExecutionError, Transport

ID_FIELDS = ("operationId", "operationInstanceId", "proposalId", "jobId", "correlationId", "auditId")
# The identities the candidate actually emits for one canonical invocation
# (honua-server OperationExecutionModels.cs: OperationHandle/OperationPolicyContext).
# Stage evidence is keyed on these; nothing here is minted by the harness.
CANONICAL_KEYS = ("operationInstanceId", "correlationId", "auditId")
# Older receipt identities no candidate emits. They stay optional, nullable receipt
# fields and are retained only if a candidate ever returns them.
LEGACY_RECEIPT_KEYS = ("policyDecisionId", "actuatorId", "verificationId", "approvalId")
# Per-stage evidence keys (journey.v1.json `stageEvidenceKeys`). Stage 5 runs on the job
# runtime, which is not the operation gateway: it is keyed on its job identities.
CONTRACT = json.loads((Path(__file__).resolve().parent / "journey.v1.json").read_text())
STAGE_EVIDENCE_KEYS = {int(k): tuple(v) for k, v in CONTRACT["stageEvidenceKeys"].items()}
JOB_KEYS = ("jobId", "resourceUri", "jobStatus", "jobCreatedAt")
# Stage 3 file import: the pinned CLI's multipart admin upload of the committed fixture.
UPLOAD_COMMAND = "honua admin import uploadImportFile"
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
STAGE_TOOLS = {
    1: {"honua_list_capabilities"},
    2: {"honua_list_capabilities"},
    3: {"honua_ingest_dataset", "honua_publish_service", "honua_query_features", "honua_list_layers"},
    4: set(stages.STYLE_TOOLS),
    5: {"honua_plan_analysis", "honua_validate_plan", "honua_dry_run_plan", "honua_execute_plan", "honua_list_jobs"},
    6: set(stages.STUDIO_DRAFT_TOOLS + stages.STUDIO_COMPOSITION_TOOLS) | {
        "honua_studio_save_version", "honua_studio_get_version", "honua_studio_reopen_version", "honua_studio_get_draft"},
    7: {"honua_studio_propose_publication", "honua_operation_status", "honua_get_operation_status"},
    8: {"honua_operation_status", "honua_get_operation_status", "honua_supported_operation_kinds"},
}
# Which fixture principal acts in each build stage. The service operator (an admin
# key) publishes the service, styles it and runs GP. The Studio author is a
# deliberately non-admin principal: an admin caller publishes immediately and never
# produces the AwaitingApproval proposal stage 7 must observe. Targets that supply
# no operator credential fall back to the proposer for every stage.
STAGE_PRINCIPALS = {3: "operator", 4: "operator", 5: "operator", 6: "proposer", 7: "proposer", 8: "operator"}
SDK_METHODS = {"CreateConnectionAsync", "TestConnectionAsync"}


def identity(value, label):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ExecutionError(label, "candidate omitted a bounded canonical identity")
    return value


def structured(result, command):
    if result.get("isError"):
        raise ExecutionError(command, "candidate returned an MCP tool error")
    value = result.get("structuredContent")
    if not isinstance(value, dict):
        raise ExecutionError(command, "candidate omitted structured tool output")
    return value


class JourneyExecutor:
    def __init__(self, state, target, observation, transport, *, manifest=None):
        self.state, self.target, self.observation, self.transport = state, target, observation, transport
        self.manifest = manifest
        self.evidence = state.setdefault("execution", {"actions": {}, "resources": {}, "checks": {}, "canonicalIds": {}})
        self.fixture = target.get("execution") or {}

    def principal(self, number):
        wanted = STAGE_PRINCIPALS.get(number, "proposer")
        return wanted if self.transport.credentials.get(wanted) else "proposer"

    @property
    def resources(self):
        return self.evidence["resources"]

    def view(self):
        found = self.observation.setup_discovery or {}
        if not self.observation.setup_view_present:
            raise ExecutionError("initialize + tools/list", "initialize-bound discovery is not verified", blocked=True)
        if not self.transport.credentials.get("proposer"):
            raise ExecutionError("proposer profile", "proposer credential environment reference is unavailable", blocked=True)
        return {"tools": found["tools"], "metadata": found["metadata"]}

    def _record(self, number, command, result):
        self.evidence["actions"].setdefault(str(number), []).append(command)
        envelope = result.get("operation", result)
        ids = {k: envelope.get(k) for k in ID_FIELDS}
        for key, value in ids.items():
            if value is not None:
                identity(value, command)
        if ids.get("operationInstanceId"):
            self.evidence["canonicalIds"][str(number)] = ids
        # Retain explicit old receipt identities only when actually returned.
        for key in LEGACY_RECEIPT_KEYS:
            if envelope.get(key):
                self.evidence.setdefault("receiptIds", {}).setdefault(str(number), {})[key] = identity(envelope[key], command)
        for key in ("connectionId", "draftId", "itemId", "versionId", "contentHash", "jobId", "proposalId"):
            if result.get(key) is not None:
                self.resources[key] = identity(str(result[key]), command)
        if type(result.get("generation")) is int:
            self.resources["generation"] = result["generation"]
        if type(result.get("layerId")) is int and result["layerId"] >= 0:
            self.resources["layerId"] = result["layerId"]
        version = result.get("version")
        if isinstance(version, dict):
            self._record_version(version, command)
        if result.get("proposalId"):
            self.evidence["publicationOperation"] = ids

    def _record_version(self, version, command):
        for key in ("itemId", "versionId", "contentHash"):
            self.resources[key] = identity(version.get(key), command)

    def _check(self, number, check, command, fn):
        # The latest attempt owns the proof, including a failed or blocked attempt.
        self.evidence.setdefault("proofs", {}).pop(check, None)
        try:
            proof = fn()
            row = probes.Check(f"{number}.{check}", "artifact", command, "pass", "independent live assertion passed")
            self.evidence.setdefault("proofs", {})[check] = proof
        except ExecutionError as exc:
            row = probes.Check(f"{number}.{check}", "http", exc.command,
                               "blocked" if exc.blocked else "fail", exc.reason,
                               [stages.JOURNEY_DRIVER] if exc.blocked else [])
        except (oracles.ProofError, KeyError, ValueError, TypeError, AttributeError, StopIteration) as exc:
            row = probes.Check(f"{number}.{check}", "artifact", command, "fail",
                               str(exc) if isinstance(exc, oracles.ProofError) else "live assertion omitted required evidence")
        self.evidence["checks"].setdefault(str(number), {})[check] = row.as_receipt()
        return row

    def execute(self, number, action):
        view = self.view()
        kind = action.get("kind")
        if kind == "terminal_command":
            return self.execute_sdk_command(number, action, view)
        if kind != "tool_call":
            raise ExecutionError("model action", "only structured calls from the bounded server view may execute")
        name, arguments = action.get("tool"), action.get("arguments")
        descriptors = [t for t in view["tools"] if t["name"] == name]
        if name not in STAGE_TOOLS.get(number, set()) or len(descriptors) != 1 or not isinstance(arguments, dict):
            raise ExecutionError("model action", "tool is outside the current stage and bounded view")
        import jsonschema
        try:
            jsonschema.validate(arguments, descriptors[0]["inputSchema"])
        except jsonschema.ValidationError as exc:
            raise ExecutionError(name, "arguments do not satisfy the observed tool schema") from exc
        fault = self.state.get("armedError")
        injected = recovered = None
        send_arguments = arguments
        if fault and fault["stageNumber"] == number and fault["status"] == "armed" and name == "honua_render_map":
            # Exercise the candidate's real invalid-argument response on a read-only
            # render. No fictional network failure and no hidden mutation of a write.
            send_arguments = {**arguments, "width": -1}
            fault["status"] = "submitted"
        if name == "honua_publish_service":
            self.bind_publication(arguments)
        if name == "honua_render_map":
            self.evidence.setdefault("proofs", {}).pop("pixel", None)
            self.evidence["checks"].setdefault("4", {}).pop("pixel", None)
        result = self.transport.tool(name, send_arguments, view, self.principal(number))
        if fault and fault["status"] == "submitted":
            error = result.get("structuredContent") or {}
            if result.get("isError") is not True or error.get("code") not in {"invalid_argument", "invalid_input"}:
                raise ExecutionError(name, "injected invalid render was not refused by the candidate")
            fault["status"] = "observed"
            injected = {"id": fault["id"], "recoverable": True}
            return {"status": "fail", "accepted": False, "injectedError": injected,
                    "recoveredError": None, "reason": "candidate refused the injected invalid render width"}
        output = structured(result, name)
        if name == "honua_apply_style_preset":
            if output.get("applied") is not True or output.get("styleId") != arguments.get("styleId"):
                raise ExecutionError(name, "candidate did not apply the selected canonical style")
            self._check(4, "style-applied", name, lambda: {"styleId": identity(output["styleId"], name)})
        self._record(number, name, output)
        if name == "honua_publish_service":
            self.record_publication(output)
        if name == "honua_render_map":
            self.check_render(output)
            if (fault and fault["status"] == "observed"
                    and self.evidence["checks"].get("4", {}).get("pixel", {}).get("status") == "pass"):
                fault["status"] = "recovered"
                recovered = {"id": fault["id"], "recovered": True}
        if name == "honua_execute_plan":
            self.record_job(output)
            self.check_job()
        if name == "honua_studio_save_version":
            self.check_map()
        if name == "honua_studio_reopen_version":
            self.check_reopened()
        if name == "honua_studio_propose_publication":
            self.check_proposal()
        return {"status": "pass", "accepted": True, "injectedError": injected, "recoveredError": recovered,
                "canonicalIds": self.evidence["canonicalIds"].get(str(number), {}),
                "resources": dict(self.resources)}

    def execute_sdk_command(self, number, action, view):
        """Interpret one typed bridge command; never invoke a shell or model argv."""
        command = action.get("command", "")
        if number == 3 and command == UPLOAD_COMMAND:
            # Fixed, fixture-bound upload: the model chooses the step, never its arguments.
            if "honua_ingest_dataset" not in {tool["name"] for tool in view["tools"]}:
                raise ExecutionError(UPLOAD_COMMAND, "upload is outside the observed bounded ingest view")
            self.upload_dataset()
            return {"status": "pass", "accepted": True, "resources": dict(self.resources),
                    "injectedError": None, "recoveredError": None,
                    "canonicalIds": self.evidence["canonicalIds"].get("3", {})}
        pieces = command.split(" ", 2) if isinstance(command, str) else []
        if number != 3 or len(pieces) != 3 or pieces[0] != "honua-journey-sdk" or pieces[1] not in SDK_METHODS:
            raise ExecutionError("model terminal command", "command is outside the typed published SDK bridge")
        method = pieces[1]
        if "honua_ingest_dataset" not in {tool["name"] for tool in view["tools"]}:
            raise ExecutionError(method, "SDK operation is outside the observed bounded ingest/publication view")
        try:
            arguments = discovery.parse(pieces[2].encode("utf-8"))
        except (discovery.DiscoveryError, UnicodeError) as exc:
            raise ExecutionError(method, "SDK arguments must be a strict JSON array") from exc
        if not isinstance(arguments, list):
            raise ExecutionError(method, "SDK arguments must be an array")
        expected = {
            "CreateConnectionAsync": [self.fixture["datasource"]],
            "TestConnectionAsync": [self.resources.get("connectionId")],
        }[method]
        if arguments != expected or any(value is None for value in expected):
            raise ExecutionError(method, "SDK arguments do not bind the authored fixture and observed resource identities")
        if method == "CreateConnectionAsync":
            datasource = dict(arguments[0])
            password = os.environ.get(datasource.pop("passwordEnv"), "")
            if not password:
                raise ExecutionError(method, "datasource password environment reference is unavailable", blocked=True)
            arguments = [{**datasource, "password": password}]
        output = self.sdk_call(method, arguments)
        if method == "TestConnectionAsync":
            self._check(3, "datasource", method, lambda: self.prove_connection(output))
        return {"status": "pass", "accepted": True, "resources": dict(self.resources),
                "injectedError": None, "recoveredError": None,
                "canonicalIds": self.evidence["canonicalIds"].get("3", {})}

    def prove_connection(self, result):
        if result.get("connectionId") != self.resources.get("connectionId") or result.get("isHealthy") is not True:
            raise oracles.ProofError("datasource test did not prove the created connection healthy")
        return {"healthy": True}

    def fixture_dataset(self):
        """The committed upload fixture, pinned by digest and equal to the authored rows."""
        spec = self.fixture.get("upload")
        if not isinstance(spec, dict):
            raise ExecutionError(UPLOAD_COMMAND, "target declares no upload fixture", blocked=True)
        base = Path(stages.__file__).resolve().parent
        path = (base / spec["path"]).resolve()
        if not path.is_relative_to(base / "fixtures") or not path.is_file():
            raise ExecutionError(UPLOAD_COMMAND, "upload fixture is outside the committed journey fixtures")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
            raise ExecutionError(UPLOAD_COMMAND, "upload fixture digest differs from the target pin")
        document = json.loads(raw)
        if document.get("type") != "FeatureCollection" or document.get("features") != self.fixture["features"]:
            raise ExecutionError(UPLOAD_COMMAND, "upload fixture differs from the authored feature rows")
        return spec, path

    def upload_dataset(self):
        if self.resources.get("importTable"):
            raise ExecutionError(UPLOAD_COMMAND, "fixture dataset has already been uploaded; publish its observed table")
        spec, path = self.fixture_dataset()
        body = {"file": "@" + str(path), "TableName": spec["tableName"],
                "TargetSrid": str(spec["targetSrid"]), "OverwriteExisting": "true"}
        if spec.get("targetSchema"):
            body["TargetSchema"] = spec["targetSchema"]
        self.evidence["actions"].setdefault("3", []).append(UPLOAD_COMMAND)
        code, document = self.transport.cli_admin("import", "uploadImportFile", self.principal(3), body=body,
                                                  content_type="multipart/form-data", yes=True)
        if code or not isinstance(document, dict):
            raise ExecutionError(UPLOAD_COMMAND, "typed CLI multipart upload failed")
        document = document.get("data", document)
        if document.get("jobId") is not None:
            # Large or forced-background uploads return a durable import job; follow it.
            job_id = identity(document["jobId"], UPLOAD_COMMAND)

            def read():
                status_code, status = self.transport.cli_admin("import", "getImportJobStatus", self.principal(3),
                                                               path={"jobId": job_id})
                if status_code or not isinstance(status, dict):
                    raise ExecutionError("honua admin import getImportJobStatus", "typed CLI job read failed")
                return status.get("data", status)
            document = self.poll("honua admin import getImportJobStatus", read,
                                 lambda r: str(r.get("status", "")).lower() in {"completed", "failed", "cancelled"})
            document = document.get("result", document)
        self._check(3, "import-complete", UPLOAD_COMMAND, lambda: self.prove_upload(document))
        self.prove_upload(document)
        self.resources["importTable"] = identity(document["physicalTableName"], UPLOAD_COMMAND)
        self.resources["importSchema"] = identity(document.get("schema") or spec.get("targetSchema") or "public",
                                                  UPLOAD_COMMAND)
        return document

    def prove_upload(self, result):
        spec = self.fixture["upload"]
        expected = len(self.fixture["features"])
        if (result.get("success") is not True or type(result.get("featureCount")) is not int
                or result["featureCount"] != expected or result.get("tableName") != spec["tableName"]
                or result.get("errorMessage") or result.get("errorCode")
                or not isinstance(result.get("physicalTableName"), str) or not result["physicalTableName"]):
            raise oracles.ProofError("upload import did not report a clean import of every fixture feature")
        return {"featureCount": expected, "failedCount": 0, "datasetSha256": spec["sha256"],
                "physicalTableName": result["physicalTableName"]}

    def bind_publication(self, arguments):
        """service.publish must publish exactly the table this journey uploaded."""
        wanted = {"connectionId": self.resources.get("connectionId"), "table": self.resources.get("importTable"),
                  "schema": self.resources.get("importSchema")}
        if any(value is None for value in wanted.values()):
            raise ExecutionError("honua_publish_service", "uploaded dataset is unavailable", blocked=True)
        if any(arguments.get(key) != value for key, value in wanted.items()):
            raise ExecutionError("honua_publish_service", "publication does not bind the uploaded dataset and connection")

    def record_publication(self, output):
        if output.get("status") != "Completed" or output.get("requiresApproval"):
            raise ExecutionError("honua_publish_service", "service.publish did not complete for the operator")
        layer = output.get("layerId")
        if type(layer) is int and layer >= 0:
            self.resources["layerId"] = layer
        elif isinstance(layer, str) and layer.isdigit() and len(layer) <= 18:
            self.resources["layerId"] = int(layer)
        else:
            raise ExecutionError("honua_publish_service", "candidate omitted the published layer identity")
        self.check_features()

    def record_job(self, output):
        """Stage 5 job-runtime evidence, exactly as honua_execute_plan returned it."""
        job = {"jobId": identity(output.get("jobId"), "honua_execute_plan")}
        for key, source in (("resourceUri", "resourceUri"), ("jobStatus", "status"), ("jobCreatedAt", "createdAt")):
            value = output.get(source)
            if not isinstance(value, str) or not 0 < len(value) <= 2048 or any(c.isspace() for c in value):
                raise ExecutionError("honua_execute_plan", f"candidate omitted a bounded job {source}")
            job[key] = value
        self.evidence.setdefault("jobEvidence", {})["5"] = job
        return job

    def sdk_call(self, method, arguments):
        if method not in SDK_METHODS:
            raise ExecutionError(method, "SDK method is outside the journey surface")
        if not self.state.get("sdkBinding") and self.manifest is not None:
            self.state["sdkBinding"] = sdk.prepare(self.manifest, self.transport.workdir)
        result = sdk.invoke(self.state.get("sdkBinding"), method, arguments,
                            base_url=self.transport.base_url, credential=self.transport.credentials[self.principal(3)])
        if not isinstance(result, dict):
            raise ExecutionError(method, "published SDK omitted a typed result")
        identity_key = {"TestConnectionAsync": "connectionId", "GetGeoservicesImportJobStatusAsync": "jobId"}.get(method)
        if identity_key and result.get(identity_key) != arguments[0]:
            raise ExecutionError(method, "SDK lifecycle response differs from the submitted resource identity")
        self._record(3, method, result)
        return result

    def poll(self, command, read, done, *, timeout=120):
        deadline = time.monotonic() + timeout
        while True:
            value = read()
            if done(value):
                return value
            if time.monotonic() >= deadline:
                raise ExecutionError(command, "canonical lifecycle did not reach the required state within its budget")
            time.sleep(1)

    def check_features(self):
        return self._check(3, "imported-content", "GET imported layer features", lambda: oracles.prove_features(
            self.transport.get_json(self.fixture["featurePath"].format(**self.resources), principal=self.principal(3)),
            self.fixture["features"]))

    def check_render(self, output):
        def prove():
            image = output["image"]
            if image.get("base64"):
                raw = base64.b64decode(image["base64"], validate=True)
            else:
                raw, _ = self.transport.http("GET", image["uri"], principal=self.principal(4))
            proof = oracles.prove_pixel(raw, bbox=self.fixture["bbox"], point=self.fixture["pixelPoint"],
                size=self.fixture["renderSize"], rgba=self.fixture["pixelRgba"])
            return proof
        return self._check(4, "pixel", "honua_render_map + decode PNG", prove)

    def check_job(self):
        def prove():
            job_id = identity(self.resources.get("jobId"), "GET canonical job")
            path = "/ogc/processes/jobs/" + urllib.parse.quote(job_id, safe="")
            job = self.poll("GET canonical job", lambda: self.transport.get_json(path, principal=self.principal(5)),
                            lambda r: r.get("status") in {"successful", "failed", "dismissed"})
            if job["status"] != "successful" or job.get("jobID") != job_id:
                raise ExecutionError("GET canonical job", "job did not succeed with the submitted identity")
            results = self.transport.get_json(path + "/results", principal=self.principal(5))
            values = results.get("outputs", results)
            if not isinstance(values, dict) or not values:
                raise oracles.ProofError("canonical job returned no outputs")
            artifact = next(iter(values.values()))
            if "value" in artifact:
                feature = artifact["value"]
            else:
                href = artifact["href"]
                if href.startswith("data:application/geo+json;base64,"):
                    feature = json.loads(base64.b64decode(href.split(",", 1)[1], validate=True))
                else:
                    feature = self.transport.get_json(href, principal=self.principal(5))
            spec = self.fixture["buffer"]
            return oracles.prove_buffer(feature, x=spec["point"][0], y=spec["point"][1], distance=spec["distance"])
        return self._check(5, "buffer", "geometry.buffer canonical job lifecycle", prove)

    def family_schema_version(self, family):
        """Read the candidate's advertised schema version; never hard-code it."""
        document = self.transport.get_json("/api/v1/studio/package-families", principal=self.principal(6))
        rows = document.get("data", document) if isinstance(document, dict) else document
        if isinstance(rows, dict):
            rows = rows.get("families", rows.get("items"))
        matches = [row for row in rows or [] if isinstance(row, dict)
                   and str(row.get("family", "")).lower() == family]
        if len(matches) != 1 or not isinstance(matches[0].get("currentSchemaVersion"), str):
            raise ExecutionError("GET /api/v1/studio/package-families",
                                 f"candidate does not advertise one current {family} schema version")
        return matches[0]["currentSchemaVersion"]

    def map_path(self):
        return ("/api/v1/studio/content-items/" + identity(self.resources.get("itemId"), "read saved map")
                + "/versions/" + identity(self.resources.get("versionId"), "read saved map"))

    def expected_map_body(self):
        """Bind authored map sources to the independently verified imported layer.

        The candidate's saved body never supplies an expected value.
        """
        def bind(value):
            if isinstance(value, dict):
                return {key: bind(item) for key, item in value.items()}
            if isinstance(value, list):
                return [bind(item) for item in value]
            if isinstance(value, str) and "{layerId}" in value:
                layer_id = self.resources.get("layerId")
                if type(layer_id) is not int or layer_id < 0:
                    raise ExecutionError("bind map source", "imported layer identity is unavailable", blocked=True)
                return value.replace("{layerId}", str(layer_id))
            return value
        return bind(self.fixture["mapBody"])

    def _prove_map(self, transport, path, principal="proposer"):
        return oracles.prove_map(transport.get_json(path, principal=principal), self.expected_map_body(),
                                item_id=self.resources["itemId"], version_id=self.resources["versionId"],
                                content_hash=self.resources["contentHash"])

    def check_map(self):
        self._check(6, "saved-map", "GET saved immutable map version", lambda: self._prove_map(self.transport, self.map_path()))
        def replica():
            import local_fixture
            endpoint = local_fixture.replica_url(self.target)
            if not endpoint or endpoint.rstrip("/") == self.transport.base_url.rstrip("/"):
                raise ExecutionError("cross-replica map read", "a distinct replica endpoint is required", blocked=True)
            loopback = {"localhost", "127.0.0.1", "::1"}
            if (urllib.parse.urlsplit(self.transport.base_url).hostname not in loopback
                    and urllib.parse.urlsplit(endpoint).hostname in loopback):
                raise ExecutionError("cross-replica map read", "remote target must declare its remote replica endpoint", blocked=True)
            other = Transport(endpoint, self.transport.proxy, self.transport.honua,
                              self.transport.workdir, self.transport.credentials)
            return self._prove_map(other, self.map_path())
        self._check(6, "replica-map", "GET saved map on a distinct replica", replica)

    def check_reopened(self):
        def prove():
            draft_id = identity(self.resources.get("draftId"), "GET reopened draft")
            draft = self.transport.get_json("/api/v1/studio/package-drafts/" + draft_id, principal=self.principal(6))
            draft = draft.get("data", draft)
            expected = self.expected_map_body()
            if draft.get("baseVersionId") != self.resources["versionId"] or draft["envelope"]["body"] != expected:
                raise oracles.ProofError("reopened map body or source version differs")
            return {"contentDigest": oracles.content_digest(expected)}
        self._check(6, "reopened-map", "GET reopened map draft", prove)

    def check_proposal(self):
        def prove():
            proposal_id = identity(self.resources.get("proposalId"), "GET publication proposal")
            # The non-admin author cannot read the admin proposal route; an independent
            # operator read proves durability.
            proposal = self.transport.get_json("/api/v1/admin/proposals/" + proposal_id, principal=self.principal(8))
            if proposal.get("proposalId") != proposal_id or proposal.get("status") not in {"Pending", "AwaitingApproval"}:
                raise ExecutionError("GET publication proposal", "publication did not persist awaiting separate approval")
            return {"proposalId": proposal_id, "status": "AwaitingApproval"}
        return self._check(7, "durable-proposal", "GET publication proposal", prove)

    def approve(self, proposal_id):
        if proposal_id != self.resources.get("proposalId"):
            raise ExecutionError("approveOperationProposal", "approval does not bind the observed proposal")
        identity(proposal_id, "approveOperationProposal")
        keys = self.transport.credentials
        if not keys.get("approver") or keys.get("approver") == keys.get("proposer"):
            raise ExecutionError("approveOperationProposal", "distinct proposer and approver credentials are required", blocked=True)
        path = "/api/v1/admin/proposals/" + proposal_id
        reader = self.principal(8)
        before = self.transport.get_json(path, principal=reader)
        code, _ = self.transport.cli_approve(proposal_id, "proposer")
        denied = self.transport.get_json(path, principal=reader)
        if code == 0 or denied.get("status") != before.get("status") or denied.get("resolvedBy"):
            raise ExecutionError("approveOperationProposal --profile proposer", "self-approval was not refused without mutation")
        # A CLI usage error is not a security denial. Independently exercise the
        # authenticated candidate endpoint and require its actual 403 response.
        self.transport.http("POST", path + "/approve", principal="proposer", expected=(403,))
        code, _ = self.transport.cli_approve(proposal_id, "approver")
        if code:
            raise ExecutionError("approveOperationProposal --profile approver", "typed separate-principal approval failed")
        final = self.poll("GET approved proposal", lambda: self.transport.get_json(path, principal="approver"),
                          lambda r: r.get("status") in {"Succeeded", "Approved", "Applied", "Rejected", "Failed"})
        if (final.get("proposalId") != proposal_id or final.get("status") not in {"Succeeded", "Approved", "Applied"}
                or not final.get("resolvedBy") or final["resolvedBy"] == final.get("requestedBy")):
            raise ExecutionError("GET approved proposal", "candidate did not prove separate principal resolution")
        self.evidence["approvalResolution"] = {"proposalId": proposal_id,
                                              "requestedBy": identity(final["requestedBy"], "GET approved proposal"),
                                              "resolvedBy": identity(final["resolvedBy"], "GET approved proposal")}
        self._check(8, "separation", "typed CLI separate-principal approval", lambda: self.evidence["approvalResolution"])
        # The candidate emits no separate approval identity. The approval is keyed on
        # what it does report: the proposal, the resolving principal, and the audited
        # operation instance the approved replay executed. A server-reported approvalId
        # is retained if one ever appears; it is never invented.
        execution_id = identity(final.get("executionOperationId"), "GET approved proposal")
        handle = self.transport.get_json("/api/v1/operations/handles/" + execution_id, principal=self.principal(8))
        handle = handle.get("data", handle)
        if handle.get("operationInstanceId") != execution_id or handle.get("proposalId") not in {None, proposal_id}:
            raise ExecutionError("GET approved execution handle", "approved replay handle does not join the proposal")
        self.evidence["approval"] = {"proposalId": proposal_id,
                                     "resolvedBy": identity(final["resolvedBy"], "GET approved proposal"),
                                     "executionOperationId": execution_id,
                                     "auditId": identity(handle.get("auditId"), "GET approved execution handle"),
                                     "correlationId": identity(handle.get("correlationId"), "GET approved execution handle"),
                                     "approvalId": (identity(final["approvalId"], "GET approved proposal")
                                                    if final.get("approvalId") else None),
                                     "proposerSelfApproval": "denied"}
        self.evidence["canonicalIds"]["8"] = {"operationId": handle.get("operationId"),
                                              "operationInstanceId": execution_id,
                                              "proposalId": proposal_id,
                                              "correlationId": self.evidence["approval"]["correlationId"],
                                              "auditId": self.evidence["approval"]["auditId"]}
        return self.evidence["approval"]

    def verify_final(self):
        def prove():
            if not self.evidence.get("approvalResolution"):
                raise ExecutionError("GET final published map", "separate-principal approval has not completed", blocked=True)
            return self._prove_map(self.transport, self.fixture["publishedPath"], principal=None)
        return self._check(8, "final-map", "GET final published map content", prove)

    def verify_fake_success(self):
        def prove():
            # First require live content to pass, then show that the same identity
            # and hash cannot conceal a fabricated body from the independent oracle.
            document = self.transport.get_json(self.fixture["publishedPath"], principal=None)
            resource = document.get("data", document)
            kwargs = {"item_id": self.resources["itemId"], "version_id": self.resources["versionId"],
                      "content_hash": self.resources["contentHash"]}
            expected = self.expected_map_body()
            oracles.prove_map(document, expected, **kwargs)
            fake = {**resource, "envelope": {**resource["envelope"], "body": {"fakeSuccess": True}}}
            try:
                oracles.prove_map(fake, expected, **kwargs)
            except oracles.ProofError:
                return {"modifiedLiveBodyRejected": True}
            raise oracles.ProofError("fabricated success body passed the independent content oracle")
        return self._check(8, "fake-success", "reject fabricated body with live canonical identity/hash", prove)

    def verify_authority(self):
        def join():
            expected = self.evidence.get("publicationOperation", {})
            handle_id = identity(expected.get("operationInstanceId"), "GET canonical publication handle")
            observed = self.transport.get_json("/api/v1/operations/handles/" + handle_id, principal=self.principal(8))
            observed = observed.get("data", observed)
            if any(not expected.get(k) or observed.get(k) != expected[k]
                   for k in ("operationId", "operationInstanceId", "proposalId", "correlationId", "auditId")):
                raise ExecutionError("GET canonical publication handle", "canonical identity join differs")
            return {k: observed[k] for k in ("operationId", "operationInstanceId", "proposalId", "correlationId", "auditId")}

        def authority():
            handle_id = identity(self.evidence.get("publicationOperation", {}).get("operationInstanceId"), "GET current authority")
            observed = self.transport.get_json("/api/v1/operations/handles/" + handle_id, principal=self.principal(8))
            observed = observed.get("data", observed)
            if (observed.get("status") != "Completed" or observed.get("policyDecision") != "Allow"
                    or str(observed.get("authorizationOutcome", "")).lower() not in {"allowed", "authorized"}):
                raise ExecutionError("GET current authority", "completed replay does not report current allowed authority")
            return {"status": "Completed", "policyDecision": "Allow", "authorizationOutcome": observed["authorizationOutcome"]}

        def denied(path, principal, expected):
            if not self.transport.credentials.get(principal):
                raise ExecutionError("GET authority denial", f"{principal} credential reference is unavailable", blocked=True)
            if principal == "other-tenant":
                # Reach a non-admin Studio route before testing a private resource;
                # a blanket RBAC refusal cannot establish tenant isolation.
                self.transport.http("GET", "/api/v1/studio/package-families", principal=principal, expected=(200,))
            _, status = self.transport.http("GET", path, principal=principal, expected=expected)
            return {"httpStatus": status}

        return {"canonicalIdJoin": self._check(8, "canonical-join", "GET canonical publication handle", join),
                "currentAuthorityRevalidation": self._check(8, "current-authority", "GET current authority", authority),
                "tenantIsolation": self._check(8, "tenant-isolation", "GET private saved map under another tenant",
                    lambda: denied(self.map_path(), "other-tenant", (403, 404))),
                "rbacDenial": self._check(8, "rbac-denial", "GET admin API keys under viewer",
                    lambda: denied("/api/v1/admin/api-keys/", "viewer", (403,)))}

    def result(self, number):
        required = {3: {"datasource", "import-complete", "imported-content"}, 4: {"style-applied", "pixel"}, 5: {"buffer"},
                    6: {"saved-map", "replica-map", "reopened-map"}, 7: {"durable-proposal"},
                    8: {"separation", "final-map", "canonical-join", "current-authority",
                        "tenant-isolation", "rbac-denial"}}.get(number, set())
        stored = self.evidence["checks"].get(str(number), {})
        checks = [probes.Check(r["id"], r["kind"], r["invocation"], r["status"], r["detail"], r.get("blockedBy", []))
                  for r in stored.values()]
        for missing in sorted(required - stored.keys()):
            checks.append(probes.blocked(f"{number}.{missing}", "artifact", missing,
                                        "required live assertion has not executed", [stages.JOURNEY_DRIVER]))
        contract = json.loads((Path(stages.__file__).parent / "journey.v1.json").read_text())
        stage = next(s for s in contract["stages"] if s["number"] == number)
        ids = self.evidence.get("receiptIds", {}).get(str(number), {})
        canonical = self.evidence["canonicalIds"].get(str(number), {})
        if number >= 3 and required:
            missing = self.canonical_gaps(number)
            receipt = "canonical job receipt" if number == 5 else "canonical operation receipt"
            if missing:
                checks.append(probes.blocked(f"{number}.canonical-evidence", "http", receipt,
                    "candidate has not returned the canonical " + ", ".join(missing) + " for this stage",
                    [stages.JOURNEY_DRIVER]))
            else:
                checks.append(probes.Check(f"{number}.canonical-evidence", "http", receipt, "pass",
                    "candidate returned " + ", ".join(self.canonical_required(number)) + " for this stage"))
        result = stages._resolve(checks, number, stage["id"], stage["command"])
        result.operation_id = canonical.get("operationId")
        for key in CANONICAL_KEYS + ("proposalId",):
            setattr(result, stages.RECEIPT_ATTRS[key], canonical.get(key))
        if number == 5:
            job = self.evidence.get("jobEvidence", {}).get("5", {})
            for key in JOB_KEYS:
                setattr(result, stages.RECEIPT_ATTRS[key], job.get(key))
        result.policy_decision_id, result.actuator_id, result.verification_id = (
            ids.get("policyDecisionId"), ids.get("actuatorId"), ids.get("verificationId"))
        result.approval_id = (self.evidence.get("approval") or {}).get("approvalId") if number == 8 else None
        return result

    @staticmethod
    def canonical_required(number):
        """Server-emitted identities a passing stage must carry (journey.v1.json stageEvidenceKeys)."""
        return STAGE_EVIDENCE_KEYS[number]

    def canonical_gaps(self, number):
        if number == 5:
            canonical = self.evidence.get("jobEvidence", {}).get("5", {})
        else:
            canonical = self.evidence["canonicalIds"].get(str(number), {})
        missing = [key for key in self.canonical_required(number) if not canonical.get(key)]
        if number == 8:
            approval = self.evidence.get("approval") or {}
            missing += [f"approval {key}" for key in ("proposalId", "resolvedBy", "auditId") if not approval.get(key)]
        return missing

    def run_build(self):
        """Fixed harness calls, distinct from execute's model-selected calls."""
        def call(number, name, args):
            return self.execute(number, {"kind": "tool_call", "tool": name, "arguments": args})

        def service():
            self.view()
            datasource = dict(self.fixture["datasource"])
            reference = datasource.pop("passwordEnv")
            password = os.environ.get(reference)
            if not password:
                raise ExecutionError("CreateConnectionAsync", "datasource password environment reference is unavailable", blocked=True)
            created = self.sdk_call("CreateConnectionAsync", [{**datasource, "password": password}])
            connection_id = identity(str(created["connectionId"]), "CreateConnectionAsync")
            tested = self.sdk_call("TestConnectionAsync", [connection_id])
            self._check(3, "datasource", "TestConnectionAsync", lambda: self.prove_connection(tested))
            self.prove_connection(tested)
            # The GeoServices URL import cannot read a private-network fixture source
            # (HTTPS-only validator, no opt-in); import the committed fixture by upload.
            self.upload_dataset()
            call(3, "honua_publish_service", {**self.fixture["publishRequest"], "connectionId": connection_id,
                 "schema": self.resources["importSchema"], "table": self.resources["importTable"]})

        def style():
            if "layerId" not in self.resources:
                raise ExecutionError("honua_apply_style_preset", "imported published layer is unavailable", blocked=True)
            params = {"serviceId": self.fixture["serviceId"], "layerId": self.resources["layerId"]}
            self.transport.http("POST", "/ogc/styles", principal=self.principal(4), body=self.fixture["style"], expected=(201,),
                                extra_headers={"X-Style-Id": self.fixture["styleId"]})
            call(4, "honua_get_style", {"styleId": self.fixture["styleId"], "includeStylesheet": True})
            call(4, "honua_apply_style_preset", {**params, "styleId": self.fixture["styleId"]})
            width, height = self.fixture["renderSize"]
            call(4, "honua_render_map", {"layers": [params], "bbox": self.fixture["bbox"], "bboxSrid": 4326,
                 "width": width, "height": height, "transparent": True, "maxInlineBytes": 1024 * 1024})

        def buffer():
            spec = self.fixture["buffer"]
            # AnalysisPlanStepKind is Geoprocess and outputs are ArtifactKind names.
            # The candidate rejects kind "Process" and a step-id output with invalid_argument.
            plan = {"planId": self.state["workspaceId"] + "-buffer", "intentId": "journey-buffer",
                    "steps": [{"stepId": "buffer", "kind": "Geoprocess", "processId": "geometry.buffer", "inputs": {
                        "wkb": oracles.point_wkb(*spec["point"]), "srid": str(spec["srid"]),
                        "distance": str(spec["distance"]), "geodesic": "false"}}], "outputs": ["FeatureLayer"]}
            call(5, "honua_validate_plan", {"plan": plan})
            call(5, "honua_dry_run_plan", {"plan": plan})
            call(5, "honua_execute_plan", {"plan": plan, "idempotencyKey": self.state["workspaceId"] + "-buffer"})

        def composition():
            if "layerId" not in self.resources:
                raise ExecutionError("honua_studio_create_draft", "imported published layer is unavailable", blocked=True)
            call(6, "honua_studio_create_draft", {"family": "map", "packageKey": self.state["workspaceId"],
                 "schemaVersion": self.family_schema_version("map"), "body": self.expected_map_body()})
            call(6, "honua_studio_validate_draft", {"draftId": self.resources["draftId"]})
            call(6, "honua_studio_save_version", {"draftId": self.resources["draftId"], "generation": self.resources["generation"]})
            call(6, "honua_studio_reopen_version", {"itemId": self.resources["itemId"], "versionId": self.resources["versionId"]})

        def publication():
            if "versionId" not in self.resources:
                raise ExecutionError("honua_studio_propose_publication", "saved immutable map version is unavailable", blocked=True)
            call(7, "honua_studio_propose_publication", {k: self.resources[k] for k in ("itemId", "versionId", "contentHash")} |
                 {"route": self.fixture["route"], "visibility": "public", "note": "Independent local journey verification"})

        def approval():
            if "proposalId" not in self.resources:
                raise ExecutionError("approveOperationProposal", "durable publication proposal is unavailable", blocked=True)
            try:
                self.approve(self.resources["proposalId"])
            finally:
                if self.evidence.get("approvalResolution"):
                    self.verify_final()
                    self.verify_authority()

        for number, invoke in ((3, service), (4, style), (5, buffer), (6, composition), (7, publication), (8, approval)):
            try:
                if not self.fixture:
                    raise ExecutionError("journey fixture", "target has no independently authored execution fixture", blocked=True)
                invoke()
            except ExecutionError as exc:
                row = probes.Check(f"{number}.execution", "cli", exc.command,
                    "blocked" if exc.blocked else "fail", exc.reason, [stages.JOURNEY_DRIVER] if exc.blocked else [])
                self.evidence["checks"].setdefault(str(number), {})["execution"] = row.as_receipt()
            except (KeyError, ValueError, TypeError, AttributeError, StopIteration):
                self.evidence["checks"].setdefault(str(number), {})["execution"] = probes.Check(
                    f"{number}.execution", "artifact", "validate target execution fixture", "fail",
                    "execution fixture or candidate response omits required fields").as_receipt()
        return [self.result(n) for n in range(3, 9)]
