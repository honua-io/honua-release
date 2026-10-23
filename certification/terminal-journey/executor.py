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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, BrokenBarrierError

import oracles
import probes
import sdk
import stages
import discovery
from transport import (ActionRejected, ExecutionError, Transport, credential_values, describe_fields, describe_tool_error,
                       masked_fields, tool_error)

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
# no operator credential fall back to the proposer for every stage. Stages 1-2 (install/handoff and
# credential preflight) verify the operator's own install: honua_list_capabilities needs Catalog Discover,
# which the proposer's write:journey key does not carry (permission_denied, trunk run 39850688596).
STAGE_PRINCIPALS = {1: "operator", 2: "operator", 3: "operator", 4: "operator", 5: "operator",
                    6: "proposer", 7: "proposer", 8: "operator"}
SDK_METHODS = {"CreateConnectionAsync", "TestConnectionAsync"}
# A model may select the authored datasource without round-tripping credential
# environment references, which the canary correctly redacts before prompting.
DATASOURCE_REFERENCE = {"fixtureReference": "journey.datasource"}
# Cross-replica read through a cloud cell's single endpoint (honua-server#5790): how many times the
# saved map is re-read, the pause between sequential reads, and the bound on an instance id the server stamps
# (printable ASCII, at most 128 characters, as ServerInstanceIdentity sanitizes it).
REPLICA_READS = 8
REPLICA_READ_DELAY_SECONDS = 0.5
INSTANCE_ID = re.compile(r"[\x21-\x7e]{1,128}\Z")
# Redis-off cells (owner ruling redis-governed-control-plane-2026-10-08, "decision 3"): Redis is a
# requirement of the governed control plane, and a stage on a Redis-off cell passes by recording the
# candidate's typed unavailable refusal with the manifest reasonCode instead of executing. Per stage:
# the capability whose manifest row must report the dependency unavailable. Decision 3 names stages
# 5-8; the owner extended it to stage 3 on 2026-10-10: on a Redis-off Production host
# honua_publish_service (routed through the governed operation runtime) is refused with the same
# durable-store refusal (run 38066103745, aws-serverless/redis-off), and that refusal is the stage's
# pass. On a Production Redis-off cell stages 6-8 never reach a tool call (they need the layer stage
# 3 would have published) and stay blocked on it; they are listed so a host that does publish records
# their typed refusal the same strict way. Stage 8 approves through the CLI, whose output is never
# retained, so it cannot match and keeps its outcome.
REDIS_OFF_RULING = "redis-governed-control-plane-2026-10-08"
TOPOLOGY_REFUSAL_STAGES = {
    # The ruling names the publish refusal's typed fields, so stage 3 requires them exactly.
    3: {"capability": "operations.proposals", "problem": {"kind": "ExecutionFailed", "retryable": "false"}},
    5: {"capability": "jobs.runner"},
    6: {"capability": "operations.proposals"},
    7: {"capability": "operations.proposals"},
    8: {"capability": "operations.proposals"},
}
CLOUD_CELL = re.compile(r"(?:aws-ecs|aws-serverless|aws-eks)/(redis-on|redis-off)\Z")


def declared_redis_topology(target):
    """The Redis topology a target declares: a cloud cell's id, or a local target's redisTopology."""
    target = target or {}
    if target.get("kind") in ("aws-ecs", "aws-serverless", "aws-eks"):
        match = CLOUD_CELL.match(str(target.get("id") or ""))
        return match.group(1) if match else None
    declared = target.get("redisTopology")
    return declared if declared in ("redis-on", "redis-off") else None


def _token(value):
    return value if isinstance(value, str) and TOKEN.fullmatch(value) else None


def identity(value, label):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ExecutionError(label, "candidate omitted a bounded canonical identity")
    return value


