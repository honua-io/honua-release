"""Gated operator calls and the human approval session (stages 3, 4 and 8)."""
import copy
import json
from pathlib import Path
from unittest import mock

import pytest

import executor
import gated_approval
import stages
import transport as transport_module
from test_executor import (CANARY_NATIVE_PUBLICATION_KEYS, LOCAL_TARGET, PUBLISH_OUTPUT, SEALED_DIFF, SEALED_HANDLE,
                           approval_engine, publish_arguments, upload_engine)
from transport import ExecutionError

AWAITING_PUBLICATION = {**{key: PUBLISH_OUTPUT[key] for key in ("operationId", "operationInstanceId", "correlationId",
                                                                  "auditId")},
                        "status": "RequiresApproval", "requiresApproval": True, "proposalId": "proposal-publish-1",
                        "policyOutcome": "RequireApproval", "message": "Proposal created and awaiting approval."}
OPERATOR_ACTOR = "apikey:api-key:0b6f2f8e-0a4e-4b8a-9d55-6f1d2c3b4a59"
HUMAN_ACTOR = "cookie:name:admin"
PENDING_PUBLICATION = {"proposalId": "proposal-publish-1", "status": "Pending", "requestedBy": OPERATOR_ACTOR,
                       "summary": "Publish 'public.imported_journey_source' as layer 'journey_source'."}
APPROVED_PUBLICATION = {**PENDING_PUBLICATION, "status": "Succeeded", "resolvedBy": HUMAN_ACTOR}
PUBLISHED_HANDLE = {"data": {**{key: PUBLISH_OUTPUT[key] for key in ("operationId", "operationInstanceId",
                                                                    "correlationId", "auditId")},
                             "proposalId": "proposal-publish-1", "status": "Completed",
                             "resourceIds": {"storageLayerId": "7", "serviceName": "journey"},
                             "result": {"summary": "published", "details": {"storageLayerId": "7",
                                        "serviceLayerIndex": "0", "serviceName": "journey"}}}}


def awaiting_publication_engine(*reads):
    engine = upload_engine(operator="private-operator")
    engine.resources.update(importTable="imported_journey_source", importSchema="public")
    engine.transport.tool.return_value = {"structuredContent": copy.deepcopy(AWAITING_PUBLICATION)}
    engine.transport.get_json.side_effect = [copy.deepcopy(read) for read in reads]
    return engine


def publish(engine):
    return engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service", "arguments": publish_arguments(engine)})


def test_publication_proposal_is_approved_by_a_person_after_the_operator_is_refused(human_approver):
    features = {"type": "FeatureCollection", "features": copy.deepcopy(LOCAL_TARGET["execution"]["features"])}
    engine = awaiting_publication_engine(PENDING_PUBLICATION, PENDING_PUBLICATION, APPROVED_PUBLICATION,
                                         PUBLISHED_HANDLE, features)
    engine.evidence["checks"]["3"] = {
        name: {"id": f"3.{name}", "kind": "artifact", "invocation": name, "status": "pass", "detail": "ok"}
        for name in ("datasource", "import-complete")}
    result = publish(engine)
    engine.transport.http.assert_called_once_with("POST", "/api/v1/admin/proposals/proposal-publish-1/approve",
                                                  principal="operator", expected=(403,))
    assert human_approver.call_args.args == ("https://localhost:8139", "proposal-publish-1",
                                             "private-admin-password", "/run/tls.crt")
    reads = [call.args[0] for call in engine.transport.get_json.call_args_list]
    assert reads[:4] == ["/api/v1/admin/proposals/proposal-publish-1"] * 3 + [
        "/api/v1/operations/handles/opinst-publish-1"]
    assert reads[4] == "/ogc/features/collections/7/items"
    assert engine.resources["layerId"] == 7 and engine.resources["serviceLayerIndex"] == 0
    assert engine.resources["serviceUri"] == "honua://published-services/journey"
    assert "proposalId" not in engine.resources and "publicationOperation" not in engine.evidence
    assert {key: value for key, value in engine.evidence["operatorApprovals"]["3"].items()
            if key != "serviceUriSource"} == {"proposalId": "proposal-publish-1", "requestedBy": OPERATOR_ACTOR,
        "resolvedBy": HUMAN_ACTOR, "approvalChannel": "console-interactive-password", "operatorSelfApproval": "denied"}
    assert set(result["nativePublication"]) == CANARY_NATIVE_PUBLICATION_KEYS
    assert engine.evidence["operatorApprovals"]["3"]["serviceUriSource"] == "derived-from-handle"
    assert engine.result(3).status == "pass"


