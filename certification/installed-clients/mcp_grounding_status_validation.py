"""Independent semantics for actual-client grounding and declared release reads.

This module judges retained observations. It never calls a candidate or grants
client, image, or authorization credit by itself.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_map_package_validation as wire
from mcp_render_validation import negative_output
from mcp_render_validation import scenario as render_scenario
from mcp_map_package_validation import strict_json
from mcp_ops_events_config import declaration as declare_configuration


PROJECTION = ("releaseVersion", "releaseDeclared", "isCoVersioned", "serving", "execution", "skewedIds")
OPERATIONS = ("tools/call:ground_candidates", "tools/call:clarify_intent",
              "tools/call:platform_release_status")
CATALOG_URI = "honua://catalog/processes"


def calls(authored, *, intent_id=None):
    """Exact authored call order; clarification carries the preceding real ID."""
    if intent_id is not None and (not isinstance(intent_id, str) or not intent_id.strip()):
        raise ValueError("clarification requires the actual preceding intent ID")
    goal, ambiguous = authored["authoredGoal"], authored["clarificationGoal"]
    entries = [
        ("catalog", "resources/read", {"uri": CATALOG_URI}, True, "catalog"),
        ("ground", "tools/call", {"name": "honua_ground_candidates", "arguments": {"goal": goal}}, True, "ground"),
        ("ambiguous", "tools/call", {"name": "honua_ground_candidates", "arguments": {"goal": ambiguous}}, True, "ambiguous"),
        ("clarify", "tools/call", {"name": "honua_clarify_intent", "arguments": {
            "intentId": intent_id, "goal": ambiguous, "response": {"answers": {"workflow_family": ["PublishData"]}}}}, True, "clarify"),
        ("status", "tools/call", {"name": "honua_platform_release_status", "arguments": {}}, True, "status"),
        ("missing-goal", "tools/call", {"name": "honua_ground_candidates", "arguments": {"goal": ""}}, True, "invalid_argument"),
        ("missing-intent", "tools/call", {"name": "honua_clarify_intent", "arguments": {
            "intentId": "", "goal": ambiguous, "response": {"answers": {"workflow_family": ["PublishData"]}}}}, True, "invalid_argument"),
        ("missing-answer", "tools/call", {"name": "honua_clarify_intent", "arguments": {
            "intentId": intent_id, "goal": ambiguous, "response": {"answers": {"workflow_family": [""]}}}}, True, "invalid_argument"),
        ("status-argument", "tools/call", {"name": "honua_platform_release_status", "arguments": {"action": "apply"}}, True, "invalid_argument"),
        ("limited-status", "tools/call", {"name": "honua_platform_release_status", "arguments": {}}, True, "permission_denied"),
    ]
    entries += [("anonymous-" + name, "tools/call", {"name": "honua_" + name, "arguments": args}, False, "anonymous")
                for name, args in (("ground_candidates", {"goal": goal}),
                                   ("clarify_intent", {"intentId": intent_id, "goal": ambiguous,
                                                       "response": {"answers": {"workflow_family": ["PublishData"]}}}),
                                   ("platform_release_status", {}))]
    return [{"id": identifier, "method": method, "params": params, "authenticated": authenticated,
             "expectation": expectation} for identifier, method, params, authenticated, expectation in entries]


def scenario(fixture_bytes, bootstrap):
    """Bind authored goals to the existing owned publication and exact image."""
    binding = render_scenario(fixture_bytes, bootstrap)
    area = bootstrap["ownedArea"]
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha",
                                          "candidateImage", "runtimeBinding")} | {
        "schema": "honua.mcp-grounding-status-scenario/v1", "operations": list(OPERATIONS),
        "ownedArea": area, "processCatalogUri": "honua://catalog/processes",
        "authoredGoal": f"Buffer geometry from {area['serviceName']}",
        "clarificationGoal": area["serviceName"],
        "requestSetSha256": "sha256:" + hashlib.sha256(json.dumps({
            "area": area, "process": "geometry.buffer", "operations": OPERATIONS,
            "goals": [f"Buffer geometry from {area['serviceName']}", area["serviceName"]]},
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()}


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is missing")
    return value


def _ranking(rows, kind, allowed, *, require_nonempty=True):
    if not isinstance(rows, list) or (require_nonempty and not rows):
        raise ValueError(f"{kind} candidates are missing")
    seen, previous, previous_id = set(), math.inf, None
    for candidate in rows:
        candidate = _mapping(candidate, kind)
        identifier = _nonempty(candidate.get("id"), f"{kind} id")
        score = candidate.get("score")
        if (identifier in seen or candidate.get("kind") != kind
                or identifier not in allowed or candidate.get("displayName") != allowed[identifier]
                or type(score) not in (int, float) or not math.isfinite(score)
                or score <= 0 or score > previous
                or (score == previous and previous_id is not None and identifier < previous_id)
                or not isinstance(candidate.get("evidence"), list)
                or not candidate["evidence"]
                or not all(isinstance(item, str) and item for item in candidate["evidence"])):
            raise ValueError(f"{kind} candidates have duplicate, unordered, or unsupported evidence")
        seen.add(identifier); previous, previous_id = score, identifier
    return seen


def _provenance(draft, ranking):
    provenance = _mapping(draft.get("provenance"), "draft provenance")
    sources = provenance.get("sources")
    if (not isinstance(sources, list)
            or not all(isinstance(row, dict) and _nonempty(row.get("sourceId"), "source ID")
                       for row in sources)
            or [row["sourceId"] for row in sources] != [row["id"] for row in ranking["datasets"]]
            or provenance.get("processDefinitions") != [row["id"] for row in ranking["processes"]]):
        raise ValueError("draft provenance differs from actual ranked source and process IDs")
    return provenance


def source_context(owned_area, metadata, catalog):
    """Bind independently observed graph resource and process catalog to setup."""
    area, metadata, catalog = (_mapping(owned_area, "owned area"),
                               _mapping(metadata, "metadata readback"),
                               _mapping(catalog, "process catalog"))
    for name in ("serviceId", "serviceName", "connectionId", "locator"):
        if _nonempty(metadata.get(name), name) != _nonempty(area.get(name), name):
            raise ValueError("metadata source differs from the owned publication")
    for name in ("layerId", "storageLayerId"):
        if type(metadata.get(name)) is not int or metadata[name] != area.get(name):
            raise ValueError("metadata layer differs from the owned publication")
    if (metadata.get("resourceType") != "feature-dataset"
            or not _nonempty(metadata.get("resourceId"), "resource ID")
            or not _nonempty(metadata.get("resourceName"), "resource name")
            or type(metadata.get("revision")) is not int or metadata["revision"] < 1):
        raise ValueError("owned publication has no current readable Metadata V2 dataset")
    processes = catalog.get("processes")
    if (catalog.get("status") != "ok" or not isinstance(catalog.get("catalogVersion"), str)
            or not catalog["catalogVersion"] or not isinstance(processes, list)
            or not all(isinstance(item, dict) for item in processes)
            or len([item for item in processes if item.get("processId") == "geometry.buffer"]) != 1):
        raise ValueError("actual process catalog lacks unique geometry.buffer")
    process = next(item for item in processes if item.get("processId") == "geometry.buffer")
    if (process.get("executionKind") != "Job"
            or not isinstance(process.get("entryPoints"), list)
            or "job" not in process["entryPoints"]
            or not isinstance(process.get("supportedExecutionModes"), list)
            or "Async" not in process["supportedExecutionModes"]):
        raise ValueError("geometry.buffer is not job-callable in the actual catalog")
    # The bounded owned graph read observes one physical layer and its service.
    # The process resource observes the entire callable catalog. A shortlist
    # entry outside either observation cannot be credited as grounded.
    dataset_names = {str(area["storageLayerId"]): metadata["resourceName"],
                     area["serviceName"]: area["serviceName"]}
    if (len(dataset_names) != 2 or not all(isinstance(name, str) and name.strip()
                                           for name in dataset_names.values())):
        raise ValueError("owned graph has ambiguous or unnamed dataset aliases")
    process_names = {}
    for item in processes:
        identifier = item.get("processId")
        if (not isinstance(identifier, str) or not identifier
                or identifier in process_names):
            raise ValueError("process catalog has an ambiguous identity")
        if (item.get("executionKind") == "Job" and "job" in (item.get("entryPoints") or [])
                and "Async" in (item.get("supportedExecutionModes") or [])):
            process_names[identifier] = _nonempty(item.get("displayName"), "process display name")
    if process_names.get("geometry.buffer") != _nonempty(process.get("displayName"), "buffer display name"):
        raise ValueError("owned buffer process is not callable in the actual catalog")
    return {"datasetIds": set(dataset_names), "datasetNames": dataset_names,
            "processId": "geometry.buffer", "processNames": process_names,
            "revision": metadata["revision"]}


def process_catalog_resource(result, uri="honua://catalog/processes"):
    """Decode only an actual JSON MCP resource read of the canonical catalog."""
    result = _mapping(result, "process catalog resource result")
    contents = result.get("contents")
    if "error" in result or not isinstance(contents, list) or len(contents) != 1:
        raise ValueError("process catalog was not read as one resource")
    item = _mapping(contents[0], "process catalog content")
    if (item.get("uri") != uri or item.get("mimeType") != "application/json"
            or not isinstance(item.get("text"), str) or "blob" in item):
        raise ValueError("process catalog read has wrong identity or media")
    return _mapping(strict_json(item["text"]), "process catalog JSON")


def grounding_output(output, context, goal):
    """A real shortlist must contain the owned dataset and callable process."""
    output = _mapping(output, "grounding output")
    if output.get("engine") != "deterministic":
        raise ValueError("grounding output changed its declared engine")
    draft = _mapping(output.get("draftIntent"), "draft intent")
    ranking = _mapping(output.get("candidates"), "candidate ranking")
    if draft.get("goal") != goal:
        raise ValueError("grounding draft changed the authored goal")
    intent = _nonempty(draft.get("intentId"), "intent ID")
    dataset_ids = _ranking(ranking.get("datasets"), "Dataset", context["datasetNames"])
    process_ids = _ranking(ranking.get("processes"), "Process", context["processNames"])
    if (not context["datasetIds"].intersection(dataset_ids)
            or context["processId"] not in process_ids):
        raise ValueError("grounding omitted the owned graph or callable process")
    _provenance(draft, ranking)
    family = _mapping(output.get("workflowFamily"), "workflow family")
    if (family.get("value") != "Analyze" or type(family.get("confidence")) not in (int, float)
            or not 0 < family["confidence"] <= 1 or draft.get("workflowFamily") != "Analyze"):
        raise ValueError("buffer goal did not classify as a bounded analysis")
    return intent


def clarification_output(before, after, goal, context):
    """The answer must apply to the initial intent and change classified state."""
    before, after = _mapping(before, "initial grounding"), _mapping(after, "clarified grounding")
    first = _mapping(before.get("draftIntent"), "initial intent")
    second = _mapping(after.get("draftIntent"), "clarified intent")
    for result in (before, after):
        candidates = _mapping(result.get("candidates"), "clarification candidates")
        datasets = _ranking(candidates.get("datasets"), "Dataset", context["datasetNames"])
        _ranking(candidates.get("processes"), "Process", context["processNames"], require_nonempty=False)
        if not context["datasetIds"].intersection(datasets):
            raise ValueError("clarification lost the owned Metadata V2 dataset")
        _provenance(_mapping(result.get("draftIntent"), "clarification intent"), candidates)
    intent = _nonempty(first.get("intentId"), "initial intent ID")
    envelope = before.get("clarification")
    elicitation = before.get("elicitation")
    if isinstance(envelope, dict) and elicitation is None:
        questions = envelope.get("questions")
        asked = (envelope.get("intentId") == intent and isinstance(questions, list)
                 and any(isinstance(question, dict) and question.get("questionId") == "workflow_family"
                         and any(isinstance(option, dict) and option.get("id") == "PublishData"
                                 for option in (question.get("options") or []))
                         for question in questions))
    elif envelope is None and isinstance(elicitation, dict):
        schema = _mapping(elicitation.get("requestedSchema"), "native clarification schema")
        properties = _mapping(schema.get("properties"), "native clarification properties")
        question = _mapping(properties.get("workflow_family"), "native workflow-family question")
        asked = (isinstance(elicitation.get("message"), str)
                 and f"'{intent}'" in elicitation["message"]
                 and isinstance(schema.get("required"), list)
                 and "workflow_family" in schema["required"]
                 and question.get("type") == "string"
                 and isinstance(question.get("enum"), list)
                 and "PublishData" in question["enum"])
    else:
        asked = False
    if first.get("goal") != goal or not asked:
        raise ValueError("initial grounding did not ask for the material workflow family")
    family = _mapping(after.get("workflowFamily"), "answered workflow family")
    provenance = _mapping(second.get("provenance"), "answered provenance")
    answered = provenance.get("clarificationsAnswered")
    if (after.get("engine") != "deterministic" or second.get("intentId") != intent
            or second.get("goal") != goal or second.get("workflowFamily") != "PublishData"
            or family.get("value") != "PublishData" or family.get("confidence") != 1
            or family.get("evidence") != ["clarification"]
            or not isinstance(answered, list) or "workflow_family" not in answered):
        raise ValueError("clarification did not carry and resolve the original intent")
    remaining = after.get("clarification")
    native_remaining = after.get("elicitation")
    if remaining is not None and native_remaining is not None:
        raise ValueError("answered clarification returned conflicting envelopes")
    if remaining is not None and (not isinstance(remaining, dict)
            or remaining.get("intentId") != intent
            or not isinstance(remaining.get("questions"), list)
            or any(isinstance(question, dict) and question.get("questionId") == "workflow_family"
                   for question in remaining["questions"])):
        raise ValueError("answered workflow-family question was replayed")
    if native_remaining is not None:
        native = _mapping(native_remaining, "remaining native clarification")
        schema = _mapping(native.get("requestedSchema"), "remaining native schema")
        properties = _mapping(schema.get("properties"), "remaining native properties")
        required = schema.get("required")
        if (not isinstance(required, list) or "workflow_family" in properties
                or "workflow_family" in required):
            raise ValueError("answered native workflow-family question was replayed")
    publishing = second.get("publishing")
    if publishing is None:
        questions = remaining.get("questions", []) if isinstance(remaining, dict) else []
        native_schema = native_remaining.get("requestedSchema", {}) if isinstance(native_remaining, dict) else {}
        native_properties = native_schema.get("properties", {}) if isinstance(native_schema, dict) else {}
        if (not any(isinstance(question, dict) and question.get("questionId") == "publish.source"
                    for question in questions)
                and "publish.source" not in native_properties):
            raise ValueError("unresolved publish source became a terminal draft")
    elif (not isinstance(publishing, dict) or publishing.get("intentId") != intent
          or publishing.get("sourceKind") != "FeatureLayer"
          or publishing.get("sourceId") not in context["datasetIds"]):
        raise ValueError("answered publish draft is not bound to the owned dataset")
    return intent


def declared_status(output, admin_preflight, declaration=None):
    """Compare configuration projections; never infer observed deployment health."""
    output = _mapping(output, "MCP release projection")
    preflight = _mapping(admin_preflight, "admin preflight")
    expected = _mapping(preflight.get("platformRelease"), "admin release projection")
    for name in PROJECTION:
        if name not in output or name not in expected or output[name] != expected[name]:
            raise ValueError("MCP release projection differs from independent admin preflight")
    if (type(output["releaseDeclared"]) is not bool or type(output["isCoVersioned"]) is not bool
            or not isinstance(output["serving"], list) or not isinstance(output["execution"], list)
            or not isinstance(output["skewedIds"], list)
            or (output["releaseVersion"] is not None and not isinstance(output["releaseVersion"], str))):
        raise ValueError("declared release projection has an invalid schema")
    if ((output["releaseDeclared"] and not output["releaseVersion"])
            or (not output["releaseDeclared"] and (output["releaseVersion"] is not None
                or output["isCoVersioned"]))):
        raise ValueError("declared release version and co-versioning state conflict")
    for projection in (output, expected):
        posture = _mapping(projection.get("evidencePosture"), "projection evidence posture")
        sources = posture.get("sources")
        if (posture.get("schemaVersion") != "1.0" or posture.get("status") != "complete"
                or not isinstance(sources, list) or len(sources) != 1):
            raise ValueError("release status lacks one complete source posture")
        source = _mapping(sources[0], "release evidence source")
        if (source.get("sourceId") != "honua_platform_release_status"
                or source.get("backendKind") != "configProjection"
                or source.get("backendId") != "control-plane-options"
                or source.get("completeness") != "complete"):
            raise ValueError("release status falsely claims an observed deployment backend")
    if declaration is not None:
        declaration = _mapping(declaration, "reviewed release declaration")
        environment = _mapping(declaration.get("environment"), "reviewed ControlPlane environment")
        bindings = _mapping(declaration.get("artifactBindings"), "reviewed artifact bindings")
        target = environment.get("ControlPlane__DeployTargets__0__TargetId")
        serving = output["serving"]
        if (declaration.get("schema") != "honua.mcp-ops-config-declaration/v1"
                or output["releaseVersion"] != environment.get("ControlPlane__PlatformRelease__Version")
                or output["releaseDeclared"] is not True or output["isCoVersioned"] is not False
                or output["skewedIds"] != declaration.get("expectedSkewedIds")
                or output["skewedIds"] != [target]
                or environment.get("ControlPlane__DeployTargets__0__ArtifactReference") != bindings.get("explicitTestTarget")
                or environment.get("ControlPlane__PlatformRelease__ServingArtifactReference") != bindings.get("declaredServing")
                or not isinstance(serving, list) or serving != [{
                    "id": target, "runtimeProfile": None,
                    "effectiveArtifactReference": bindings["explicitTestTarget"],
                    "projectedFromRelease": False, "skewed": True}]
                or declared_workers(environment) != [("managed", bindings.get("declaredServing"))]
                or not declared_execution(output["execution"], declared_workers(environment), target)):
            raise ValueError("declared status differs from the reviewed owned configuration skew")
    return {name: output[name] for name in PROJECTION}


WORKER_KEY = re.compile(r"ControlPlane__PlatformRelease__Workers__([0-9]+)__(RuntimeProfile|ArtifactReference)")
EXECUTION_FIELDS = {"id", "runtimeProfile", "effectiveArtifactReference", "projectedFromRelease", "skewed"}


def declared_workers(environment):
    """Return the reviewed release's worker images as (runtimeProfile, artifact) in index order.

    honua-server refuses to boot a release that declares serving without workers
    (PlatformReleaseValidation), so a reviewed declaration must carry them.
    """
    indexed = {}
    for key, value in environment.items():
        match = WORKER_KEY.fullmatch(key)
        if match:
            indexed.setdefault(int(match.group(1)), {})[match.group(2)] = value
    if (not indexed or sorted(indexed) != list(range(len(indexed)))
            or any(set(item) != {"RuntimeProfile", "ArtifactReference"}
                   or not isinstance(item["ArtifactReference"], str) or not item["ArtifactReference"]
                   or not isinstance(item["RuntimeProfile"], str)
                   for item in indexed.values())):
        raise ValueError("reviewed release declaration does not co-version a worker image")
    return [(indexed[i]["RuntimeProfile"], indexed[i]["ArtifactReference"]) for i in sorted(indexed)]


def resolve_worker(workers, runtime_profile):
    """Mirror honua-server PlatformReleaseDefinition.ResolveWorker: an exact
    case-insensitive profile match wins, else the first default (blank-profile) image."""
    default = None
    for profile, artifact in workers:
        if not profile.strip():
            default = default or artifact
        elif runtime_profile and runtime_profile.strip() and profile.lower() == runtime_profile.lower():
            return artifact
    return default


def declared_execution(execution, workers, target):
    """Every execution workload must inherit exactly what the declared workers project.

    The reviewed declaration owns no execution workload pins, so an explicit pin
    (projectedFromRelease false with an artifact), any skew, or a workload that
    resolves differently from the declared workers is not the reviewed configuration.
    Workloads themselves come from the candidate's own ControlPlane:ExecutionWorkloads.
    """
    if not isinstance(execution, list):
        return False
    seen = set()
    for entry in execution:
        if not isinstance(entry, dict) or set(entry) != EXECUTION_FIELDS:
            return False
        identifier, profile = entry["id"], entry["runtimeProfile"]
        if (not isinstance(identifier, str) or not identifier or identifier in seen or identifier == target
                or (profile is not None and not isinstance(profile, str))):
            return False
        seen.add(identifier)
        expected = resolve_worker(workers, profile)
        if (entry["effectiveArtifactReference"] != expected
                or entry["projectedFromRelease"] is not (expected is not None)
                or entry["skewed"] is not False):
            return False
    return True


def successful_tool(result):
    result = _mapping(result, "tool result")
    if (result.get("isError") is not False or not isinstance(result.get("structuredContent"), dict)
            or not isinstance(result.get("content"), list) or not result["content"]):
        raise ValueError("grounding/status result lacks successful structured output")
    for item in result["content"]:
        item = _mapping(item, "tool content")
        if item.get("type") != "text" or not isinstance(item.get("text"), str):
            raise ValueError("grounding/status result has unrelated media")
        text = item["text"].strip()
        if text.startswith("{") and not wire.same_json(strict_json(text), result["structuredContent"]):
            raise ValueError("tool text contradicts structured output")
    return result["structuredContent"]


def judge(fixture_bytes, bootstrap, manifest_bytes, declaration, observed):
    """Rejudge original source, exact wire and installed-client call sequence."""
    from mcp_managed_identity_fixture import verify_receipt

    authored = scenario(fixture_bytes, bootstrap)
    actual_declaration = declare_configuration(manifest_bytes,
        release_source_sha=declaration.get("releaseSourceSha"),
        candidate_source_sha=authored["candidateSourceSha"],
        candidate_image=authored["candidateImage"])
    if not wire.same_json(declaration, actual_declaration):
        raise ValueError("status declaration differs from governed manifest")
    expected_keys = set(authored) | {"canonicalClient", "clientVersion", "runtimeBefore", "runtimeAfter",
                                  "graphObservation", "preflightBefore", "preflightAfter", "declaration", "calls",
                                  "identityFixture", "limitedIdentityFixture", "credentialSalt"}
    if (not isinstance(observed, dict) or set(observed) != expected_keys
            or any(not wire.same_json(observed[key], value) for key, value in authored.items())
            or observed["canonicalClient"] not in ("Official MCP TypeScript SDK", "MCP Inspector")
            or not isinstance(observed["clientVersion"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", observed["clientVersion"])
            or not wire.same_json(observed["declaration"], declaration)
            or not wire.same_json(observed["runtimeBefore"], authored["runtimeBinding"])
            or not wire.same_json(observed["runtimeAfter"], authored["runtimeBinding"])):
        raise ValueError("grounding journey changed its authored candidate or runtime")
    actual_calls = observed["calls"]
    if not isinstance(actual_calls, list) or len(actual_calls) != len(calls(authored)):
        raise ValueError("grounding journey lacks the complete authored call sequence")
    identity = verify_receipt(observed["identityFixture"], authored["runtimeBinding"], "admin")
    fingerprint = identity["credentialFingerprint"]
    limited_identity = verify_receipt(observed["limitedIdentityFixture"], authored["runtimeBinding"], "limited-read")
    limited_fingerprint = limited_identity["credentialFingerprint"]
    if (not isinstance(observed["credentialSalt"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", observed["credentialSalt"])
            or observed["credentialSalt"] != observed["identityFixture"]["credentialSalt"]
            or observed["credentialSalt"] != observed["limitedIdentityFixture"]["credentialSalt"]
            or not fingerprint.startswith("scrypt:" + observed["credentialSalt"] + ":")
            or not limited_fingerprint.startswith("scrypt:" + observed["credentialSalt"] + ":")
            or limited_fingerprint == fingerprint or limited_identity["keyId"] == identity["keyId"]):
        raise ValueError("normal managed caller salt differs from original calls")
    graph = _mapping(observed["graphObservation"], "independent graph observation")
    if (graph.get("schema") != "honua.mcp-grounding-graph/v1"
            or any(graph.get(key) != authored[key] for key in ("candidateSourceSha", "candidateImage",
                                                          "fixtureSha256", "bootstrapSha256"))):
        raise ValueError("independent graph observation belongs to another candidate")
    # The independently retained SQL result is checked against owned setup;
    # this is not a second call to the database at judgment time.
    context = source_context(authored["ownedArea"], graph.get("metadata"),
        process_catalog_resource(_mapping(actual_calls[0], "catalog call").get("result")))
    preflights = []
    for name in ("preflightBefore", "preflightAfter"):
        preflight = _mapping(observed[name], "independent admin preflight")
        if (preflight.get("schema") != "honua.mcp-grounding-admin-preflight/v1"
                or preflight.get("credentialFingerprint") != fingerprint
                or not isinstance(preflight.get("responseSha256"), str)
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", preflight["responseSha256"])
                or any(preflight.get(key) != authored[key] for key in ("candidateSourceSha", "candidateImage",
                                                                       "fixtureSha256", "bootstrapSha256"))):
            raise ValueError("independent preflight belongs to another candidate")
        preflights.append(_mapping(preflight.get("projection"), "admin projection"))
    sessions = set()
    actual_intent = None
    initial_ambiguous = None
    status = None
    for index, actual in enumerate(actual_calls):
        expected = calls(authored, intent_id=actual_intent)[index]
        if (not isinstance(actual, dict) or set(actual) != set(expected) | {"wire", "client", "result"}
                or any(not wire.same_json(actual[key], value) for key, value in expected.items())):
            raise ValueError("actual call differs from authored goal, continuation or refusal")
        frame, session = wire.observed_wire(actual["wire"], expected["method"], expected["params"], expected["authenticated"])
        call_fingerprint = limited_fingerprint if expected["expectation"] == "permission_denied" else fingerprint
        if (session in sessions or actual["wire"].get("expectedCredentialFingerprint") != call_fingerprint
                or not wire.same_json(actual["result"], frame if "error" in frame else frame["result"])):
            raise ValueError("actual call changed managed caller, reused session or replaced wire output")
        sessions.add(session)
        wire.client_observation(actual["client"], frame, actual["wire"],
                                observed["canonicalClient"], observed["clientVersion"])
        if expected["expectation"] == "catalog":
            process_catalog_resource(actual["result"])
        elif expected["expectation"] == "ground":
            grounding_output(successful_tool(actual["result"]), context, authored["authoredGoal"])
        elif expected["expectation"] == "ambiguous":
            initial_ambiguous = successful_tool(actual["result"])
            actual_intent = _nonempty(_mapping(initial_ambiguous.get("draftIntent"), "ambiguous intent").get("intentId"), "intent ID")
        elif expected["expectation"] == "clarify":
            clarification_output(initial_ambiguous, successful_tool(actual["result"]),
                                 authored["clarificationGoal"], context)
        elif expected["expectation"] == "status":
            status = successful_tool(actual["result"])
            for preflight in preflights:
                declared_status(status, preflight, declaration)
        elif expected["expectation"] == "invalid_argument":
            negative_output(actual["result"], "invalid_argument")
        elif expected["expectation"] == "permission_denied":
            negative_output(actual["result"], "permission_denied")
        elif expected["expectation"] == "anonymous":
            wire.anonymous_tool_refusal(actual["result"])
        else:
            raise ValueError("unreviewed grounding call expectation")
    if actual_intent is None or status is None or context["revision"] < 1:
        raise ValueError("grounding journey omitted actual source/continuation/status")
    return {key: authored[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage",
                                          "operations", "requestSetSha256")} | {
        "canonicalClient": observed["canonicalClient"], "clientVersion": observed["clientVersion"],
        "positive": True, "clarificationContinuity": True, "declaredStatusOnly": True,
        "negative": True, "freshSession": True, "sourceReadback": True,
        "graphRevision": context["revision"], "intentId": actual_intent,
        "releaseVersion": status["releaseVersion"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--declaration", type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--calls", action="store_true")
    parser.add_argument("--call-intent-id")
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), strict_json(args.bootstrap.read_bytes())
        if args.observations is None:
            authored = scenario(fixture, bootstrap)
            value = {"calls": calls(authored, intent_id=args.call_intent_id)} if args.calls else authored
        else:
            if not args.manifest or not args.declaration:
                raise ValueError("grounding judgment requires original manifest and declaration")
            value = judge(fixture, bootstrap, args.manifest.read_bytes(),
                          strict_json(args.declaration.read_bytes()), strict_json(args.observations.read_bytes()))
        print(json.dumps(value, allow_nan=False, sort_keys=True))
    except (ValueError, KeyError, TypeError, OSError, OverflowError):
        print("owned grounding/status observation refused", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