def structured(result, command, secrets=()):
    if result.get("isError"):
        problem = tool_error(result, secrets)
        raise ExecutionError(command, describe_tool_error(problem), problem=problem)
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
            # A proof may name what it observed (e.g. the instances a cross-replica read saw).
            detail = proof.pop("detail", None) if isinstance(proof, dict) else None
            row = probes.Check(f"{number}.{check}", "artifact", command, "pass",
                               detail if isinstance(detail, str) and detail else "independent live assertion passed")
            self.evidence.setdefault("proofs", {})[check] = proof
        except ExecutionError as exc:
            row = probes.Check(f"{number}.{check}", "http", exc.command,
                               "blocked" if exc.blocked else "fail", exc.reason,
                               (exc.blocked_by or [stages.JOURNEY_DRIVER]) if exc.blocked else [])
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
            raise ActionRejected("model action", "only structured calls from the bounded server view may execute")
        name, arguments = action.get("tool"), action.get("arguments")
        descriptors = [t for t in view["tools"] if t["name"] == name]
        if name not in STAGE_TOOLS.get(number, set()) or len(descriptors) != 1 or not isinstance(arguments, dict):
            raise ActionRejected("model action", "tool is outside the current stage and bounded view")
        import jsonschema
        try:
            jsonschema.validate(arguments, descriptors[0]["inputSchema"])
        except jsonschema.ValidationError as exc:
            raise ActionRejected(name, "arguments do not satisfy the observed tool schema") from exc
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
            self.evidence.pop("renderDimensions", None)
        if name == "honua_studio_save_version":
            self.prepare_map_save(arguments)
        result = self.transport.tool(name, send_arguments, view, self.principal(number))
        if fault and fault["status"] == "submitted":
            error = result.get("structuredContent") or {}
            if result.get("isError") is not True or error.get("code") not in {"invalid_argument", "invalid_input"}:
                raise ExecutionError(name, "injected invalid render was not refused by the candidate")
            fault["status"] = "observed"
            injected = {"id": fault["id"], "recoverable": True}
            return {"status": "fail", "accepted": False, "injectedError": injected,
                    "recoveredError": None, "reason": "candidate refused the injected invalid render width"}
        output = structured(result, name, credential_values(self.transport.credentials))
        if name == "honua_apply_style_preset":
            if output.get("applied") is not True or output.get("styleId") != arguments.get("styleId"):
                raise ExecutionError(name, "candidate did not apply the selected canonical style")
            self._check(4, "style-applied", name, lambda: {"styleId": identity(output["styleId"], name)})
        if name == "honua_publish_service":
            self.record_publication(output)
        if number == 6 and name in {"honua_studio_create_draft", "honua_studio_update_draft",
                                     *stages.STUDIO_COMPOSITION_TOOLS}:
            self.record_map_draft(name, arguments, output)
        self._record(number, name, output)
        if name == "honua_render_map":
            self.check_render(output, selected_arguments=arguments)
            if (fault and fault["status"] == "observed"
                    and self.evidence["checks"].get("4", {}).get("pixel", {}).get("status") == "pass"):
                fault["status"] = "recovered"
                recovered = {"id": fault["id"], "recovered": True}
        if name == "honua_execute_plan":
            self.record_job(output)
            self.check_job()
        if name == "honua_studio_save_version":
            self.evidence["mapSaveInput"]["savedVersionId"] = self.resources["versionId"]
            self.check_map()
        if name == "honua_studio_reopen_version":
            self.check_reopened()
        if name == "honua_studio_propose_publication":
            self.check_proposal()
        return {"status": "pass", "accepted": True, "injectedError": injected, "recoveredError": recovered,
                "canonicalIds": self.evidence["canonicalIds"].get(str(number), {}),
                "resources": dict(self.resources),
                **({"mapReadbacks": list(self.evidence.get("mapReadbacks", []))}
                   if name == "honua_studio_save_version" else {}),
                **({"nativePublication": dict(self.evidence["nativePublication"])}
                   if name == "honua_publish_service" else {})}

    def terminal_surface(self, number):
        """Describe the harness bridge grammar without choosing or executing an action."""
        if number == 6:
            try:
                authored_body = self.expected_map_body()
            except ExecutionError as exc:
                if not exc.blocked:
                    raise
                return {"compositionContract": {"available": False,
                    "blockedBy": "independently observed published layer identity"}}
            return {"compositionContract": {
                "family": "map", "bodyFormat": "honua_map_package.v1",
                "schemaVersion": self.family_schema_version("map"),
                "currentDraft": {key: value for key, value in (self.evidence.get("mapDraftReceipt") or {}).items()
                                 if key in {"draftId", "generation", "packageKey"}},
                "requirement": "Save the authored portable map package body exactly. Native catalog composition "
                               "tools produce a different representation; a valid composition draft alone does not "
                               "satisfy this case. Create or update the draft body with this portable package, "
                               "using the candidate's advertised map schema version. The model selects every write.",
                "authoredBody": authored_body,
                "bodySource": "committed execution.mapBody with the independently published layerId bound",}}
        if number != 3 or "honua_ingest_dataset" not in {tool["name"] for tool in self.view()["tools"]}:
            return {}
        return {
            "sdkCommand": "honua-journey-sdk",
            "sdkMethods": sorted(SDK_METHODS),
            "sdkSyntax": "honua-journey-sdk METHOD JSON_ARRAY",
            "sdkFixtureReferences": {"datasource": dict(DATASOURCE_REFERENCE)},
            "sdkCommandExamples": {
                "CreateConnectionAsync": "honua-journey-sdk CreateConnectionAsync " + json.dumps([DATASOURCE_REFERENCE]),
                **({"TestConnectionAsync": "honua-journey-sdk TestConnectionAsync " + json.dumps([self.resources["connectionId"]])}
                   if self.resources.get("connectionId") else {}),
            },
            "sdkArgumentSources": {
                "CreateConnectionAsync": ["terminalSurface.sdkFixtureReferences.datasource (unchanged)"],
                "TestConnectionAsync": ["currentState.connectionId"],
            },
            "uploadCommand": UPLOAD_COMMAND,
            "uploadArguments": "None. Use this exact command; the bridge supplies the committed fixture file and options.",
            "uploadResult": "currentState.importTable and currentState.importSchema identify the uploaded dataset.",
            "publicationBindings": {"connectionId": "currentState.connectionId",
                                    "table": "currentState.importTable", "schema": "currentState.importSchema"},
            "scope": "This stage requires the committed file upload, not a URL ingestion. "
                     "These terminal commands use kind=terminal_command; they are not MCP tool names. "
                     "No shell, extra flags, or other SDK methods are accepted.",
        }

    def execute_sdk_command(self, number, action, view):
        """Interpret one typed bridge command; never invoke a shell or model argv."""
        command = action.get("command", "")
        if number == 3 and command == UPLOAD_COMMAND:
            # Fixed, fixture-bound upload: the model chooses the step, never its arguments.
            if "honua_ingest_dataset" not in {tool["name"] for tool in view["tools"]}:
                raise ActionRejected(UPLOAD_COMMAND, "upload is outside the observed bounded ingest view")
            self.upload_dataset()
            return {"status": "pass", "accepted": True, "resources": dict(self.resources),
                    "injectedError": None, "recoveredError": None,
                    "canonicalIds": self.evidence["canonicalIds"].get("3", {})}
        pieces = command.split(" ", 2) if isinstance(command, str) else []
        if number != 3 or len(pieces) != 3 or pieces[0] != "honua-journey-sdk" or pieces[1] not in SDK_METHODS:
            raise ActionRejected("model terminal command", "command is outside the typed published SDK bridge")
        method = pieces[1]
        if "honua_ingest_dataset" not in {tool["name"] for tool in view["tools"]}:
            raise ActionRejected(method, "SDK operation is outside the observed bounded ingest/publication view")
        try:
            arguments = discovery.parse(pieces[2].encode("utf-8"))
        except (discovery.DiscoveryError, UnicodeError) as exc:
            raise ActionRejected(method, "SDK arguments must be a strict JSON array") from exc
        if not isinstance(arguments, list):
            raise ActionRejected(method, "SDK arguments must be an array")
        expected = {
            "CreateConnectionAsync": [self.fixture["datasource"]],
            "TestConnectionAsync": [self.resources.get("connectionId")],
        }[method]
        fixture_reference = method == "CreateConnectionAsync" and arguments == [DATASOURCE_REFERENCE]
        if (arguments != expected and not fixture_reference) or any(value is None for value in expected):
            hint = ("use the exact CreateConnectionAsync command in terminalSurface.sdkCommandExamples; "
                    "the bridge resolves the authored datasource privately"
                    if method == "CreateConnectionAsync" else
                    "create the connection first, then use currentState.connectionId in the TestConnectionAsync command")
            raise ActionRejected(method, "SDK arguments do not bind the authored fixture and observed resource identities; " + hint)
        if method == "CreateConnectionAsync":
            arguments = [self.datasource_arguments()]
        output = self.sdk_call(method, arguments)
        if method == "TestConnectionAsync":
            self._check(3, "datasource", method, lambda: self.prove_connection(output))
        return {"status": "pass", "accepted": True, "resources": dict(self.resources),
                "injectedError": None, "recoveredError": None,
                "canonicalIds": self.evidence["canonicalIds"].get("3", {})}

    def datasource_arguments(self):
        """The authored datasource with its environment references resolved, in memory only.

        `passwordEnv` is required. A cloud target also names its connection coordinates
        (`hostEnv`, `portEnv`, `databaseNameEnv`, `usernameEnv`) by reference, so the target
        document the cloud harness retains carries no database host or login.
        """
        datasource = dict(self.fixture["datasource"])
        resolved = {}
        for reference in [key for key in datasource if key.endswith("Env")]:
            field = reference[:-len("Env")]
            value = os.environ.get(datasource.pop(reference) or "", "")
            if not value:
                raise ExecutionError("CreateConnectionAsync",
                                     f"datasource {field} environment reference is unavailable", blocked=True)
            resolved[field] = int(value) if field == "port" else value
        if "password" not in resolved:
            raise ExecutionError("CreateConnectionAsync", "datasource password environment reference is unavailable", blocked=True)
        return {**datasource, **resolved}

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
            raise ActionRejected(UPLOAD_COMMAND, "fixture dataset has already been uploaded; publish its observed table")
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
        proved = self._check(3, "import-complete", UPLOAD_COMMAND, lambda: self.prove_upload(document))
        if proved.status != "pass":
            # The failed proof is already recorded; stop the stage as a handled failure.
            raise ExecutionError(UPLOAD_COMMAND, proved.detail, blocked=proved.status == "blocked")
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
        missing = [key for key, value in wanted.items() if value is None]
        if missing:
            raise ActionRejected("honua_publish_service", "publication prerequisites are missing: "
                                 + ", ".join(missing) + "; consult terminalSurface and currentState")
        if any(arguments.get(key) != value for key, value in wanted.items()):
            raise ActionRejected("honua_publish_service", "publication does not bind the uploaded dataset and connection")

    def record_publication(self, output):
        if output.get("status") != "Completed" or output.get("requiresApproval") is not False:
            # service.publish is synchronous on the candidate (the submit handle is terminal), so name
            # the terminal state it returned instead (masked; never the raw tool output).
            seen = masked_fields({name: output.get(name) for name in (
                "status", "requiresApproval", "policyOutcome", "authorizationOutcome", "message")},
                credential_values(self.transport.credentials))
            raise ExecutionError("honua_publish_service", "service.publish did not complete for the operator"
                                 + (f" ({describe_fields(seen)})" if seen else ""))
        # McpPublishServiceOutput projects these from the completed canonical
        # handle. Requested serviceName and the fixture are not publication proof.
        service = output.get("serviceName")
        if (not isinstance(service, str) or not 0 < len(service) <= 200
                or not service.isprintable() or service.strip() != service):
            raise ExecutionError("honua_publish_service", "candidate omitted a bounded published service name")
        try:
            service_bytes = service.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ExecutionError("honua_publish_service", "candidate returned an invalid published service name") from exc
        if any(secret and secret in service for secret in credential_values(self.transport.credentials)):
            raise ExecutionError("honua_publish_service", "candidate returned credential material as a service identity")
        uri = output.get("serviceUri")
        if uri != "honua://published-services/" + urllib.parse.quote_from_bytes(service_bytes, safe=""):
            raise ExecutionError("honua_publish_service", "candidate omitted or contradicted the published service URI")
        layer = output.get("layerId")
        raw_layer = str(layer) if isinstance(layer, str) or type(layer) is int else None
        if raw_layer is not None and any(secret and (secret == raw_layer or
                secret.isdecimal() and raw_layer.isdecimal() and secret.lstrip("0") == raw_layer.lstrip("0"))
                for secret in credential_values(self.transport.credentials)):
            raise ExecutionError("honua_publish_service", "candidate returned credential material as a layer identity")
        if type(layer) is int and layer >= 0:
            layer_id = layer
        elif isinstance(layer, str) and re.fullmatch(r"[0-9]{1,18}", layer):
            layer_id = int(layer)
        else:
            raise ExecutionError("honua_publish_service", "candidate omitted the published layer identity")
        # Native map tools accept the published service name as serviceId.
        # Keep the returned name too, so this normalization stays explicit.
        self.resources.update(serviceName=service, serviceId=service, serviceUri=uri, layerId=layer_id)
        self.evidence["nativePublication"] = {
            "serviceNameSha256": hashlib.sha256(service_bytes).hexdigest(),
            "serviceUriSha256": hashlib.sha256(uri.encode()).hexdigest(),
            "layerId": layer_id,
            "fixtureServiceNameMatches": service == self.fixture.get("serviceId"),
        }
        self.check_features()

    def record_job(self, output):
        """Stage 5 job-runtime evidence, exactly as honua_execute_plan returned it."""
        job = {"jobId": identity(output.get("jobId"), "honua_execute_plan")}
        for key, source in (("resourceUri", "resourceUri"), ("jobStatus", "status"), ("jobCreatedAt", "createdAt")):
            value = output.get(source)
            if not isinstance(value, str) or not 0 < len(value) <= 2048 or any(c.isspace() for c in value):
                raise ExecutionError("honua_execute_plan", f"candidate omitted a bounded job {source}")
            job[key] = value
        # The resource URI must identify the same job (honua://jobs/<jobId>).
        location = urllib.parse.urlsplit(job["resourceUri"])
        if (location.query or location.fragment
                or (location.netloc + location.path).rstrip("/").split("/")[-1] != job["jobId"]):
            raise ExecutionError("honua_execute_plan", "job resourceUri does not identify the returned jobId")
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

    def check_render(self, output, *, selected_arguments=None):
        self.evidence.pop("renderDimensions", None)

        def media_type_of(value):
            if not isinstance(value, str):
                return ""
            return value.split(";", 1)[0].strip().lower()

        def prove():
            image = output["image"]
            # honua-server's McpRenderedImage names the media type `format` (RenderMapTool.cs);
            # `mimeType` is the MCP content-block spelling. Both, when present, must agree.
            spellings = {media_type_of(image.get(key)) for key in ("format", "mimeType") if key in image}
            if len(spellings) > 1:
                raise oracles.ProofError("render image declares conflicting media types")
            declared = spellings.pop() if spellings else ""
            if image.get("base64"):
                raw = base64.b64decode(image["base64"], validate=True)
                media_type = declared
            else:
                raw, _ = self.transport.http("GET", image["uri"], principal=self.principal(4))
                fetched = media_type_of(self.transport.last_response_headers.get("content-type", ""))
                # A declared image/png must match the bytes the client actually fetched.
                if not fetched or (declared and declared != fetched):
                    raise oracles.ProofError("render response media type conflicts with the declared image type")
                media_type = fetched
            # Record only independently decoded dimensions, never image-declared
            # width/height. This distinguishes a model-selected wrong size from
            # a renderer returning different bytes than the model requested.
            if media_type == "image/png":
                try:
                    width, height, _ = oracles.decode_png(raw)
                except oracles.PngError as exc:
                    raise oracles.ProofError(f"PNG decoder refused: {exc.code}") from exc
                selected = [(selected_arguments or {}).get(key) for key in ("width", "height")]
                self.evidence["renderDimensions"] = {
                    "expected": list(self.fixture["renderSize"]),
                    "requested": selected if all(type(v) is int and 0 < v <= 16384 for v in selected) else None,
                    "decoded": [width, height], "sha256": hashlib.sha256(raw).hexdigest(),
                    "requestedPixelVerified": False}
                if (self.evidence["renderDimensions"]["requested"] == [width, height]
                        and selected != self.fixture["renderSize"]):
                    # Matching dimensions alone cannot exclude a renderer defect
                    # that occurs only at the model's selected size. Independently
                    # sample the authored point/colour in that actual frame too.
                    oracles.prove_pixel(raw, bbox=self.fixture["bbox"], point=self.fixture["pixelPoint"],
                        size=selected, rgba=self.fixture["pixelRgba"], media_type=media_type)
                    self.evidence["renderDimensions"]["requestedPixelVerified"] = True
            proof = oracles.prove_pixel(raw, bbox=self.fixture["bbox"], point=self.fixture["pixelPoint"],
                size=self.fixture["renderSize"], rgba=self.fixture["pixelRgba"], media_type=media_type)
            return proof
        row = self._check(4, "pixel", "honua_render_map + decode PNG", prove)
        return row

    def repairable_render_selection(self, number, check):
        """A correct render of a wrong selected size is unfinished model work.

        The failed pixel assertion remains unchanged. Missing selection, invalid
        PNG, wrong pixels, transport failures and a renderer ignoring the request
        are never admitted through this narrow feedback path.
        """
        dimensions = self.evidence.get("renderDimensions") or {}
        requested = dimensions.get("requested")
        return (number == 4 and check.id == "4.pixel" and check.status == "fail"
                and check.detail == "PNG dimensions differ from the authored render size"
                and isinstance(requested, list) and len(requested) == 2
                and all(type(v) is int and 0 < v <= 16384 for v in requested)
                and requested == dimensions.get("decoded")
                and dimensions.get("requestedPixelVerified") is True
                and dimensions.get("expected") == self.fixture.get("renderSize")
                and requested != dimensions["expected"])

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
        version = matches[0]["currentSchemaVersion"]
        if (not 1 <= len(version) <= 50 or any(ord(c) < 32 for c in version) or
                any(secret and secret in version for secret in credential_values(self.transport.credentials))):
            raise ExecutionError("GET /api/v1/studio/package-families",
                                 "candidate map schema version is not a bounded non-secret identity")
        return version

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

    def record_map_draft(self, command, arguments, output):
        """Retain the actual last write receipt for a later independent HTTP read."""
        try:
            envelope = output["envelope"]
            snapshot = {key: output[key] for key in
                        ("draftId", "itemId", "generation", "ownerId", "packageKey", "family")}
            # Unpaired drafts omit the nullable workspaceId in the published API.
            snapshot["workspaceId"] = output.get("workspaceId")
            if (type(snapshot["generation"]) is not int or snapshot["generation"] < 0
                    or not isinstance(envelope, dict) or str(envelope.get("family", "")).lower() != "map"):
                raise ValueError()
            previous = self.evidence.get("mapDraftReceipt")
            if command != "honua_studio_create_draft" and isinstance(previous, dict):
                if (arguments.get("draftId") != previous["draftId"] or
                        any(snapshot[key] != previous[key] for key in
                            ("draftId", "itemId", "ownerId", "workspaceId", "packageKey", "family"))):
                    raise ExecutionError(command, "native draft write changed its original identity binding")
            snapshot["bodyDigest"] = oracles.content_digest(envelope["body"])
            if command in {"honua_studio_create_draft", "honua_studio_update_draft"} and "body" in arguments:
                if envelope["body"] != arguments["body"]:
                    raise ExecutionError(command, "native draft write body differs from the selected body")
            package_key = snapshot["packageKey"]
            if (not isinstance(package_key, str) or not 1 <= len(package_key) <= 200 or
                    any(ord(c) < 32 for c in package_key) or
                    any(secret and secret in package_key for secret in credential_values(self.transport.credentials))):
                raise ExecutionError(command, "native draft omitted a bounded non-secret package identity")
            self.evidence["mapDraftReceipt"] = snapshot
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError(command, "native draft write omitted its content or canonical identity") from exc

    def _map_readback(self, transport, path, slot, *, snapshot=None, principal="proposer"):
        document = transport.get_json(path, principal=principal)
        value = document.get("data", document) if isinstance(document, dict) else {}
        value = value if isinstance(value, dict) else {}
        envelope = value.get("envelope")
        envelope = envelope if isinstance(envelope, dict) else {}
        body = envelope.get("body")
        # Only fixed field names, booleans and digests leave private state. Never
        # retain candidate keys/values or use candidate content as an expectation.
        fields = {"itemId": value.get("itemId") == self.resources.get("itemId"),
                  "family": str(envelope.get("family", "")).lower() == "map",
                  "body": "body" in envelope and body == self.expected_map_body()}
        if snapshot is not None:
            fields.update({key: value.get(key) == snapshot.get(key) for key in
                ("draftId", "generation", "ownerId", "workspaceId", "packageKey")})
            fields["draftReceipt"] = all(fields[key] for key in
                ("draftId", "itemId", "generation", "ownerId", "workspaceId", "packageKey")) and value.get("family") == snapshot.get("family")
            fields["bodyMatchesDraft"] = "body" in envelope and oracles.content_digest(body) == snapshot["bodyDigest"]
        elif slot != "pre-save":
            fields["versionId"] = value.get("versionId") == self.resources.get("versionId")
            fields["contentHash"] = value.get("contentHash") == self.resources.get("contentHash")
            saved_input = self.evidence.get("mapSaveInput")
            if isinstance(saved_input, dict):
                fields.update({key: value.get(key) == saved_input[key] for key in ("ownerId", "packageKey")})
                fields["sourceDraftId"] = value.get("sourceDraftId") == saved_input["draftId"]
                fields["draftReceipt"] = all(fields[key] for key in ("sourceDraftId", "ownerId", "packageKey"))
                fields["bodyMatchesDraft"] = ("body" in envelope and
                    oracles.content_digest(body) == saved_input["bodyDigest"] and
                    saved_input.get("savedVersionId") == self.resources.get("versionId"))
        diagnostic = {"slot": slot, "fieldMatches": fields,
                      "bodyDigest": oracles.content_digest(body),
                      "authoredBodyDigest": oracles.content_digest(self.expected_map_body())}
        original = getattr(transport, "last_json_observation", None)
        if isinstance(original, dict) and original.get("path") == path:
            diagnostic.update({key: original[key] for key in ("responseSha256", "responseBytes")})
        self.evidence.setdefault("mapReadbacks", []).append(diagnostic)
        return document, fields

    def prepare_map_save(self, arguments):
        self.evidence["mapReadbacks"] = []
        snapshot = self.evidence.get("mapDraftReceipt")
        if not isinstance(snapshot, dict):
            raise ExecutionError("save map", "original native draft write receipt is unavailable")
        if (arguments.get("draftId") != snapshot["draftId"] or
                type(arguments.get("generation")) is not int or
                arguments.get("generation") != snapshot["generation"] or
                self.resources.get("draftId") != snapshot["draftId"] or
                self.resources.get("itemId") != snapshot["itemId"]):
            raise ActionRejected("save map", "save must bind the latest observed draft and generation")
        path = "/api/v1/studio/package-drafts/" + identity(snapshot["draftId"], "read draft before save")
        _, fields = self._map_readback(self.transport, path, "pre-save", snapshot=snapshot,
                                       principal=self.principal(6))
        if not all(fields[key] for key in ("itemId", "family", "draftReceipt", "bodyMatchesDraft")):
            raise ExecutionError("read draft before save", "native draft readback differs from its original write receipt")
        if not fields["body"]:
            raise ActionRejected("save map", "draft body differs from the authored portable map package; "
                                 "consult terminalSurface.compositionContract and select a draft body update")
        self.evidence["mapSaveInput"] = dict(snapshot)

    def _prove_map(self, transport, path, principal="proposer", *, slot=None):
        slot = slot or ("saved-primary" if transport is self.transport else "saved-replica")
        document, fields = self._map_readback(transport, path, slot, principal=principal)
        proof = oracles.prove_map(document, self.expected_map_body(),
                                 item_id=self.resources["itemId"], version_id=self.resources["versionId"],
                                 content_hash=self.resources["contentHash"])
        if fields.get("draftReceipt") is False:
            raise oracles.ProofError("saved map identity differs from its independently read pre-save draft")
        if fields.get("bodyMatchesDraft") is False:
            raise oracles.ProofError("saved map body differs from its independently read pre-save draft")
        return proof

    def check_map(self):
        self._check(6, "saved-map", "GET saved immutable map version", lambda: self._prove_map(self.transport, self.map_path()))
        import local_fixture
        topology = local_fixture.replica_topology(self.target)
        command = ("GET saved map on a distinct replica" if topology is None
                   else f"GET saved map on a distinct replica behind the cell endpoint ({topology['id']}: {topology['description']})")

        def replica():
            endpoint = local_fixture.replica_url(self.target)
            same = bool(endpoint) and endpoint.rstrip("/") == self.transport.base_url.rstrip("/")
            if same and topology is not None:
                return self.reread_through_cell_endpoint(command)
            if not endpoint or same:
                raise ExecutionError("cross-replica map read", "a distinct replica endpoint is required", blocked=True)
            loopback = {"localhost", "127.0.0.1", "::1"}
            if (urllib.parse.urlsplit(self.transport.base_url).hostname not in loopback
                    and urllib.parse.urlsplit(endpoint).hostname in loopback):
                raise ExecutionError("cross-replica map read", "remote target must declare its remote replica endpoint", blocked=True)
            other = Transport(endpoint, self.transport.proxy, self.transport.honua,
                              self.transport.workdir, self.transport.credentials)
            return self._prove_map(other, self.map_path())
        self._check(6, "replica-map", command, replica)

    def _instance(self, transport):
        """The answering instance's `Honua-Instance` id, or None when the response carried none."""
        import local_fixture
        read = getattr(transport, "response_header", None)
        value = read(local_fixture.INSTANCE_HEADER) if callable(read) else None
        if not isinstance(value, str):
            return None
        if not INSTANCE_ID.match(value):
            raise oracles.ProofError(f"{local_fixture.INSTANCE_HEADER} header is not a bounded instance id")
        return value

    def _server_sha(self):
        try:
            sha = self.manifest["components"]["honua-server"]["sha"]
        except (TypeError, KeyError):
            return None
        return sha if isinstance(sha, str) else None

    def _instance_sources(self, ids):
        """Best effort: each observed instance's identity source from GET /api/v1/admin/version,
        whose `instance.id` equals the header of the instance that answered it."""
        principal = next((name for name in ("operator", "approver") if self.transport.credentials.get(name)), None)
        sources = {}
        if principal is None:
            return sources
        for _ in range(REPLICA_READS):
            try:
                document = self.transport.get_json("/api/v1/admin/version", principal=principal)
            except ExecutionError:
                return sources
            document = document.get("data", document) if isinstance(document, dict) else {}
            instance = document.get("instance") if isinstance(document, dict) else None
            if isinstance(instance, dict) and instance.get("id") in ids and isinstance(instance.get("source"), str):
                sources[instance["id"]] = instance["source"][:64]
            if set(sources) >= set(ids):
                break
        return sources

    def reread_through_cell_endpoint(self, command):
        """Cross-replica read-after-write through a cloud cell's single endpoint.

        Re-reads the saved map up to REPLICA_READS times, each read proven against the authored
        body, and collects the `Honua-Instance` id that answered (honua-server#5790). Two or more
        distinct instances agreeing with the write pass. One instance only is blocked on the
        topology. No header at all is blocked on the server need, naming the pinned candidate.
        """
        import local_fixture
        observed = []
        if self.target.get("kind") == "aws-serverless" and self._instance(self.transport) is not None:
            # Sequential requests can all reuse one warm Lambda environment. Give the real
            # endpoint bounded concurrent work; only observed distinct responders prove a pass.
            observed = self._concurrent_replica_reads(command)
        else:
            for number in range(REPLICA_READS):
                if number:
                    time.sleep(REPLICA_READ_DELAY_SECONDS)
                self._prove_map(self.transport, self.map_path(), slot="saved-replica")
                observed.append(self._instance(self.transport))
                if len({value for value in observed if value}) >= 2:
                    break
        ids = list(dict.fromkeys(value for value in observed if value))
        if not ids:
            raise ExecutionError(command, local_fixture.replica_unprovable_reason(self._server_sha()),
                                 blocked=True, blocked_by=[local_fixture.REPLICA_SERVER_NEED])
        missing = sum(1 for value in observed if not value)
        if missing:
            raise oracles.ProofError(f"{missing} of {len(observed)} re-reads carried no "
                                     f"{local_fixture.INSTANCE_HEADER} header while others did")
        if len(ids) < 2:
            raise ExecutionError(command, f"topology served one instance: {ids[0]} ({len(observed)} "
                                 "re-reads through the cell endpoint, each agreeing with the write)",
                                 blocked=True, blocked_by=[local_fixture.REPLICA_SINGLE_INSTANCE])
        sources = self._instance_sources(set(ids))
        instances = [{"id": value, "source": sources.get(value, "unobserved")} for value in ids]
        described = ", ".join(f"{item['id']} ({item['source']})" for item in instances)
        return {"detail": f"{len(observed)} re-reads through the cell endpoint agreed with the write "
                          f"and were answered by {len(ids)} instances: {described}",
                "instances": instances, "reads": len(observed)}

    def _concurrent_replica_reads(self, command):
        # Each client owns its response headers and opener. Sharing one transport could join
        # one request's map body to another response's instance id and fabricate replica proof.
        clients = [Transport(self.transport.base_url, self.transport.proxy, self.transport.honua,
                             self.transport.workdir, self.transport.credentials)
                   for _ in range(REPLICA_READS)]
        start = Barrier(REPLICA_READS, timeout=10)

        def read(client):
            try:
                start.wait()
            except BrokenBarrierError as error:
                raise ExecutionError(command, "bounded concurrent replica reads could not start") from error
            self._prove_map(client, self.map_path())
            return self._instance(client)

        try:
            with ThreadPoolExecutor(max_workers=REPLICA_READS) as pool:
                futures = [pool.submit(read, client) for client in clients]
                # Validate every response, including responses after a second instance appears.
                return [future.result() for future in futures]
        finally:
            # The pool has drained even on failure. Preserve fresh bearer values for the
            # parent transport's existing receipt/error redaction without sharing mutable clients.
            issued = list(getattr(self.transport, "_issued_credentials", []))
            for client in clients:
                issued.extend(getattr(client, "_issued_credentials", []))
            self.transport._issued_credentials = issued

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
        #
        # OperationGateway records ExecutionOperationId only from a replay's job id; a
        # Studio publication completes synchronously, so the candidate instead completes
        # the publication's own canonical handle (the one honua_studio_propose_publication
        # returned together with this proposalId). Use the proposal's execution id when
        # present, otherwise that handle, and join it on the proposal's sealed content.
        publication = self.evidence.get("publicationOperation") or {}
        if final.get("executionOperationId"):
            execution_id, source = identity(final["executionOperationId"], "GET approved proposal"), "proposal"
        elif publication.get("proposalId") == proposal_id:
            execution_id = identity(publication.get("operationInstanceId"), "GET approved proposal")
            source = "publication-handle"
        else:
            raise ExecutionError("GET approved proposal", "candidate reported no execution for the approved proposal")
        handle = self.transport.get_json("/api/v1/operations/handles/" + execution_id, principal=self.principal(8))
        handle = handle.get("data", handle)
        sealed = {"itemId": self.resources.get("itemId"), "versionId": self.resources.get("versionId")}
        diff = set(final.get("diff") or [])
        if (handle.get("operationInstanceId") != execution_id or handle.get("proposalId") not in {None, proposal_id}
                or handle.get("status") != "Completed"
                or any((handle.get("resourceIds") or {}).get(k) != v for k, v in sealed.items())
                or not {f"{k}={v}" for k, v in sealed.items()} <= diff
                or f"contentHash={self.resources.get('contentHash')}" not in diff):
            raise ExecutionError("GET approved execution handle", "approved replay handle does not join the proposal")
        operation_id = identity(handle.get("operationId"), "GET approved execution handle")
        self.evidence["approval"] = {"proposalId": proposal_id,
                                     "resolvedBy": identity(final["resolvedBy"], "GET approved proposal"),
                                     "executionOperationId": execution_id,
                                     "executionSource": source,
                                     "auditId": identity(handle.get("auditId"), "GET approved execution handle"),
                                     "correlationId": identity(handle.get("correlationId"), "GET approved execution handle"),
                                     "approvalId": (identity(final["approvalId"], "GET approved proposal")
                                                    if final.get("approvalId") else None),
                                     "proposerSelfApproval": "denied"}
        self.evidence["canonicalIds"]["8"] = {"operationId": operation_id,
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
            keys = ("operationId", "operationInstanceId", "correlationId", "auditId")
            # The handle may omit proposalId; the propose envelope returned it together
            # with this operationInstanceId, so a handle that names a different proposal
            # fails, and an omitted one is joined through that envelope.
            if (any(not expected.get(k) or observed.get(k) != expected[k] for k in keys)
                    or not expected.get("proposalId") or observed.get("proposalId") not in {None, expected["proposalId"]}):
                raise ExecutionError("GET canonical publication handle", "canonical identity join differs")
            return {**{k: observed[k] for k in keys}, "proposalId": expected["proposalId"],
                    "proposalJoin": "handle" if observed.get("proposalId") else "propose-envelope"}

        def authority():
            handle_id = identity(self.evidence.get("publicationOperation", {}).get("operationInstanceId"), "GET current authority")
            observed = self.transport.get_json("/api/v1/operations/handles/" + handle_id, principal=self.principal(8))
            observed = observed.get("data", observed)
            if (observed.get("status") != "Completed" or observed.get("policyDecision") != "Allow"
                    or str(observed.get("authorizationOutcome", "")).lower() not in {"allowed", "authorized", "approved"}):
                raise ExecutionError("GET current authority", "completed replay does not report current allowed authority")
            return {"status": "Completed", "policyDecision": "Allow", "authorizationOutcome": observed["authorizationOutcome"]}

        def denied(path, principal, expected):
            if not self.transport.credentials.get(principal):
                unavailable = (self.target.get("unavailablePrincipals") or {}).get(principal)
                if isinstance(unavailable, dict) and unavailable.get("reason") and unavailable.get("blockedBy"):
                    # The target topology documents why this principal cannot exist there. That one
                    # assertion is blocked on its named dependency; the rest of the stage still runs.
                    raise ExecutionError("GET authority denial", unavailable["reason"], blocked=True,
                                         blocked_by=list(unavailable["blockedBy"]))
                raise ExecutionError("GET authority denial", f"{principal} credential reference is unavailable", blocked=True)
            if principal == "other-tenant":
                if self.target.get("tenantFixture"):
                    # Prove this exact private resource is readable by the same subject/role
                    # in tenant A immediately before the tenant B comparison.
                    self.transport.http("GET", path, principal="proposer", expected=(200,))
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
        refusal = self.evidence.get("topologyRefusals", {}).get(str(number))
        if refusal is not None:
            # A Redis-off stage passes on the typed refusal plus every assertion that ran before it
            # (stage 3: datasource and import); nothing that needs the refused operation is required.
            required = {3: {"datasource", "import-complete", "execution"}}.get(number, {"execution"})
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
        if number >= 3 and required and refusal is None:
            missing = self.canonical_gaps(number)
            receipt = "canonical job receipt" if number == 5 else "canonical operation receipt"
            if missing:
                cause = [stages.JOURNEY_DRIVER]
                why = "candidate has not returned the canonical " + ", ".join(missing) + " for this stage"
                checks.append(probes.blocked(f"{number}.canonical-evidence", "http", receipt, why, cause))
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
        approval = self.evidence.get("approval") or {}
        result.approval_id = approval.get("approvalId") if number == 8 else None
        if number == 8:
            result.resolved_by = approval.get("resolvedBy")
        result.topology_refusal = refusal
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

    def declared_redis_topology(self):
        return declared_redis_topology(self.target)

    def record_topology_refusal(self, number, exc):
        """Record a Redis-off stage by the candidate's typed refusal; False leaves the failure as it is."""
        spec = TOPOLOGY_REFUSAL_STAGES.get(number)
        problem = exc.problem
        if (spec is None or exc.blocked or self.declared_redis_topology() != "redis-off"
                or problem.get("missingDependency") != "redis" or problem.get("code") != "unavailable"
                or problem.get("capability", spec["capability"]) != spec["capability"]
                or any(problem.get(key) != value for key, value in spec.get("problem", {}).items())):
            return False
        try:
            manifest = self.transport.get_json("/api/v1/capabilities/manifest", principal=self.principal(number))
        except ExecutionError:
            return False
        rows = [row for row in (manifest.get("capabilities") if isinstance(manifest, dict) else None) or []
                if isinstance(row, dict) and row.get("id") == spec["capability"]]
        if (len(rows) != 1 or rows[0].get("available") is not False
                or rows[0].get("reasonCode") != "dependency-unavailable"):
            # The cell declares Redis off but the candidate does not report the dependency missing:
            # the refusal is not the topology's, so the stage keeps its failure.
            exc.reason += (f"; the cell declares redis-off but the capability manifest does not report "
                           f"{spec['capability']} dependency-unavailable")
            return False
        server = manifest.get("server") if isinstance(manifest.get("server"), dict) else {}
        refusal = {"topology": "redis-off", "ruling": REDIS_OFF_RULING, "rulingStatus": "ruled",
                   "tool": exc.command, "code": problem["code"], "missingDependency": "redis",
                   "manifestCapability": spec["capability"], "manifestReasonCode": "dependency-unavailable",
                   "deploymentEnvironment": _token(server.get("deploymentEnvironment")),
                   **{key: problem[key] for key in ("kind", "capability", "message") if key in problem}}
        self.evidence.setdefault("topologyRefusals", {})[str(number)] = refusal
        evidence = (f"topology: redis-off; {exc.command} refused with the typed durable-store refusal "
                    f"({describe_fields(problem)}); manifest {spec['capability']} reasonCode=dependency-unavailable"
                    f" (owner ruling {REDIS_OFF_RULING})")
        check = probes.Check(f"{number}.execution", "mcp-tool", exc.command, "pass", evidence)
        self.evidence["checks"].setdefault(str(number), {})["execution"] = check.as_receipt()
        return True

    def run_build(self):
        """Fixed harness calls, distinct from execute's model-selected calls."""
        def call(number, name, args):
            return self.execute(number, {"kind": "tool_call", "tool": name, "arguments": args})

        def service():
            self.view()
            created = self.sdk_call("CreateConnectionAsync", [self.datasource_arguments()])
            connection_id = identity(str(created["connectionId"]), "CreateConnectionAsync")
            tested = self.sdk_call("TestConnectionAsync", [connection_id])
            proved = self._check(3, "datasource", "TestConnectionAsync", lambda: self.prove_connection(tested))
            if proved.status != "pass":
                raise ExecutionError("TestConnectionAsync", proved.detail, blocked=proved.status == "blocked")
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
            discovery.mark_step(f"stage {number} execution ({invoke.__name__})")
            try:
                if not self.fixture:
                    raise ExecutionError("journey fixture", "target has no independently authored execution fixture", blocked=True)
                invoke()
            except ExecutionError as exc:
                if self.record_topology_refusal(number, exc):
                    continue
                row = probes.Check(f"{number}.execution", "cli", exc.command,
                    "blocked" if exc.blocked else "fail", exc.reason, (exc.blocked_by or [stages.JOURNEY_DRIVER]) if exc.blocked else [])
                self.evidence["checks"].setdefault(str(number), {})["execution"] = row.as_receipt()
            except (KeyError, ValueError, TypeError, AttributeError, StopIteration):
                self.evidence["checks"].setdefault(str(number), {})["execution"] = probes.Check(
                    f"{number}.execution", "artifact", "validate target execution fixture", "fail",
                    "execution fixture or candidate response omits required fields").as_receipt()
        return [self.result(n) for n in range(3, 9)]