def test_publication_refused_by_the_human_session_is_not_a_published_layer(human_approver):
    human_approver.return_value = {"result": "refused", "stage": "session", "httpStatus": 401,
                                   "detail": "bad private-admin-password"}
    engine = awaiting_publication_engine(PENDING_PUBLICATION, PENDING_PUBLICATION)
    with pytest.raises(ExecutionError, match=r"human approval did not approve the proposal \(result=refused") as error:
        publish(engine)
    assert "private-admin-password" not in error.value.reason
    assert "layerId" not in engine.resources and "operatorApprovals" not in engine.evidence


def test_publication_without_an_https_approval_channel_is_blocked():
    engine = awaiting_publication_engine(PENDING_PUBLICATION, PENDING_PUBLICATION)
    with mock.patch.object(gated_approval.local_fixture, "human_approval_channel", return_value=None):
        with pytest.raises(ExecutionError, match="no HTTPS human approval channel") as error:
            publish(engine)
    assert error.value.blocked is True


@pytest.mark.parametrize("approved,handle,reason", [
    ({**APPROVED_PUBLICATION, "resolvedBy": OPERATOR_ACTOR}, PUBLISHED_HANDLE, "separate principal resolution"),
    ({**APPROVED_PUBLICATION, "status": "Rejected"}, PUBLISHED_HANDLE, "separate principal resolution"),
    (APPROVED_PUBLICATION, {"data": {**PUBLISHED_HANDLE["data"], "status": "Failed"}}, "did not complete"),
    (APPROVED_PUBLICATION, {"data": {**PUBLISHED_HANDLE["data"], "proposalId": "other"}}, "does not join the proposal"),
    (APPROVED_PUBLICATION, {"data": {**PUBLISHED_HANDLE["data"], "operationId": "style.apply-preset"}},
     "not the service publication"),
])
def test_approved_publication_must_resolve_separately_and_complete_its_own_handle(approved, handle, reason):
    engine = awaiting_publication_engine(PENDING_PUBLICATION, PENDING_PUBLICATION, approved, handle)
    with pytest.raises(ExecutionError, match=reason):
        publish(engine)
    assert "layerId" not in engine.resources


def test_publication_proposal_that_is_not_pending_is_never_approved(human_approver):
    engine = awaiting_publication_engine({**PENDING_PUBLICATION, "status": "Succeeded", "resolvedBy": OPERATOR_ACTOR})
    with pytest.raises(ExecutionError, match="did not persist awaiting approval"):
        publish(engine)
    human_approver.assert_not_called()
    engine.transport.http.assert_not_called()


def test_journey_stack_serves_the_human_approval_session():
    import yaml
    compose = yaml.safe_load((Path(__file__).parent / "targets" / "build-compose.yml").read_text())
    server = compose["services"]["server"]
    env = server["environment"]
    assert env["Kestrel__Endpoints__Https__Url"] == "https://+:8443"
    assert env["Security__DisableHttpsRedirection"] == "true"
    # The server answers the approval review 503 until a public base URL binds the capability.
    assert env["Public__BaseUrl"] == "http://127.0.0.1:${HONUA_SERVER_PORT:-8137}"
    assert "${HONUA_JOURNEY_HTTPS_PORT:-8139}:8443" in server["ports"]
    assert any(volume.endswith(":/certs:ro") for volume in server["volumes"])
    assert LOCAL_TARGET["compose"]["httpsPort"] == 8139


