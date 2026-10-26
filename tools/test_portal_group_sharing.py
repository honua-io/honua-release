"""Checker controls for durable group-sharing readback.

A complete synthetic document stays nonpass. It is not an exact-candidate receipt.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portal_group_sharing as portal


def _view(visible):
    return {name: visible for name in portal.SURFACES}


def _document():
    shared = {"member": _view(True), "nonmember": _view(False)}
    hidden = {"member": _view(False), "nonmember": _view(False)}
    return {
        "schema": portal.SCHEMA,
        "candidateSourceSha": "a" * 40,
        "candidateImage": "ghcr.io/example/server@sha256:" + "b" * 64,
        "identities": {
            "owner": "owner-1",
            "member": "member-1",
            "nonmember": "nonmember-1",
            "group": "group-1",
            "item": "item-1",
        },
        "runtime": {
            "beforeRestart": "process-before",
            "afterRestart": "process-after",
            "replica": "process-replica",
            "store": "store-1",
        },
        "notSharedWith": ["everyone", "org"],
        "publicCallerVisibilityUnchanged": True,
        "policyRefusals": {name: "refused" for name in portal.POLICY_REFUSALS},
        "phases": {
            "shared": shared,
            "sharedAfterRestart": copy.deepcopy(shared),
            "sharedOnReplica": copy.deepcopy(shared),
            "unshared": hidden,
            "unsharedAfterRestart": copy.deepcopy(hidden),
        },
    }


def test_absent_observations_are_nonpass_and_a_complete_shape_does_not_qualify():
    assert portal.qualify(None) == {"status": "nonpass", "reason": "no-retained-observations"}
    verdict = portal.qualify(_document())
    assert verdict["status"] == "nonpass"
    assert verdict["reason"] == "exact-candidate-execution-not-claimed"
    assert verdict["retainedShape"] is True
    assert "ece4c3a2f68557fe8329e9932de36193d0732894" in portal.CORRECTION
    assert "4bd08872fb47429bd4623e8477c9761b083cf25e" in portal.CORRECTION
    assert "not claimed" in portal.CORRECTION


def test_inconsistent_readback_and_grants_are_refused():
    cases = {
        "schema": lambda doc: doc.pop("schema"),
        "everyone-or-org-granted": lambda doc: doc.__setitem__("publicCallerVisibilityUnchanged", False),
        "policy-refusal": lambda doc: doc["policyRefusals"].__setitem__("tenant", "granted"),
        "nonmember-readback": lambda doc: doc["phases"]["shared"]["nonmember"].__setitem__("feature", True),
        "unshare-readback": lambda doc: doc["phases"]["unshared"]["member"].__setitem__("item", True),
        "restart-readback": lambda doc: doc["phases"]["sharedAfterRestart"]["member"].__setitem__("search", False),
        "replica-readback": lambda doc: doc["phases"]["sharedOnReplica"]["member"].__setitem__("groupContent", False),
        "restart-revocation-readback": lambda doc: doc["phases"]["unsharedAfterRestart"]["member"].__setitem__("feature", True),
    }
    for reason, mutate in cases.items():
        document = _document()
        mutate(document)
        assert portal.qualify(document)["reason"] == reason


def test_a_complete_document_passes_only_when_bound_to_the_candidate_it_names():
    document = _document()
    candidate = {"sourceSha": document["candidateSourceSha"], "image": document["candidateImage"]}
    verdict = portal.qualify(document, candidate=candidate)
    assert verdict["status"] == "pass"
    assert verdict["candidateSourceSha"] == "a" * 40
    for other in ({"sourceSha": "c" * 40, "image": candidate["image"]},
                  {"sourceSha": candidate["sourceSha"], "image": "ghcr.io/example/server@sha256:" + "d" * 64},
                  {}, "not-a-candidate"):
        assert portal.qualify(document, candidate=other) == {"status": "refused", "reason": "candidate-mismatch"}
    # Binding never rescues an incomplete or wrong observation.
    broken = copy.deepcopy(document)
    broken["phases"]["unsharedAfterRestart"]["member"]["feature"] = True
    assert portal.qualify(broken, candidate=candidate)["status"] == "refused"
    assert portal.qualify(None, candidate=candidate)["status"] == "nonpass"


def test_the_ledger_binding_names_the_candidate_by_source_and_image_digest():
    document = _document()
    digest = document["candidateImage"].rsplit("@", 1)[1]
    ledger = {"sourceSha": document["candidateSourceSha"], "imageDigest": digest}
    assert portal.qualify(document, candidate=ledger)["status"] == "pass"
    for other in ({**ledger, "imageDigest": "sha256:" + "d" * 64}, {**ledger, "imageDigest": "b" * 64},
                  {**ledger, "imageDigest": None}, {"sourceSha": ledger["sourceSha"]},
                  {**ledger, "image": "ghcr.io/example/other@" + digest}):
        assert portal.qualify(document, candidate=other) == {"status": "refused", "reason": "candidate-mismatch"}
