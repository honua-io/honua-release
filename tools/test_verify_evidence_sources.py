from __future__ import annotations

import base64

import pytest

import verify_evidence_sources as ves


class FakeClient:
    def __init__(self, *, comparison: str = "behind", events: str = "push:\n  branches: [trunk]\n"):
        self.comparison = comparison
        self.events = events

    def json(self, path: str) -> dict:
        if "/commits/" in path:
            return {"sha": "a" * 40}
        if "/compare/" in path:
            return {"status": self.comparison}
        if "/contents/" in path:
            indented_events = "\n".join(f"  {line}" for line in self.events.splitlines())
            workflow = f"name: Evidence\non:\n{indented_events}\njobs: {{}}\n".encode()
            return {"encoding": "base64", "content": base64.b64encode(workflow).decode()}
        raise AssertionError(path)


SOURCE = {
    "repository": "honua-io/producer",
    "producerSha": "a" * 40,
    "trustedBranch": "trunk",
    "workflowPath": ".github/workflows/evidence.yml",
    "trustedEvents": ["push"],
    "artifactIdentity": "evidence-bundle",
}


def test_required_producer_must_be_on_branch_and_declare_trusted_events():
    identity = ves.verify_source("producer", SOURCE, FakeClient())
    assert identity.startswith("honua-io/producer@" + "a" * 40)


def test_diverged_producer_is_rejected():
    with pytest.raises(ves.VerificationError, match="not on trusted branch"):
        ves.verify_source("producer", SOURCE, FakeClient(comparison="diverged"))


def test_undeclared_trusted_event_is_rejected():
    with pytest.raises(ves.VerificationError, match="does not declare trusted event"):
        ves.verify_source("producer", SOURCE, FakeClient(events="schedule:\n  - cron: '0 0 * * *'\n"))


@pytest.mark.parametrize("events", [
    "push:\n  branches: [release/**]\n",
    "push:\n  branches-ignore: [trunk]\n",
    "push:\n  branches: ['*', '!trunk']\n",
])
def test_trusted_event_must_allow_the_trusted_branch(events):
    with pytest.raises(ves.VerificationError, match="cannot run for trusted branch 'trunk'"):
        ves.verify_source("producer", SOURCE, FakeClient(events=events))


def test_ordered_negative_branch_filter_can_reinclude_trusted_branch():
    events = "push:\n  branches: ['*', '!release/**', trunk]\n"
    identity = ves.verify_source("producer", SOURCE, FakeClient(events=events))
    assert identity.startswith("honua-io/producer@")


def test_verify_manifest_checks_non_evidence_pins_too():
    manifest = {
        "components": {"honua-server": {"sha": "a" * 40}},
        "evidenceSources": {"producer": SOURCE},
        "protocolCertification": {"ledger": {"requirementsSourceRevision": "pending"}},
    }
    verified = ves.verify_manifest(manifest, FakeClient())
    assert len(verified) == 1