STYLE_PROPOSAL = {"proposalId": "proposal-style-1", "status": "Pending", "requestedBy": OPERATOR_ACTOR,
                  "summary": "Apply style 'journey-solid-red' on service 'journey', layer '0'."}
STYLE_GATED = {"isError": True, "structuredContent": {
    "status": "error", "code": "failed_precondition", "approvalRequired": True, "proposalId": "proposal-style-1",
    "resourceUri": "honua://proposals/proposal-style-1", "message": "This operation requires approval",
    "error": {"kind": "ApprovalRequired"}}}
STYLE_RESOURCE = {"contents": [{"uri": "honua://proposals/proposal-style-1", "text": json.dumps(
    {"proposalId": "proposal-style-1", "operationInstanceId": "opinst-style-1", "status": "Pending"})}]}
STYLE_HANDLE = {"data": {"operationInstanceId": "opinst-style-1", "operationId": "style.apply-preset",
                         "correlationId": "00-corr-style", "auditId": "audit-style-1", "status": "Completed"}}


def style_engine(*reads, handle=STYLE_HANDLE):
    transport = mock.Mock(credentials={"proposer": "private-proposer", "operator": "private-operator"})
    observation = stages.Observation(setup_view_present=True, setup_discovery={
        "tools": [{"name": "honua_apply_style_preset", "inputSchema": {"type": "object"}}], "metadata": {"view": "setup"}})
    engine = executor.JourneyExecutor({"workspaceId": "workspace"}, {"execution": {"serviceId": "journey",
        "styleId": "journey-solid-red"}}, observation, transport)
    engine.resources.update(layerId=7, serviceLayerIndex=0)
    transport.tool.return_value = copy.deepcopy(STYLE_GATED)
    transport.resource_read.return_value = copy.deepcopy(STYLE_RESOURCE)
    transport.get_json.side_effect = [copy.deepcopy(read) for read in (*reads, handle)]
    return engine


def apply_style(engine):
    return engine.execute(4, {"kind": "tool_call", "tool": "honua_apply_style_preset", "arguments": {
        "serviceId": "journey", "serviceLayerIndex": 0, "styleId": "journey-solid-red"}})


def test_gated_style_application_is_approved_by_a_person_and_joined_on_its_replay(human_approver):
    engine = style_engine(STYLE_PROPOSAL, STYLE_PROPOSAL, {**STYLE_PROPOSAL, "status": "Succeeded", "resolvedBy": HUMAN_ACTOR})
    apply_style(engine)
    engine.transport.http.assert_called_once_with("POST", "/api/v1/admin/proposals/proposal-style-1/approve",
                                                  principal="operator", expected=(403,))
    assert engine.transport.resource_read.call_args.args[0] == "honua://proposals/proposal-style-1"
    assert human_approver.call_args.args[1] == "proposal-style-1"
    assert engine.transport.get_json.call_args.args[0] == "/api/v1/operations/handles/opinst-style-1"
    assert engine.evidence["checks"]["4"]["style-applied"]["status"] == "pass"
    assert engine.evidence["operatorApprovals"]["4"]["resolvedBy"] == HUMAN_ACTOR
    assert engine.evidence["canonicalIds"]["4"]["operationInstanceId"] == "opinst-style-1"


@pytest.mark.parametrize("handle,reason", [
    ({"data": {**STYLE_HANDLE["data"], "operationId": "service.publish"}}, "not the style application"),
    ({"data": {**STYLE_HANDLE["data"], "status": "Failed"}}, "did not apply the selected canonical style"),
    ({"data": {**STYLE_HANDLE["data"], "operationInstanceId": "other"}}, "does not join the proposal"),
])
def test_approved_style_replay_must_be_the_completed_style_application(handle, reason):
    engine = style_engine(STYLE_PROPOSAL, STYLE_PROPOSAL, {**STYLE_PROPOSAL, "status": "Succeeded",
                          "resolvedBy": HUMAN_ACTOR}, handle=handle)
    with pytest.raises(ExecutionError, match=reason):
        apply_style(engine)
    assert "style-applied" not in engine.evidence["checks"].get("4", {})


