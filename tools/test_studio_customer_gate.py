"""The customer gate cannot admit another image, skipped cases or a partial journey."""
from copy import deepcopy
import pytest
import studio_customer_gate as g


def pins():
    return {name: {"image": f"ghcr.io/honua-io/{name}:candidate", "digest": "sha256:" + char * 64,
                   "sha": char * 40} for name, char in [("honua-console", "a"), ("honua-server", "b")]}


def evidence():
    selected = g.resolve_pins({"components": pins()})
    runtime = {name: {"requested": value["image"], "sourceRevision": value["sha"]}
               for name, value in selected.items()}
    runtime["suiteSourceRevision"] = selected["console"]["sha"]
    report = {"stats": {"expected": 2, "unexpected": 0, "flaky": 0, "skipped": 0}}
    return selected, runtime, report, {"qualificationComplete": True, "completed": list(g.REQUIRED_STAGES)}


def test_exact_images_and_complete_customer_evidence_pass():
    g.validate_evidence(*evidence())


@pytest.mark.parametrize("component", ["console", "server"])
def test_another_digest_or_source_is_rejected(component):
    for key in ["requested", "sourceRevision"]:
        values = deepcopy(evidence())
        values[1][component][key] = "wrong"
        with pytest.raises(ValueError):
            g.validate_evidence(*values)


@pytest.mark.parametrize("field", ["unexpected", "flaky", "skipped"])
def test_nonpassing_tests_are_rejected(field):
    values = evidence()
    values[2]["stats"][field] = 1
    with pytest.raises(ValueError):
        g.validate_evidence(*values)


def test_partial_or_no_customer_journey_is_rejected():
    for progress in [{}, {"qualificationComplete": False}, {"qualificationComplete": True, "completed": []}]:
        values = evidence()
        with pytest.raises(ValueError):
            g.validate_evidence(*values[:3], progress)


def test_previous_non_admin_receipt_cannot_replace_the_publisher_journey():
    values = evidence()
    completed = set(g.REQUIRED_STAGES) - {"publisher-auth"}
    completed.add("non-admin-auth")
    with pytest.raises(ValueError, match="partial"):
        g.validate_evidence(*values[:3], {"qualificationComplete": True, "completed": list(completed)})


def test_publication_without_separate_approval_is_rejected():
    values = evidence()
    completed = g.REQUIRED_STAGES - {"separate-approval"}
    with pytest.raises(ValueError, match="partial"):
        g.validate_evidence(*values[:3], {"qualificationComplete": True, "completed": list(completed)})


def test_tag_only_or_mismatched_digest_is_rejected():
    for digest in ["nightly", "", "sha256:short"]:
        components = pins()
        components["honua-console"]["digest"] = digest
        with pytest.raises(ValueError):
            g.resolve_pins({"components": components})
    components = pins()
    components["honua-console"]["image"] += "@sha256:" + "c" * 64
    with pytest.raises(ValueError):
        g.resolve_pins({"components": components})


def test_zero_tests_and_wrong_suite_revision_are_rejected():
    values = evidence()
    values[2]["stats"]["expected"] = 0
    with pytest.raises(ValueError):
        g.validate_evidence(*values)
    values = evidence()
    values[1]["suiteSourceRevision"] = "c" * 40
    with pytest.raises(ValueError):
        g.validate_evidence(*values)
