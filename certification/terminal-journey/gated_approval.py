"""Operator calls the candidate files as proposals, approved by a person over the human session."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import local_fixture
from identities import _token, identity, principal_identity
from transport import PROPOSAL_URI, ExecutionError, credential_values, describe_fields, masked_fields

sys.path.append(str(Path(__file__).resolve().parents[2] / "e2e" / "harness" / "lib"))
import human_approval  # noqa: E402

# Proposal states: awaiting a decision, and resolved by an approval. An approved service.publish whose
# replay materializes asynchronously stays Submitted until its handle completes.
PROPOSAL_PENDING = {"Pending", "AwaitingApproval"}
PROPOSAL_APPROVED = {"Succeeded", "Approved", "Applied", "Submitted"}
PROPOSAL_RESOLVED = PROPOSAL_APPROVED | {"Rejected", "Failed"}
HANDLE_IN_FLIGHT = {"Queued", "Running", "RequiresApproval"}
# Proposal fields an operator's refused approval must leave untouched.
PROPOSAL_STATE = ("proposalId", "status", "version", "planHash", "summary", "resolvedBy", "executionOperationId")
HANDLE_IDS = ("operationId", "operationInstanceId", "correlationId", "auditId")


@dataclass(frozen=True)
class GatedCall:
    """An operator call the server filed as a proposal: its stage, proposal, replay instance and summary."""
    stage: int
    proposal_id: str
    instance_id: str
    summary: str


class GatedApprovals:
    """Approval steps shared by the executor's stages; needs principal, poll, view, transport and evidence."""

    def approve_publication(self, submitted, arguments):
        """Service publication is the human approval point: the operator proposes, a person approves.

        The approved replay reuses the submitted operation instance, so its completed handle is the
        publication's identity.
        """
        gate = GatedCall(3, identity(submitted.get("proposalId"), "honua_publish_service"),
                         identity(submitted.get("operationInstanceId"), "honua_publish_service"),
                         f"Publish '{arguments.get('schema')}.{arguments.get('table')}' "
                         f"as layer '{arguments.get('layerName')}'.")
        handle = self.approve_gated(gate)
        if handle.get("operationId") != "service.publish":
            raise ExecutionError("GET approved publication handle", "approved replay is not the service publication")
        # The handle names no URI; honua_publish_service derives it from the service name the same way.
        self.evidence["operatorApprovals"]["3"]["serviceUriSource"] = "derived-from-handle"
        return self.project_publication(handle)

    def approve_style(self, proposal_id, arguments):
        """An operator's style application on an agent transport is gated the same way.

        The handle names no style, so the style is bound by the proposal a person approved.
        """
        gate = GatedCall(4, proposal_id, self.proposal_operation_instance(proposal_id),
                         f"Apply style '{arguments.get('styleId')}' on service '{arguments.get('serviceId')}', "
                         f"layer '{arguments.get('serviceLayerIndex')}'.")
        handle = self.approve_gated(gate)
        if handle.get("operationId") != "style.apply-preset":
            raise ExecutionError("GET approved style handle", "approved replay is not the style application")
        return {"status": handle.get("status"), "applied": handle.get("status") == "Completed",
                "styleId": arguments.get("styleId"), **{key: handle.get(key) for key in HANDLE_IDS}}

    def approve_gated(self, gate):
        """A gated operator call: a person approves the exact proposal after the operator is refused.

        The proposal must name the gated call before anyone approves it, and the operator's refused
        attempt must leave it untouched. Returns the approved replay's settled handle.
        """
        path = "/api/v1/admin/proposals/" + gate.proposal_id
        operator = self.principal(gate.stage)
        pending = self.pending_gated_proposal(gate, operator)
        self.transport.http("POST", path + "/approve", principal=operator, expected=(403,))
        after = self.transport.get_json(path, principal=operator)
        if any(after.get(key) != pending.get(key) for key in PROPOSAL_STATE):
            raise ExecutionError("approve as operator", "operator self-approval was not refused without mutation")
        _, resolution = self.resolve_by_human(gate.proposal_id, pending, operator)
        handle = self.settled_handle(gate.instance_id, operator)
        if handle.get("proposalId") not in {None, gate.proposal_id}:
            raise ExecutionError("GET approved operation handle", "approved replay handle does not join the proposal")
        self.evidence.setdefault("operatorApprovals", {})[str(gate.stage)] = {
            **resolution, "operatorSelfApproval": "denied"}
        return handle

    def pending_gated_proposal(self, gate, operator):
        pending = self.transport.get_json("/api/v1/admin/proposals/" + gate.proposal_id, principal=operator)
        if (pending.get("proposalId") != gate.proposal_id or pending.get("status") not in PROPOSAL_PENDING
                or pending.get("resolvedBy")):
            raise ExecutionError("GET gated proposal", "gated operator call did not persist awaiting approval")
        if pending.get("summary") != gate.summary:
            raise ExecutionError("GET gated proposal", "gated proposal does not describe the operator's call")
        return pending

    def settled_handle(self, instance_id, principal):
        handle = self.poll("GET approved operation handle", lambda: self.operation_handle(instance_id, principal),
                           lambda value: value.get("status") not in HANDLE_IN_FLIGHT)
        if handle.get("operationInstanceId") != instance_id:
            raise ExecutionError("GET approved operation handle", "approved replay handle does not join the proposal")
        return handle

    def proposal_operation_instance(self, proposal_id):
        """The operation instance a gated proposal will replay, from its MCP resume resource."""
        command = "read proposal resource"
        result = self.transport.resource_read(PROPOSAL_URI + proposal_id, self.view(), self.principal(4))
        try:
            document = json.loads(result["contents"][0]["text"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ExecutionError(command, "candidate proposal resource omitted its content") from exc
        if not isinstance(document, dict) or document.get("proposalId") != proposal_id:
            raise ExecutionError(command, "candidate proposal resource names another proposal")
        return identity(document.get("operationInstanceId"), command)

    @staticmethod
    def project_publication(handle):
        """The fields honua_publish_service projects from a completed handle (McpPublishServiceOutput)."""
        published = {**(handle.get("resourceIds") or {}), **((handle.get("result") or {}).get("details") or {})}
        service = published.get("serviceName")
        return {"status": handle.get("status"), "requiresApproval": False,
                **{key: handle.get(key) for key in HANDLE_IDS},
                "serviceName": service,
                "serviceUri": ("honua://published-services/" + urllib.parse.quote(service, safe="")
                               if isinstance(service, str) else None),
                **{key: published.get(key) for key in ("storageLayerId", "serviceLayerIndex")}}

    def operation_handle(self, instance_id, principal):
        handle = self.transport.get_json("/api/v1/operations/handles/" + instance_id, principal=principal)
        return handle.get("data", handle)

    def resolve_by_human(self, proposal_id, before, reader):
        """Approve through the human session and require a resolver distinct from the proposer."""
        path = "/api/v1/admin/proposals/" + proposal_id
        outcome = self.human_approve(proposal_id)
        final = self.poll("GET approved proposal", lambda: self.transport.get_json(path, principal=reader),
                          lambda value: value.get("status") in PROPOSAL_RESOLVED)
        if (final.get("proposalId") != proposal_id or final.get("status") not in PROPOSAL_APPROVED
                or final.get("requestedBy") != before.get("requestedBy")
                or not final.get("resolvedBy") or final["resolvedBy"] == final.get("requestedBy")):
            raise ExecutionError("GET approved proposal", "candidate did not prove separate principal resolution")
        return final, {"proposalId": proposal_id,
                       "requestedBy": principal_identity(final["requestedBy"], "GET approved proposal", "requestedBy"),
                       "resolvedBy": principal_identity(final["resolvedBy"], "GET approved proposal", "resolvedBy"),
                       "approvalChannel": _token(outcome.get("approvalChannel"))}

    def human_approve(self, proposal_id):
        """Approve as a person does in Console: a password session over HTTPS, in a child process."""
        command = "human approval session"
        channel = local_fixture.human_approval_channel(self.target, self.transport.workdir)
        if channel is None:
            raise ExecutionError(command, "target exposes no HTTPS human approval channel", blocked=True)
        try:
            outcome = human_approval.approve_in_child(channel["baseUrl"], proposal_id, channel["password"],
                                                      channel["cafile"])
        except subprocess.TimeoutExpired as exc:
            raise ExecutionError(command, "human approval session did not finish within its budget") from exc
        if outcome.get("result") != "approved":
            seen = masked_fields({key: outcome.get(key) for key in ("result", "stage", "httpStatus", "detail")},
                                 [*credential_values(self.transport.credentials), channel["password"]])
            raise ExecutionError(command, "human approval did not approve the proposal (" + describe_fields(seen) + ")")
        return outcome