def test_approval_refusal_without_its_own_resume_resource_stays_contradictory():
    gated = copy.deepcopy(STYLE_GATED)
    assert transport_module.pending_approval(gated) == "proposal-style-1"
    for change in ({"resourceUri": "honua://proposals/other"}, {"approvalRequired": False},
                   {"code": "invalid_argument"}, {"proposalId": "bad id"}):
        assert transport_module.pending_approval({**gated, "structuredContent": {**gated["structuredContent"], **change}}) is None
    assert transport_module.pending_approval({**gated, "isError": False}) is None


@pytest.mark.parametrize("summary", ["Apply style 'other' on service 'journey', layer '0'.",
                                     "Apply style 'journey-solid-red' on service 'journey', layer '1'.",
                                     "Apply style 'journey-solid-red' on service '', layer '0'.",
                                     "Preview style 'journey-solid-red' on service 'journey', layer '0'.", None])
def test_gated_proposal_for_another_call_is_never_approved(human_approver, summary):
    engine = style_engine({**STYLE_PROPOSAL, "summary": summary})
    with pytest.raises(ExecutionError, match="does not describe the operator's call"):
        apply_style(engine)
    human_approver.assert_not_called()
    engine.transport.http.assert_not_called()


def test_operator_self_approval_that_changes_the_proposal_is_not_a_refusal(human_approver):
    engine = style_engine(STYLE_PROPOSAL, {**STYLE_PROPOSAL, "status": "Executing"})
    with pytest.raises(ExecutionError, match="not refused without mutation"):
        apply_style(engine)
    human_approver.assert_not_called()


def test_approval_refusal_carrying_other_execution_evidence_stays_contradictory():
    gated = copy.deepcopy(STYLE_GATED)
    assert not transport_module.has_execution_evidence(transport_module.without_resume_resource(gated))
    for extra in ({"jobId": "job-1", "jobStatus": "Completed"}, {"operationInstanceId": "opinst-1"}):
        tampered = {**gated, "structuredContent": {**gated["structuredContent"], **extra}}
        assert transport_module.has_execution_evidence(transport_module.without_resume_resource(tampered))
    text = {**gated, "content": [{"type": "text", "text": json.dumps({**gated["structuredContent"], "jobId": "job-1"})}]}
    assert transport_module.has_execution_evidence(transport_module.without_resume_resource(text))


def test_hung_human_approval_session_is_a_typed_failure(human_approver):
    human_approver.side_effect = gated_approval.subprocess.TimeoutExpired(["human_approval.py"], 180)
    engine = style_engine(STYLE_PROPOSAL, STYLE_PROPOSAL)
    with pytest.raises(ExecutionError, match="did not finish within its budget"):
        apply_style(engine)


def test_stage_eight_waits_for_a_submitted_replay_to_complete():
    transport = mock.Mock(credentials={"proposer": "private-proposer", "approver": "private-approver"})
    engine = approval_engine(transport)
    pending = {"proposalId": "proposal-1", "status": "Pending", "requestedBy": "actor-proposer", "diff": SEALED_DIFF}
    handle = {"operationInstanceId": "opinst-exec", "operationId": "studio.content.create-publication-request",
              "proposalId": "proposal-1", "auditId": "audit-approval", "correlationId": "00-corr-approval",
              **SEALED_HANDLE}
    transport.get_json.side_effect = [pending, pending,
        {**pending, "status": "Submitted", "resolvedBy": "actor-approver", "executionOperationId": "opinst-exec"},
        {"data": {**handle, "status": "Running"}}, {"data": handle}]
    transport.cli_approve.side_effect = [(1, None)]
    with mock.patch.object(executor.time, "sleep"):
        assert engine.approve("proposal-1")["executionOperationId"] == "opinst-exec"
    assert engine.evidence["checks"]["8"]["separation"]["status"] == "pass"


