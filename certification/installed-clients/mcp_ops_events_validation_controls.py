"""Synthetic semantic mutations; never candidate/client/store execution evidence."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

import mcp_ops_events_validation as oracle
from mcp_ops_events_config_controls import selected as configuration_declaration, manifest as configuration_manifest
from mcp_ops_events_state_controls import seed, store
from mcp_ops_events_setup_controls import fixture_seed
from mcp_render_validation_controls import FIXTURE, bootstrap, runtime


def scenario_inputs():
    """Synthetic originals for authoring controls; never a SQL/runtime receipt."""
    selected_runtime = runtime()
    selected_runtime["startedAt"] = "2026-10-19T21:59:00Z"
    selected_seed = fixture_seed()
    raw = json.dumps(selected_seed)
    setup = {"schema": "honua.mcp-ops-events-setup/v1", "phase": "complete",
        "setupSqlSha256": "sha256:" + hashlib.sha256(oracle.SETUP_SQL.encode()).hexdigest(),
        "setupResponseText": raw, "setupResponseSha256": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(),
        "seed": selected_seed, "runtimeBinding": selected_runtime,
        "originalStore": {"store": store(), "runtimeBinding": copy.deepcopy(selected_runtime)}}
    manifest = configuration_manifest()
    manifest["candidate"]["ref"] = selected_runtime["candidateSourceSha"]
    manifest["components"]["honua-server"].update(sha=selected_runtime["candidateSourceSha"], digest="sha256:" + "c" * 64)
    declaration = configuration_declaration(manifest, source=selected_runtime["candidateSourceSha"], image=selected_runtime["candidateImage"])
    return setup, declaration


class OperateScenarioControls(unittest.TestCase):
    def author(self, setup=None, declaration=None, **kwargs):
        original, config = scenario_inputs()
        return oracle.scenario(FIXTURE, bootstrap(), setup if setup is not None else original,
            declaration if declaration is not None else config, "abcdef123456", "Official MCP TypeScript SDK", **kwargs)

    def test_four_operations_have_original_rows_filters_restart_and_both_refusal_roles(self):
        value = self.author()
        self.assertEqual(value["operations"], oracle.OPERATIONS)
        self.assertEqual(len(value["calls"]), 26)
        calls = {call["id"]: call for call in value["calls"]}
        self.assertEqual(len(calls), 26)
        self.assertEqual(calls["alerts"]["selectedEventIds"], [31, 32])
        self.assertEqual(calls["alerts-resolved"]["selectedEventIds"], [32])
        self.assertEqual(calls["alerts-empty"]["selectedEventIds"], [])
        for role in ("anonymous", "limited"):
            selected = [call for call in calls.values() if call["credentialRole"] == role]
            self.assertEqual(len(selected), 4)
            self.assertTrue(all(call["authenticated"] is (role != "anonymous") for call in selected))
        for original in ("findings", "findings-resource", "alerts", "operate"):
            self.assertEqual({**calls[original], "id": "restarted-" + original}, calls["restarted-" + original])

    def test_continuation_is_explicitly_unresolved_until_actual_first_page_supplies_it(self):
        second = next(call for call in self.author()["calls"] if call["id"] == "alerts-second")
        self.assertNotIn("cursor", second["params"]["arguments"])
        self.assertEqual(second["cursorFrom"], "alerts-first")
        continued = next(call for call in self.author(actual_cursor="actual-first-page-cursor")["calls"] if call["id"] == "alerts-second")
        self.assertEqual(continued["params"]["arguments"]["cursor"], "actual-first-page-cursor")
        for cursor in ("", True, {}, "x" * 1025):
            with self.subTest(cursor=type(cursor).__name__), self.assertRaises(ValueError):
                self.author(actual_cursor=cursor)

    def test_original_setup_sql_response_phase_and_candidate_are_required(self):
        mutations = (lambda v: v.update(phase="seeded"), lambda v: v.update(setupSqlSha256="sha256:" + "0" * 64),
            lambda v: v.update(setupResponseText=v["setupResponseText"] + " "),
            lambda v: v["seed"].update(actor="foreign"),
            lambda v: v["runtimeBinding"].update(candidateSourceSha="0" * 40),
            lambda v: v["originalStore"]["runtimeBinding"].update(containerId="f" * 64))
        for mutate in mutations:
            setup, config = scenario_inputs(); mutate(setup)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError): self.author(setup, config)

    def test_old_or_shared_event_window_and_missing_durable_rows_cannot_author_success(self):
        for mutate in (lambda v: v["runtimeBinding"].update(startedAt="2026-10-19T21:00:00Z"),
                       lambda v: v["originalStore"]["store"].update(windowEventIds=[31, 32, 33]),
                       lambda v: v["originalStore"]["store"].update(alerts=[])):
            setup, config = scenario_inputs(); mutate(setup)
            setup["originalStore"]["runtimeBinding"] = copy.deepcopy(setup["runtimeBinding"])
            with self.assertRaises(ValueError): self.author(setup, config)

    def test_read_fixture_cannot_be_rebranded_as_alert_engine_or_delivery_execution(self):
        for field in ("evaluationExercised", "deliveryExercised", "lifecycleApiExercised"):
            setup, config = scenario_inputs(); setup["seed"][field] = True
            setup["setupResponseText"] = json.dumps(setup["seed"])
            setup["setupResponseSha256"] = "sha256:" + hashlib.sha256(setup["setupResponseText"].encode()).hexdigest()
            with self.subTest(field=field), self.assertRaises(ValueError): self.author(setup, config)

    def test_configuration_cannot_select_a_different_candidate_or_omit_release(self):
        for mutate in (lambda v: v.update(candidateSourceSha="0" * 40),
                       lambda v: v.update(candidateImage="floating:nightly"),
                       lambda v: v["environment"].pop("ControlPlane__PlatformRelease__Version")):
            setup, config = scenario_inputs(); mutate(config)
            with self.assertRaises(ValueError): self.author(setup, config)


def context_vector():
    setup, declaration = scenario_inputs()
    selected_runtime = setup["runtimeBinding"]
    identity = {"credentialFingerprint": "scrypt:" + "a" * 32 + ":" + "b" * 64}
    preflight = json.dumps({"platformRelease": {"releaseVersion": "2026.1-rc.2", "releaseDeclared": True,
        "isCoVersioned": False, "skewedIds": ["cert-ops-skew"]}})
    value = {"schema": "honua.mcp-ops-events-context/v1", "startedAt": "2026-10-19T22:00:00Z",
        "completedAt": "2026-10-19T22:00:01.500Z", "runtimeBinding": copy.deepcopy(selected_runtime),
        "storeObservation": {"schema": "honua.mcp-ops-events-observation/v1", "store": store(),
            "runtimeBinding": copy.deepcopy(selected_runtime), "startedAt": "2026-10-19T22:00:00Z",
            "completedAt": "2026-10-19T22:00:01Z"},
        "configuration": {"schema": "honua.mcp-ops-config-observation/v1", "observedAt": "2026-10-19T22:00:00.1Z",
            "runtimeBinding": copy.deepcopy(selected_runtime), "environment": copy.deepcopy(declaration["environment"]),
            "environmentSha256": declaration["environmentSha256"], "selectedScopeEntryCount": 10,
            "deployedTargetObserved": False, "actuationRequested": False},
        "preflight": {"method": "GET", "url": "http://127.0.0.1:8080/api/v1/admin/deploy/preflight?includeDiagnostics=true",
            "status": 200, "responseText": preflight, "responseSha256": "sha256:" + hashlib.sha256(preflight.encode()).hexdigest(),
            "credentialFingerprint": identity["credentialFingerprint"],
            "startedAt": "2026-10-19T22:00:00.2Z", "completedAt": "2026-10-19T22:00:00.3Z"}}
    return value, setup, declaration, selected_runtime, identity


class OriginalOperateContextControls(unittest.TestCase):
    def judge(self, value=None):
        original, setup, declaration, selected_runtime, identity = context_vector()
        return oracle.context(value if value is not None else original, FIXTURE, bootstrap(), setup,
            declaration, selected_runtime, identity)

    def test_independent_original_store_daemon_options_and_authenticated_preflight_are_accepted(self):
        self.assertEqual(self.judge()["platformRelease"]["skewedIds"], ["cert-ops-skew"])

    def test_original_store_schema_identity_runtime_and_clock_cannot_be_substituted(self):
        mutations = (lambda v: v["storeObservation"].update(schema="fixture-success"),
            lambda v: v["runtimeBinding"].update(containerId="f" * 64),
            lambda v: v["storeObservation"]["runtimeBinding"].update(containerId="f" * 64),
            lambda v: v["storeObservation"]["store"]["databaseIdentity"].update(user="foreign"),
            lambda v: v["storeObservation"]["store"].update(windowEventIds=[31, 32, 33]),
            lambda v: v["storeObservation"].update(startedAt="2026-10-19T22:00:02Z"),
            lambda v: v.update(completedAt="2026-10-19T21:59:59Z"))
        for mutate in mutations:
            value = context_vector()[0]; mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError): self.judge(value)

    def test_daemon_option_coverage_hash_runtime_and_l1_boundary_are_required(self):
        mutations = (lambda v: v["configuration"].update(selectedScopeEntryCount=True),
            lambda v: v["configuration"].update(environmentSha256="sha256:" + "0" * 64),
            lambda v: v["configuration"]["environment"].update(ControlPlane__DeployTargets__0__RequiresApproval="false"),
            lambda v: v["configuration"]["runtimeBinding"].update(containerId="f" * 64),
            lambda v: v["configuration"].update(observedAt="2026-10-19T21:59:00Z"),
            lambda v: v["configuration"].update(actuationRequested=True),
            lambda v: v["configuration"].update(deployedTargetObserved=True))
        for mutate in mutations:
            value = context_vector()[0]; mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError): self.judge(value)

    def test_preflight_original_bytes_method_fixed_url_auth_and_observation_time_are_required(self):
        mutations = (lambda v: v["preflight"].update(responseText=v["preflight"]["responseText"] + " "),
            lambda v: v["preflight"].update(method="POST"),
            lambda v: v["preflight"].update(url="https://foreign.example/preflight"),
            lambda v: v["preflight"].update(status=True),
            lambda v: v["preflight"].update(credentialFingerprint="scrypt:" + "0" * 32 + ":" + "1" * 64),
            lambda v: v["preflight"].update(completedAt="2026-10-19T22:00:02Z"),
            lambda v: v["preflight"].update(startedAt="2026-10-19T21:59:00Z"))
        for mutate in mutations:
            value = context_vector()[0]; mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError): self.judge(value)

    def test_typed_limited_permission_denial_cannot_be_reauthentication_success_or_data(self):
        body = {"status": "error", "code": "permission_denied",
            "message": "Caller is not authorized to read operational observability.",
            "error": {"kind": "AuthorizationDenied", "message": "Caller is not authorized to read operational observability."}}
        result = {"isError": True, "structuredContent": body,
            "content": [{"type": "text", "text": json.dumps(body)}]}
        self.assertIsNone(oracle.limited_refusal(result))
        for mutate in (lambda v: v.update(isError=False),
                       lambda v: v["structuredContent"].update(code="unauthenticated", requiresReauthentication=True),
                       lambda v: v["structuredContent"].update(items=[{"eventId": 31}]),
                       lambda v: v["content"][0].update(text="success")):
            value = copy.deepcopy(result); mutate(value)
            with self.assertRaises(ValueError): oracle.limited_refusal(value)


def observations():
    original = {"store": store(), "runtimeBinding": {"containerId": "c" * 64},
        "startedAt": "2026-10-19T22:00:00Z", "completedAt": "2026-10-19T22:00:01Z"}
    after = copy.deepcopy(original)
    after.update(startedAt="2026-10-19T22:00:03Z", completedAt="2026-10-19T22:00:04Z")
    after["store"]["observedAt"] = after["startedAt"]
    return original, after


def response(value, summary="Alert-event page returned in structuredContent."):
    text = summary if len(json.dumps(value, separators=(",", ":"), ensure_ascii=True)) >= 1024 else json.dumps(value)
    return {"content": [{"type": "text", "text": text}], "structuredContent": value}


def alert_page(*, operate=False, paged=False, empty=False):
    occurred = "2026-10-19T21:51:00Z"
    item = {"eventId": 31, "ruleId": 20, "ruleName": "Canonical MCP Harbor Entry", "zoneId": 10,
        "serviceId": "canonical-mcp-ops", "layerId": 1, "objectId": 4201, "severity": "warning",
        "occurredAt": occurred, "triggerType": "enter", "incidentStatus": "started", "incidentDurationMs": 0,
        "lifecycleStatus": "open", "resourceRef": "alert/31"}
    if operate:
        item = {"eventId": "alert:31", "kind": "alert", "severity": "warning", "occurredAt": occurred,
            "title": "Enter alert on layer 1", "summary": "Canonical MCP Harbor Entry",
            "serviceId": "canonical-mcp-ops", "layerId": 1, "objectId": 4201, "resourceRef": "alert/31"}
    observed = "2026-10-19T22:00:02Z" if empty else occurred
    reasons = ([] if empty else ["stale"]) + (["truncated"] if paged else [])
    status = "unavailable" if "stale" in reasons else "partial" if reasons else "complete"
    coverage = {"hasMore": paged, "truncated": paged}
    if not empty:
        coverage.update(returnedFrom=occurred, returnedTo=occurred)
    source = {"sourceId": "honua_operate_events" if operate else "honua_alert_events", "backendKind": "durableStore",
        "backendId": "operate-event-feed" if operate else "alert-event-store", "observedAt": observed,
        "lastSuccessfulAt": observed, "maximumAgeSeconds": 300,
        "validUntil": (datetime.fromisoformat(observed.replace("Z", "+00:00")) + timedelta(minutes=5)).isoformat(),
        "completeness": status, "reasonCodes": sorted(reasons), "coverage": coverage}
    value = {"items": [] if empty else [item], "evidencePosture": {"schemaVersion": "1.0", "status": status, "sources": [source]}}
    if paged and not operate:
        value["nextCursor"] = "actual-wire-cursor-example"
    if operate:
        value["partialResult"] = False
    return value


class OperateSemanticControls(unittest.TestCase):
    def judge(self, value, *, operate=False, paged=False, empty=False):
        before, after = observations()
        summary = "Operate-event page returned in structuredContent." if operate else "Alert-event page returned in structuredContent."
        return oracle.event_output(response(value, summary), before, after, seed(), {}, [] if empty else [31],
            operate=operate, has_more=paged)

    def test_nonempty_original_alert_and_fused_feed_read_successfully_with_honest_staleness(self):
        self.assertIsNone(self.judge(alert_page()))
        self.assertIsNone(self.judge(alert_page(operate=True), operate=True))

    def test_page_cursor_and_honest_truncation_are_both_required(self):
        self.assertEqual(self.judge(alert_page(paged=True), paged=True), "actual-wire-cursor-example")
        for mutate in (lambda v: v.pop("nextCursor"), lambda v: v.update(nextCursor=""),
                       lambda v: v["evidencePosture"]["sources"][0]["coverage"].update(hasMore=False),
                       lambda v: v["evidencePosture"]["sources"][0]["coverage"].update(truncated=False)):
            value = alert_page(paged=True); mutate(value)
            with self.assertRaises(ValueError): self.judge(value, paged=True)

    def test_empty_window_requires_actual_fresh_query_observation(self):
        self.assertIsNone(self.judge(alert_page(empty=True), empty=True))
        value = alert_page(empty=True)
        value["evidencePosture"]["sources"][0]["observedAt"] = "2026-10-19T21:51:00Z"
        with self.assertRaises(ValueError): self.judge(value, empty=True)

    def test_every_original_alert_identity_geometry_scope_lifecycle_and_causal_field_is_checked(self):
        replacements = {"eventId": 32, "ruleId": 21, "ruleName": "foreign", "zoneId": 11,
            "serviceId": "foreign", "layerId": True, "objectId": 4202, "severity": "critical",
            "occurredAt": "2026-10-19T21:52:00Z", "triggerType": "exit", "incidentStatus": "ended",
            "incidentDurationMs": 1, "lifecycleStatus": "resolved", "resourceRef": "alert/32"}
        for key, replacement in replacements.items():
            with self.subTest(key=key):
                value = alert_page(); value["items"][0][key] = replacement
                with self.assertRaises(ValueError): self.judge(value)

    def test_fused_alert_projection_cannot_be_replaced_with_an_unrelated_event(self):
        for key, replacement in {"eventId": "audit:31", "kind": "audit", "title": "ok", "summary": "foreign",
                                 "resourceRef": "alert/32", "objectId": 4202}.items():
            with self.subTest(key=key):
                value = alert_page(operate=True); value["items"][0][key] = replacement
                with self.assertRaises(ValueError): self.judge(value, operate=True)

    def test_read_success_cannot_claim_recent_health_or_mask_partial_feed(self):
        mutations = (lambda v: v["evidencePosture"].update(status="complete"),
            lambda v: v["evidencePosture"]["sources"][0].update(completeness="complete", reasonCodes=[]),
            lambda v: v["evidencePosture"]["sources"][0].update(observedAt="2026-10-19T22:00:02Z"),
            lambda v: v["evidencePosture"]["sources"][0].update(backendId="fake"),
            lambda v: v["evidencePosture"]["sources"][0].update(maximumAgeSeconds=True))
        for mutate in mutations:
            value = alert_page(); mutate(value)
            with self.assertRaises(ValueError): self.judge(value)
        value = alert_page(operate=True); value["partialResult"] = True
        with self.assertRaises(ValueError): self.judge(value, operate=True)

    def test_original_row_or_database_or_runtime_change_inside_call_refuses(self):
        for mutate in (lambda v: v["store"]["databaseIdentity"].update(clusterId="999"),
                       lambda v: v["runtimeBinding"].update(containerId="d" * 64),
                       lambda v: v["store"]["alerts"][0].update(lifecycleCode=3)):
            before, after = observations(); mutate(after)
            with self.assertRaises(ValueError): oracle.bracket(before, after, seed())

    def test_text_projection_resource_media_and_wrapper_cannot_replace_actual_payload(self):
        value = alert_page(); wrapped = response(value); wrapped["content"][0]["text"] = "success"
        with self.assertRaises(ValueError): oracle.payload(wrapped, "Alert-event page returned in structuredContent.")
        resource = {"contents": [{"uri": oracle.FINDINGS_URI, "mimeType": "application/json", "text": json.dumps({"findings": []})}]}
        self.assertEqual(oracle.payload(resource, "", resource=True), {"findings": []})
        resource["contents"][0]["uri"] += "/wrong"
        with self.assertRaises(ValueError): oracle.payload(resource, "", resource=True)

    def test_duplicates_missing_rows_and_wrong_order_do_not_satisfy_nonempty_read(self):
        for items in ([], [alert_page()["items"][0]] * 2):
            value = alert_page(); value["items"] = items
            with self.assertRaises(ValueError): self.judge(value)
        before, after = observations()
        with self.assertRaises(ValueError): oracle.event_output(response(alert_page()), before, after, seed(), {}, [32, 31])


def findings_vector():
    declaration = configuration_declaration()
    generated = "2026-10-19T22:00:02Z"
    names = [oracle.FINDINGS_SOURCE + "." + suffix for suffix in oracle.FINDINGS_SECTIONS]
    backend = {"alert_dispatch": ("durableStore", "alert-dispatch-store"),
        "control_plane": ("configProjection", "control-plane-options"),
        "deploy_preflight": ("inProcess", "deploy-preflight-probe"),
        "gp_queue": ("durableStore", "execution-job-store"),
        "workflow_operations": ("durableStore", "workflow-operation-store"),
        "serving_latency_rollup": ("durableStore", "ops-health-rollup-store"),
        "database_pressure": ("durableStore", "database-pressure-signal"),
        "batch_backends": ("inProcess", "batch-compute-backends")}
    sources = []
    for name in names:
        kind, identifier = backend[name.removeprefix(oracle.FINDINGS_SOURCE + ".")]
        sources.append({"sourceId": name, "backendKind": kind, "backendId": identifier,
            "observedAt": generated, "lastSuccessfulAt": generated, "completeness": "complete",
            "reasonCodes": [], "maximumAgeSeconds": 300, "validUntil": "2026-10-19T22:05:02Z"})
    aggregate = {"sourceId": oracle.FINDINGS_SOURCE, "backendKind": "composite", "backendId": "ops-findings-engine",
        "observedAt": generated, "lastSuccessfulAt": generated, "completeness": "complete", "reasonCodes": [],
        "maximumAgeSeconds": 300, "validUntil": "2026-10-19T22:05:02Z",
        "coverage": {"includedComponentIds": sorted(names), "expectedComponentIds": sorted(names)}}
    # Literal contract material here is deliberately independent from the
    # producer's semantic mapper; synthetic source controls cannot certify it.
    release = "2026.1-rc.2"
    item = {"id": oracle.finding_identity(release), "rule": "platform-release-skew", "severity": "Warning",
        "title": "Platform release is not co-versioned", "detectedAt": generated,
        "subject": {"releaseVersion": release}, "requiredSourceIds": [oracle.CONTROL_SOURCE],
        "evidenceRefs": ["GET /api/v1/admin/deploy/preflight?includeDiagnostics=true", "skewed:cert-ops-skew"],
        "observationWindow": {"returnedFrom": generated, "returnedTo": generated},
        "explanation": "Declared platform release '2026.1-rc.2' is not co-versioned: 1 plane(s) explicitly pin an artifact "
            "that diverges from the release (cert-ops-skew). This is L1 guidance only: a runtime action cannot clear "
            "config-derived skew, since the pinned configuration would simply re-assert itself on the next reconcile. "
            "Remove or update the explicit pin, then redeploy the plane to the release revision to restore co-versioning."}
    value = {"generatedAt": generated, "findings": [item],
        "evidencePosture": {"schemaVersion": "1.0", "status": "complete", "sources": [aggregate, *sources]}}
    config = {"schema": "honua.mcp-ops-config-observation/v1", "environment": declaration["environment"],
        "environmentSha256": declaration["environmentSha256"], "selectedScopeEntryCount": 10,
        "observedAt": "2026-10-19T22:00:00Z", "deployedTargetObserved": False, "actuationRequested": False,
        "runtimeBinding": {"candidateSourceSha": declaration["candidateSourceSha"], "candidateImage": declaration["candidateImage"]}}
    preflight = {"platformRelease": {"releaseVersion": release, "releaseDeclared": True,
        "isCoVersioned": False, "skewedIds": ["cert-ops-skew"]}}
    return value, declaration, config, preflight


class FindingsSemanticControls(unittest.TestCase):
    def judge(self, vector, *, resource=False, expected_present=True):
        value, declaration, config, preflight = vector
        wrapped = {"contents": [{"uri": oracle.FINDINGS_URI, "mimeType": "application/json", "text": json.dumps(value)}]} if resource else response(value, "Ops findings returned in structuredContent.")
        return oracle.findings_output(wrapped, resource=resource, before="2026-10-19T22:00:01Z",
            after="2026-10-19T22:00:03Z", declaration=declaration, configuration=config,
            admin_preflight=preflight, expected_present=expected_present)

    def test_tool_and_resource_require_real_nonempty_config_derived_finding_contract(self):
        for resource in (False, True):
            result = self.judge(findings_vector(), resource=resource)
            self.assertEqual(result["findings"][0]["subject"], {"releaseVersion": "2026.1-rc.2"})

    def test_configuration_and_independent_preflight_cannot_be_substituted(self):
        mutations = (lambda v: v[2].update(environmentSha256="sha256:" + "0" * 64),
            lambda v: v[2]["environment"].update(ControlPlane__PlatformRelease__Version="foreign"),
            lambda v: v[2].update(selectedScopeEntryCount=11), lambda v: v[2].update(actuationRequested=True),
            lambda v: v[2]["runtimeBinding"].update(candidateSourceSha="c" * 40),
            lambda v: v[3]["platformRelease"].update(skewedIds=[]),
            lambda v: v[3]["platformRelease"].update(isCoVersioned=True))
        for mutate in mutations:
            value = findings_vector(); mutate(value)
            with self.assertRaises(ValueError): self.judge(value)

    def test_each_finding_identity_source_authority_and_observation_window_is_required(self):
        replacements = {"id": "foreign", "rule": "foreign", "severity": "Info", "subject": {},
            "title": "ok", "detectedAt": "2026-10-19T22:00:01Z", "evidenceRefs": [],
            "requiredSourceIds": [oracle.FINDINGS_SOURCE], "explanation": "Runtime deploy is approved",
            "observationWindow": {"returnedFrom": "2026-10-19T21:00:00Z", "returnedTo": "2026-10-19T22:00:02Z"}}
        for field, replacement in replacements.items():
            value = findings_vector(); value[0]["findings"][0][field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError): self.judge(value)

    def test_missing_duplicate_nonmatching_or_actionable_substitute_finding_refuses(self):
        for mutate in (lambda v: v[0].update(findings=[]),
                       lambda v: v[0]["findings"].append(copy.deepcopy(v[0]["findings"][0])),
                       lambda v: v[0]["findings"][0].update(recommendedAction={"autoSafe": True})):
            value = findings_vector(); mutate(value)
            with self.assertRaises(ValueError): self.judge(value)

    def test_all_original_sources_and_truthful_aggregate_are_required(self):
        for mutate in (lambda v: v[0]["evidencePosture"]["sources"].pop(),
                       lambda v: v[0]["evidencePosture"].update(status="unavailable"),
                       lambda v: v[0]["evidencePosture"]["sources"][0]["coverage"].update(includedComponentIds=[]),
                       lambda v: v[0]["evidencePosture"]["sources"][2].update(backendId="foreign"),
                       lambda v: v[0]["evidencePosture"]["sources"][2].update(observedAt="2026-10-19T21:00:00Z")):
            value = findings_vector(); mutate(value)
            with self.assertRaises(ValueError): self.judge(value)

    def test_nonmatching_filter_must_be_empty_without_losing_evaluation_posture(self):
        value = findings_vector(); value[0]["findings"] = []
        self.assertEqual(self.judge(value, expected_present=False)["findings"], [])
        with self.assertRaises(ValueError): self.judge(findings_vector(), expected_present=False)


if __name__ == "__main__":
    unittest.main()
