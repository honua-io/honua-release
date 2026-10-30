"""Actual serving and typed-refusal operations for the explicit Redis-off model profile.

No operation changes a dependency refusal into a completed GP job. The model's actual tool
request, an independent feature read and the live dependency manifest determine the outcome.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import executor
from transport import ActionRejected, ExecutionError, has_execution_evidence

PROFILE = "redis-off-serving-refusal"
PLAN = Path(__file__).resolve().parents[1] / "terminal-model-canary/redis-off-serving-refusal.v1.json"
STAGE_TOOLS = {1: {"honua_list_capabilities", "honua_list_layers", "honua_query_features"},
               2: {"honua_plan_analysis", "honua_validate_plan", "honua_dry_run_plan",
                   "honua_execute_plan", "honua_list_capabilities"}}
PLAN_TOOLS = ("honua_validate_plan", "honua_dry_run_plan", "honua_execute_plan")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def fixture_binding(path):
    """Bind resolved publication IDs to the independent maintained seed SQL, never either response."""
    raw = Path(path).read_bytes()
    manifest = json.loads(raw)
    publication = manifest.get("slice1", {}).get("e2e_src_fs", {})
    layer = publication.get("layerId")
    if (publication.get("service") != "e2e" or type(layer) is not int or layer < 0
            or manifest.get("layers", {}).get("e2e_src_fs") != layer):
        raise ExecutionError("serving fixture", "retained seed manifest does not bind the authored e2e publication")
    seed = (PLAN.parents[2] / "e2e/harness/seed/seed.sh").read_bytes()
    sql = seed.decode("utf-8").split("psql_apply <<'SQL'\n", 1)[1].split("\nSQL\n", 1)[0]
    match = re.search(r"INSERT INTO honua_data.e2e_src_fs \(name, geom\) VALUES\s*(.*?);", sql, re.S)
    if match is None or "gid   serial PRIMARY KEY" not in sql or "DROP TABLE IF EXISTS honua_data.e2e_src_fs;" not in sql:
        raise ExecutionError("serving fixture", "maintained seed SQL no longer matches the reviewed serial Point fixture")
    values = match.group(1)
    row = re.compile(r"\('((?:[^']|'')*)',\s*ST_SetSRID\(ST_MakePoint\((-?[0-9.]+),\s*(-?[0-9.]+)\),4326\)\)")
    rows = list(row.finditer(values))
    if len(rows) != 2 or row.sub("", values).strip(" ,\r\n\t"):
        raise ExecutionError("serving fixture", "maintained seed Point rows require a reviewed fixture update")
    expected = [{"id": str(index), "name": m[1].replace("''", "'"),
                 "geometry": {"type": "Point", "coordinates": [float(m[2]), float(m[3])]}}
                for index, m in enumerate(rows, 1)]
    return {"serviceId": "e2e", "layerId": layer, "seedManifestSha256": hashlib.sha256(raw).hexdigest(),
            "seedScriptSha256": hashlib.sha256(seed).hexdigest(), "seedSqlSha256": hashlib.sha256(sql.encode()).hexdigest(),
            "expectedFeaturesSha256": _digest(expected), "expectedFeatures": expected}


def _stage(request):
    if request.get("profile") != PROFILE:
        raise ExecutionError("model profile", "unknown model profile")
    stages = json.loads(PLAN.read_text())["stages"]
    stage = next((s for s in stages if s["id"] == request.get("stage")), None)
    if stage is None:
        raise ExecutionError("model profile", "stage does not belong to the reviewed refusal profile")
    return stage


def _context(driver, request):
    if request.get("profile") != PROFILE:
        raise ExecutionError("model profile", "unknown model profile")
    state, target, manifest, observation, workspace = driver._rehydrate(request)
    if executor.declared_redis_topology(target) != "redis-off":
        raise ExecutionError("model profile", "refusal profile requires a declared Redis-off cell")
    if not state.get("servingFixtureBinding"):
        raise ExecutionError("serving fixture", "profile requires the retained authored serving-fixture binding")
    engine = driver._executor(state, target, observation)
    profile = state.setdefault("modelRefusal", {"serving": None, "refusal": None, "acknowledged": False,
                                              "lastResult": None, "executionOutcome": "not-attempted"})
    return state, target, manifest, observation, engine, profile


def observe(driver, request):
    stage = _stage(request)
    state, _target, _manifest, observation, engine, profile = _context(driver, request)
    complete = bool(profile["serving"] if stage["number"] == 1 else profile["refusal"] and profile["acknowledged"])
    available = observation.ready and observation.setup_view_present and state.get("stackUp")
    status = "pass" if complete else "ready" if available else "blocked"
    tools = stage_tools(engine.view(), stage["number"]) if available else []
    return {"status": "pass" if complete else "ready" if available else "blocked",
            "stageStatus": {**stage, "status": status, "blockedBy": [] if available else ["candidate-serving"],
                "checks": [{"status": "pass", "invocation": stage["command"], "detail": "independent profile oracle passed"}] if complete else []},
            "observation": {**driver._observation_payload(observation),
                "profile": PROFILE, "lastActionResult": profile["lastResult"],
                "servingFixture": {key: state["servingFixtureBinding"][key] for key in ("serviceId", "layerId")},
                "requiredOutcome": {"executionOutcome": "refused", "functionalGpExecuted": False},
                "bufferFixture": {**((engine.fixture or {}).get("buffer") or {}),
                    "wkb": executor.oracles.point_wkb(*engine.fixture["buffer"]["point"])} if engine.fixture.get("buffer") else None,
                "planContract": plan_contract(engine.fixture, tools) if stage["number"] == 2 else None},
            "toolView": driver._tool_view(observation),
            # The only tool calls execute admits for this stage (same view, same reviewed set).
            "stageTools": tools}


def stage_tools(view, number):
    """The tool calls this stage admits: its reviewed set intersected with the observed bounded view.

    observe advertises exactly this list and execute admits exactly this list, both from engine.view(),
    so the model is never offered a tool the check refuses (run 41013398407 offered all 41 view tools).
    """
    names = [tool.get("name") for tool in view.get("tools") or [] if isinstance(tool, dict)]
    return sorted(name for name in STAGE_TOOLS.get(number, set()) if names.count(name) == 1)


def plan_contract(fixture, tools):
    """The independently authored buffer input validate_durable_plan requires, stated to the model.

    The candidate descriptors point at a catalog resource the bounded view cannot read, so without
    this the model cannot know the reviewed input encoding (string srid/distance, geodesic "false").
    """
    spec = (fixture or {}).get("buffer")
    if not spec:
        return None
    return {"tools": [name for name in PLAN_TOOLS if name in tools],
            "arguments": {"plan": {"planId": "<your token>", "intentId": "<your token>", "outputs": ["FeatureLayer"],
                "steps": [{"stepId": "<your token>", "kind": "Geoprocess", "processId": "geometry.buffer",
                           "inputs": {"wkb": executor.oracles.point_wkb(*spec["point"]), "srid": str(spec["srid"]),
                                      "distance": str(spec["distance"]), "geodesic": "false"}}]}},
            "requirement": "Call one of these tools with exactly this plan. Choose your own planId, intentId and "
                           "stepId tokens (letters, digits, '-', '_'); copy outputs, kind, processId and inputs "
                           "unchanged; add no other plan or step fields. A typed Redis refusal is the expected result."}


def action_summary(action):
    """Safe receipt detail for a rejected action: its kind and tool name when each is an identifier."""
    action = action if isinstance(action, dict) else {}
    parts = []
    for key in ("kind", "tool"):
        value = action.get(key)
        if value is None:
            continue
        parts.append(f"{key}={value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else '<non-identifier>'}")
    return "; ".join(parts) or "no kind or tool"


ASSESSMENT_FIELDS = ("executionOutcome", "missingDependency", "functionalGpExecuted")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")


def _shown(value):
    """A receipt-safe rendering: short enum-like tokens and scalars verbatim, never free text."""
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, (int, float)) and len(repr(value)) <= 32:
        return repr(value)
    if isinstance(value, str):
        return repr(value) if _TOKEN.fullmatch(value) else f"<string of {len(value)} chars>"
    return "<object>" if isinstance(value, dict) else "<array>" if isinstance(value, list) else "<other>"


def assessment_problems(action, refusal):
    """Why the model's action-level assessment does not acknowledge the observed typed refusal.

    It must say executionOutcome "refused", functionalGpExecuted false (the boolean) and name the
    dependency the server's typed refusal named (case-insensitively; nothing is hard-coded), with no
    other keys. Each problem names its field and shows the value only through _shown, so a model's
    free text never reaches the receipt notice. An empty list means the acknowledgement holds.
    """
    observed = refusal.get("missingDependency") if isinstance(refusal, dict) else None
    if not isinstance(observed, str) or not observed:
        return ["observed typed refusal names no missingDependency"]
    if "assessment" not in action:
        arguments = action.get("arguments")
        nested = isinstance(arguments, dict) and "assessment" in arguments
        return ["assessment absent" + (" (placed inside the tool arguments)" if nested else "")]
    assessment = action["assessment"]
    if not isinstance(assessment, dict):
        return [f"assessment is not an object ({_shown(assessment)})"]
    problems = []
    for field in ASSESSMENT_FIELDS:
        if field not in assessment:
            problems.append(f"{field} absent")
            continue
        value = assessment[field]
        if field == "executionOutcome":
            matches = value == "refused"
        elif field == "missingDependency":
            matches = isinstance(value, str) and value.casefold() == observed.casefold()
        else:
            matches = value is False
        if not matches:
            problems.append(f"{field}={_shown(value)}")
    extra = len(set(assessment) - set(ASSESSMENT_FIELDS))
    if extra:
        problems.append(f"{extra} unexpected key{'s' if extra > 1 else ''}")
    return problems


def _reference_features(transport, arguments):
    # The independent oracle uses a distinct serving adapter, a fixed unfiltered read and an
    # encoded service/layer path. The model cannot select a host, URL or credential.
    service, layer = arguments.get("serviceId"), arguments.get("layerId")
    if (not isinstance(service, str) or not service or type(layer) is not int or layer < 0
            or arguments.get("where") not in (None, "", "1=1")
            or arguments.get("bbox") is not None or arguments.get("cursor") is not None
            or arguments.get("resultOffset", 0) != 0 or arguments.get("returnCountOnly", False)
            or arguments.get("returnGeometry", True) is not True
            or arguments.get("outFields") not in (None, "*", ["*"])
            or arguments.get("outSrid", 4326) != 4326):
        raise ExecutionError("serving oracle", "profile requires an unfiltered first-page feature/geometry read")
    limit, precision = arguments.get("limit", 100), arguments.get("geometryPrecision", 6)
    if type(limit) is not int or not 1 <= limit <= 100 or type(precision) is not int or not 0 <= precision <= 12:
        raise ExecutionError("serving oracle", "serving read exceeds the reviewed page/precision bound")
    query = urllib.parse.urlencode({"f": "geojson", "where": "1=1", "outFields": "*", "outSR": 4326,
                                   "returnGeometry": "true", "resultRecordCount": limit, "geometryPrecision": precision})
    path = f"/rest/services/{urllib.parse.quote(service, safe='')}/FeatureServer/{layer}/query"
    credential = transport.credential("operator")
    headers = {"Accept": "application/geo+json, application/json", "Cache-Control": "no-cache"}
    headers["Authorization" if credential.startswith("Bearer ") else "X-API-Key"] = credential
    url = transport.base_url.rstrip("/") + path + "?" + query
    try:
        with transport.opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 4 * 1024 * 1024:
                raise ExecutionError("serving oracle", "independent serving read failed or exceeded its bound")
    except (OSError, urllib.error.URLError) as exc:
        raise ExecutionError("serving oracle", "independent serving read failed") from exc
    return json.loads(raw)


def serving_proof(output, reference, fixture):
    actual = output.get("geojson", output.get("geoJson"))
    if isinstance(actual, str):
        actual = json.loads(actual)
    if (not isinstance(actual, dict) or actual.get("type") != "FeatureCollection"
            or not isinstance(reference, dict) or reference.get("type") != "FeatureCollection"):
        raise ExecutionError("serving oracle", "both adapters must return actual FeatureCollections")
    features, other = actual.get("features"), reference.get("features")
    if (not isinstance(features, list) or not 1 <= len(features) <= 100
            or not isinstance(other, list) or len(features) != len(other)):
        raise ExecutionError("serving oracle", "serving query must return a bounded nonempty feature page")
    indexed = {str(f.get("id")): f for f in other if isinstance(f, dict) and f.get("id") is not None}
    seen = set()
    for feature in features:
        if not isinstance(feature, dict) or feature.get("id") is None:
            raise ExecutionError("serving oracle", "served feature has no independently joinable identity")
        key = str(feature["id"])
        expected = indexed.get(key)
        if (key in seen or expected is None or not feature.get("geometry")
                or feature.get("geometry") != expected.get("geometry")
                or feature.get("properties") != expected.get("properties")):
            raise ExecutionError("serving oracle", "model-selected feature read disagrees with the independent adapter")
        seen.add(key)
    expected = fixture["expectedFeatures"]
    normalized = [{"id": str(f["id"]), "name": (f.get("properties") or {}).get("name"), "geometry": f["geometry"]}
                  for f in sorted(features, key=lambda f: str(f["id"]))]
    if normalized != expected or _digest(normalized) != fixture["expectedFeaturesSha256"]:
        raise ExecutionError("serving fixture", "both adapters disagree with the independently authored fixture data")
    return {"fixtureBinding": {key: value for key, value in fixture.items() if key != "expectedFeatures"},
            "featureCount": len(features), "featureIds": sorted(seen), "featuresSha256": _digest(features),
            "independentReadMatches": True}


def validate_durable_plan(arguments, fixture):
    """The disabled job service can refuse before parsing; independently require valid authored input."""
    plan = arguments.get("plan")
    spec = fixture.get("buffer") or {}
    if (not isinstance(plan, dict) or not spec or not executor._token(plan.get("planId"))
            or not executor._token(plan.get("intentId")) or plan.get("outputs") != ["FeatureLayer"]
            or set(plan) - {"planId", "intentId", "steps", "outputs"}
            or not isinstance(plan.get("steps"), list) or len(plan["steps"]) != 1):
        raise ExecutionError("durable plan", "model must select one valid authored buffer plan before refusal can count")
    step = plan["steps"][0]
    expected = {"wkb": executor.oracles.point_wkb(*spec["point"]), "srid": str(spec["srid"]),
                "distance": str(spec["distance"]), "geodesic": "false"}
    if (not isinstance(step, dict) or not executor._token(step.get("stepId"))
            or set(step) != {"stepId", "kind", "processId", "inputs"}
            or step.get("kind") != "Geoprocess" or step.get("processId") != "geometry.buffer"
            or step.get("inputs") != expected):
        raise ExecutionError("durable plan", "model-selected plan differs from the independently authored valid buffer input")


def execute(driver, request):
    """Execute the model's action; a rejected action is model feedback, never infrastructure.

    An action outside the stage's admitted tools or an assessment that does not acknowledge the
    observed refusal returns an invalid-action response (as live_driver.op_execute does for the full
    workflow), so the runner can return the bounded reason to the model. No state changes.
    """
    stage = _stage(request)
    try:
        return _execute(driver, request, stage)
    except ActionRejected as exc:
        return {"status": "fail", "stageStatus": stage, "refusal": "invalid-action",
                "result": {"accepted": False, "command": exc.command, "reason": exc.reason},
                "canonicalIds": {}, "blockedBy": []}


def _execute(driver, request, stage):
    state, _target, _manifest, _observation, engine, profile = _context(driver, request)
    action = request.get("action") or {}
    view = engine.view()
    name, arguments = action.get("tool"), action.get("arguments")
    descriptors = [tool for tool in view["tools"] if tool.get("name") == name]
    if (action.get("kind") != "tool_call" or name not in stage_tools(view, stage["number"])
            or len(descriptors) != 1 or not isinstance(arguments, dict)):
        # The notice carries the kind and tool name (identifiers only), never the arguments.
        raise ActionRejected("model action", "action is outside the refusal stage and observed bounded view "
                             f"({action_summary(action)})")
    import jsonschema
    try:
        jsonschema.validate(arguments, descriptors[0]["inputSchema"])
    except jsonschema.ValidationError as exc:
        raise ExecutionError("model action", "arguments differ from the observed server schema") from exc
    if name in PLAN_TOOLS:
        validate_durable_plan(arguments, engine.fixture)
    try:
        raw = engine.transport.tool(name, arguments, view, "operator")
        if "isError" in raw and type(raw["isError"]) is not bool:
            raise ExecutionError("model action", "candidate tool result has a nonboolean error marker")
        if raw.get("isError") is True and has_execution_evidence(raw):
            raise ExecutionError("model action", "typed refusal carries contradictory execution evidence")
        output = executor.structured(raw, name, executor.credential_values(engine.transport.credentials))
    except ExecutionError as exc:
        if name not in {"honua_validate_plan", "honua_dry_run_plan", "honua_execute_plan"} or not engine.record_topology_refusal(5, exc):
            raise
        profile["refusal"] = dict(engine.evidence["topologyRefusals"]["5"])
        profile["executionOutcome"] = "refused"
        result = {"accepted": False, "executionOutcome": "refused", "functionalGpExecuted": False,
                  "typedRefusal": profile["refusal"]}
    else:
        if name == "honua_execute_plan":
            profile["executionOutcome"] = "unexpectedly-accepted"
            result = {"accepted": True, "executionOutcome": "unexpectedly-accepted", "functionalGpExecuted": None}
            profile["lastResult"] = result
            driver._write_state(state["workspaceId"], state)
            return {"status": "pass", "stageStatus": stage, "result": result, "canonicalIds": {}, "blockedBy": []}
        if name == "honua_query_features":
            fixture = state["servingFixtureBinding"]
            if any(arguments.get(key) != fixture[key] for key in ("serviceId", "layerId")):
                raise ExecutionError("serving fixture", "model-selected query addresses another service or layer")
            profile["serving"] = serving_proof(output, _reference_features(engine.transport, arguments), fixture)
            result = {"accepted": True, "servingProof": profile["serving"]}
        elif name == "honua_list_capabilities" and profile["refusal"]:
            problems = assessment_problems(action, profile["refusal"])
            if problems:
                raise ActionRejected("model assessment", "acknowledgement of the observed refusal differs: "
                                     + "; ".join(problems))
            profile["acknowledged"] = True
            result = {"accepted": True, "modelAcknowledgedRefusal": True, "executionOutcome": "refused"}
        else:
            # Only bounded, public catalog metadata enters the next model prompt.
            result = {"accepted": True, "catalog": output}
    profile["lastResult"] = result
    driver._write_state(state["workspaceId"], state)
    return {"status": "pass", "stageStatus": stage, "result": result, "canonicalIds": {}, "blockedBy": []}


def verify(driver, request):
    state, _target, manifest, observation, engine, profile = _context(driver, request)
    transport = engine.transport
    # Independent checks must prove authentication and permission enforcement rather than a
    # blanket unavailable endpoint. No unpublished/private resource or tenant-isolation claim.
    transport.http("GET", "/api/v1/admin/api-keys/", principal="operator", expected=(200,))
    transport.http("GET", "/api/v1/admin/api-keys/", principal=None, expected=(401, 403))
    transport.http("GET", "/api/v1/admin/api-keys/", principal="viewer", expected=(403,))
    credential_probe = observation.credential_probe
    server = (observation.capability_manifest or {}).get("server") or {}
    revision = server.get("deploymentRevision", server.get("DeploymentRevision"))
    discovery = observation.setup_discovery or {}
    http, proxy = discovery.get("http", {}), discovery.get("proxy", {})
    assertions = {"servingRead": bool(profile["serving"]), "typedRedisRefusal": bool(profile["refusal"]),
                  "modelAcknowledgedRefusal": profile["acknowledged"],
                  "authentication": bool(credential_probe and credential_probe.status == "pass"
                                         and revision == manifest["components"]["honua-server"]["sha"]),
                  "anonymousDenial": True, "viewerDenial": True,
                  "catalogExact": bool(observation.setup_view_present and observation.proxy_available
                    and http.get("status") == proxy.get("status") == "pass"
                    and http.get("fullCatalogComparisonSha256")
                    and http["fullCatalogComparisonSha256"] == proxy.get("fullCatalogComparisonSha256"))}
    return {"status": "pass" if all(assertions.values()) else "fail",
            "assertions": {key: "pass" if value else "fail" for key, value in assertions.items()},
            "executionOutcome": profile["executionOutcome"], "functionalGpExecuted": False,
            "servingProof": profile["serving"], "typedRefusal": profile["refusal"],
            "canonicalIds": {}, "finalUrlProof": None, "pixelProof": None}