def test_human_approval_channel_is_the_local_https_endpoint_with_the_bootstrap_password(
        tmp_path, monkeypatch, unpatched_human_approval_channel):
    channel = unpatched_human_approval_channel
    monkeypatch.delenv("HONUA_JOURNEY_HTTPS_PORT", raising=False)
    monkeypatch.delenv("HONUA_ADMIN_PASSWORD", raising=False)
    assert channel(copy.deepcopy(LOCAL_TARGET), tmp_path) == {
        "baseUrl": "https://localhost:8139", "password": "honua-console-dev-key", "cafile": str(tmp_path / "tls" / "tls.crt")}
    no_https = copy.deepcopy(LOCAL_TARGET)
    del no_https["compose"]["httpsPort"]
    assert channel(no_https, tmp_path) is None
    assert channel({**copy.deepcopy(LOCAL_TARGET), "kind": "aws-ecs"}, tmp_path) is None


def test_style_proposal_names_the_service_by_its_catalog_metadata_id(human_approver):
    canonical = {**STYLE_PROPOSAL, "summary": "Apply style 'journey-solid-red' on service 'svc-4f1c2a', layer '0'."}
    engine = style_engine(canonical, canonical, {**canonical, "status": "Succeeded", "resolvedBy": HUMAN_ACTOR})
    apply_style(engine)
    assert engine.evidence["checks"]["4"]["style-applied"]["status"] == "pass"
    assert human_approver.call_count == 1


def test_proposal_ids_share_the_one_bounded_token_validator():
    import identities
    assert transport_module.TOKEN is identities.TOKEN
    assert not hasattr(transport_module, "PROPOSAL_ID")


def _journey_calls():
    import discovery
    return {(row["surface"], row["name"]) for row in discovery.calls()}


def test_human_session_admin_calls_enter_the_roster_inventory(human_approver):
    import discovery
    discovery.reset_calls()
    engine = style_engine(STYLE_PROPOSAL, STYLE_PROPOSAL, {**STYLE_PROPOSAL, "status": "Succeeded", "resolvedBy": HUMAN_ACTOR})
    apply_style(engine)
    calls = _journey_calls()
    assert {("rest", "GET /api/v1/admin/auth/csrf"), ("rest", "POST /api/v1/admin/auth/session"),
            ("rest", "POST /api/v1/admin/proposals/proposal-style-1/approve"),
            ("human-session", "GET /api/v1/admin/proposals/proposal-style-1/approval-review")} <= calls


def test_refused_human_session_records_only_the_calls_it_reached(human_approver):
    import discovery
    discovery.reset_calls()
    human_approver.return_value = {"result": "refused", "stage": "session", "httpStatus": 401, "detail": "denied"}
    engine = style_engine(STYLE_PROPOSAL, STYLE_PROPOSAL)
    with pytest.raises(ExecutionError, match="human approval did not approve"):
        apply_style(engine)
    calls = _journey_calls()
    assert ("rest", "POST /api/v1/admin/auth/session") in calls
    assert not any("approval-review" in name for _, name in calls)


REOPEN_PROPOSAL = {
    "proposalId": "proposal-reopen-1",
    "status": "AwaitingApproval",
    "requestedBy": "bearer:subject:https%3A%2F%2Fterminal-journey.invalid:journey-author",
    "summary": "Execute studio.content.reopen-version with its accepted typed payload.",
}
REOPEN_HANDLE = {
    "data": {
        "operationInstanceId": "opinst-reopen-1",
        "operationId": "studio.content.reopen-version",
        "correlationId": "00-corr-reopen",
        "auditId": "audit-reopen-1",
        "proposalId": "proposal-reopen-1",
        "status": "Completed",
        "resourceIds": {"draftId": "draft-reopened-1", "generation": "1"},
    }
}


