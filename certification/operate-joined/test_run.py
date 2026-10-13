"""Fail-closed controls for the real joined proof producer."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("joined_proof", Path(__file__).with_name("run.py"))
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


@pytest.mark.parametrize("prefix", ["", "devops191", "operate3302-*", "operate3302-../../", "operate3302-12345678"])
def test_cleanup_refuses_unowned_namespaces(prefix):
    with pytest.raises(ValueError):
        proof.owned_prefix(prefix)


@pytest.mark.parametrize("text", ["missing", "anchor anchor"])
def test_changed_fixture_anchor_refused(text):
    with pytest.raises(ValueError):
        proof.replace_once(text, "anchor", "replacement")


def test_proposal_cannot_already_have_actuated():
    value = {"findingId": "finding", "kind": "Deploy", "status": "AwaitingApproval", "requestedBy": "proposer", "executionOperationId": "already-running"}
    with pytest.raises(AssertionError):
        proof.assert_pending(value, "finding")


@pytest.mark.parametrize("finish,status", [("2026-10-13T00:00:44Z", "Succeeded"), ("2026-10-13T00:00:50Z", "Reconciling")])
def test_success_requires_elapsed_full_window(finish, status):
    observed = {"operationId": "operation", "protection": {"firstExposureAt": "2026-10-13T00:00:00Z", "observationDeadline": "2026-10-13T00:00:45Z"}}
    with pytest.raises(AssertionError):
        proof.assert_completed_window(observed, {"operationId": "operation", "status": status, "completedAt": finish})


def test_completed_exact_window_accepted():
    observed = {"operationId": "operation", "protection": {"firstExposureAt": "2026-10-13T00:00:00Z", "observationDeadline": "2026-10-13T00:00:45Z"}}
    proof.assert_completed_window(observed, {"operationId": "operation", "status": "Succeeded", "completedAt": "2026-10-13T00:00:45Z"})