def test_gated_version_reopen_is_approved_by_a_person_and_binds_new_draft(human_approver):
    transport = mock.Mock(credentials={"proposer": "private-proposer", "operator": "private-operator"})
    observation = stages.Observation(setup_view_present=True, setup_discovery={
        "tools": [{"name": "honua_studio_reopen_version", "inputSchema": {"type": "object"}}],
        "metadata": {"view": "setup"}})
    execution = copy.deepcopy(LOCAL_TARGET["execution"])
    engine = executor.JourneyExecutor({"workspaceId": "workspace"}, {"execution": execution}, observation, transport)
    engine.resources.update(layerId=7, itemId="item-1", versionId="version-1", draftId="draft-old")
    transport.tool.return_value = {"structuredContent": {
        "operation": {"operationId": "studio.content.reopen-version", "operationInstanceId": "opinst-reopen-1",
                      "correlationId": "00-corr-reopen", "auditId": "audit-reopen-1",
                      "proposalId": "proposal-reopen-1", "status": "RequiresApproval"},
        "draftId": None}}
    reopened_draft = {"data": {"draftId": "draft-reopened-1", "baseVersionId": "version-1",
                               "envelope": {"body": engine.expected_map_body()}}}
    transport.get_json.side_effect = [
        copy.deepcopy(REOPEN_PROPOSAL), copy.deepcopy(REOPEN_PROPOSAL),
        {**REOPEN_PROPOSAL, "status": "Succeeded", "resolvedBy": HUMAN_ACTOR},
        copy.deepcopy(REOPEN_HANDLE), reopened_draft]
    engine.execute(6, {"kind": "tool_call", "tool": "honua_studio_reopen_version",
                       "arguments": {"itemId": "item-1", "versionId": "version-1"}})
    transport.http.assert_called_once_with("POST", "/api/v1/admin/proposals/proposal-reopen-1/approve",
                                           principal="proposer", expected=(403,))
    assert human_approver.call_args.args[1] == "proposal-reopen-1"
    assert engine.resources["draftId"] == "draft-reopened-1"
    assert engine.evidence["checks"]["6"]["reopened-map"]["status"] == "pass"
    assert engine.evidence["operatorApprovals"]["6"]["resolvedBy"] == HUMAN_ACTOR
    assert "publicationOperation" not in engine.evidence


def test_local_docker_reopen_and_features_scope_author_tenant(human_approver):
    transport = mock.Mock(credentials={"proposer": "private-proposer", "operator": "private-operator"})
    observation = stages.Observation(setup_view_present=True, setup_discovery={
        "tools": [{"name": "honua_studio_reopen_version", "inputSchema": {"type": "object"}}],
        "metadata": {"view": "setup"}})
    engine = executor.JourneyExecutor({"workspaceId": "workspace"}, copy.deepcopy(LOCAL_TARGET), observation, transport)
    engine.resources.update(layerId=7, itemId="item-1", versionId="version-1", draftId="draft-old")
    transport.tool.return_value = {"structuredContent": {
        "operation": {"operationId": "studio.content.reopen-version", "operationInstanceId": "opinst-reopen-1",
                      "correlationId": "00-corr-reopen", "auditId": "audit-reopen-1",
                      "proposalId": "proposal-reopen-1", "status": "RequiresApproval"},
        "draftId": None}}
    reopened_draft = {"data": {"draftId": "draft-reopened-1", "baseVersionId": "version-1",
                               "envelope": {"body": engine.expected_map_body()}}}
    transport.get_json.side_effect = [
        copy.deepcopy(REOPEN_PROPOSAL), copy.deepcopy(REOPEN_PROPOSAL),
        {**REOPEN_PROPOSAL, "status": "Succeeded", "resolvedBy": HUMAN_ACTOR},
        copy.deepcopy(REOPEN_HANDLE), reopened_draft,
        {"type": "FeatureCollection", "features": LOCAL_TARGET["execution"]["features"]}]
    engine.execute(6, {"kind": "tool_call", "tool": "honua_studio_reopen_version",
                       "arguments": {"itemId": "item-1", "versionId": "version-1"}})
    assert human_approver.call_args.args[3] == gated_approval.human_approval.ApprovalOptions(
        cafile="/run/tls.crt", tenant="public")
    row = engine.check_features()
    assert row.status == "pass"
    assert transport.get_json.call_args.kwargs == {
        "principal": "operator", "extra_headers": {"X-Honua-Tenant": "public"}}
