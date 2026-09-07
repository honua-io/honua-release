"""#233: the first-release introduction model and the receipt that is allowed to establish it.

Expected values here are computed independently of the tools: the receipt fixtures are written
by hand, the digests are taken with hashlib in the test, and the derived floor is asserted as a
literal. No assertion snapshots whatever the current implementation happens to print.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import server_publication_history as history
from sdk_baselines import PUBLISHER, check_component, content_digest, findings, release_context
from test_platform_lock import DIGEST, REVISION, component, valid_lock
from validate_platform_lock import validate
from verify_sdk_baseline_sources import SourceReader, verify_publication_history, verify_sources

ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = "certification/sources/server-publication-history.v1.json"
RECEIPT_URI = f"https://github.com/honua-io/honua-release/blob/{'c' * 40}/{RECEIPT_PATH}"
FIRST_RELEASE_VERSION = "1.0.0"


def receipt(**overrides):
    """A hand-written receipt asserting the publisher has published nothing at all."""
    value = {
        "schema": "honua.server-publication-history/v1",
        "issue": "honua-io/honua-release#233",
        "repository": "honua-io/honua-server",
        "observedAt": "2026-09-07T04:46:49Z",
        "observedDefaultBranch": "trunk",
        "observedDefaultBranchSha": "9" * 40,
        "sources": [
            {"api": "https://api.github.com/repos/honua-io/honua-server/tags",
             "count": 0, "complete": True},
            {"api": "https://api.github.com/repos/honua-io/honua-server/releases",
             "count": 0, "complete": True},
            {"api": "https://api.github.com/repos/honua-io/honua-server/git/refs/tags",
             "count": 0, "complete": True, "answer": "http-404-empty-ref-namespace"},
        ],
        "publishedRefs": [],
    }
    value.update(overrides)
    return value


# --- the receipt itself ---------------------------------------------------------------------

def test_accepts_only_a_complete_empty_enumeration():
    assert history.verify(receipt()) == "honua-io/honua-server"


@pytest.mark.parametrize("tag_name", ["v0.9.0", "honua-server-2026.0", "nightly"])
def test_any_prior_ref_defeats_the_first_release_model(tag_name):
    """One tag of any shape means some capability may predate the candidate."""
    value = receipt(publishedRefs=[tag_name])
    value["sources"][0]["count"] = 1
    with pytest.raises(ValueError, match="prior publication ref"):
        history.verify(value)


def test_a_ref_the_counts_hide_is_still_a_prior_publication():
    with pytest.raises(ValueError, match="prior publication ref"):
        history.verify(receipt(publishedRefs=["v0.9.0"]))


def test_unenumerated_namespace_is_not_emptiness():
    value = receipt()
    del value["sources"][1]
    with pytest.raises(ValueError, match="releases"):
        history.verify(value)


def test_partial_pagination_is_rejected():
    value = receipt()
    value["sources"][0]["complete"] = False
    with pytest.raises(ValueError, match="pagination"):
        history.verify(value)


def test_a_404_is_emptiness_only_for_the_ref_namespace():
    """`tags`/`releases` answer 200 with an empty array; a 404 there is unreadable, not empty."""
    value = receipt()
    value["sources"][0]["answer"] = "http-404-empty-ref-namespace"
    with pytest.raises(ValueError, match="not read as a complete listing"):
        history.verify(value)


@pytest.mark.parametrize("field,bad", [
    ("schema", "honua.something-else/v1"),
    ("repository", "honua-io/honua-sdk-js"),
    ("observedDefaultBranchSha", "trunk"),
    ("observedAt", "2026-09-07"),
])
def test_receipt_identity_must_be_exact(field, bad):
    with pytest.raises(ValueError):
        history.verify(receipt(**{field: bad}))


@pytest.mark.parametrize("api", [
    "https://example.invalid/repos/honua-io/honua-server/tags",
    "https://api.github.com/repos/honua-io/honua-server/tags?per_page=100",
    "https://api.github.com/repos/honua-io/honua-server-mirror/tags",
    "https://api.github.com./repos/honua-io/honua-server/tags",
])
def test_only_the_exact_github_endpoints_are_an_enumeration(api):
    """The verifier never fetches these URLs, so a lookalike host proves nothing."""
    value = receipt()
    value["sources"][0]["api"] = api
    with pytest.raises(ValueError, match="exact GitHub enumeration endpoints"):
        history.verify(value)


def test_a_namespace_read_twice_is_not_three_namespaces():
    value = receipt()
    value["sources"][1] = copy.deepcopy(value["sources"][0])
    with pytest.raises(ValueError, match="enumerated twice"):
        history.verify(value)


@pytest.mark.parametrize("count", ["100", "0", None, 1.0, True, False, [], -1])
def test_a_count_that_is_not_a_nonnegative_integer_is_unreadable_not_zero(count):
    """`"count": "100"` used to be summed as nothing at all, which reads a published
    repository as unpublished."""
    value = receipt()
    value["sources"][0]["count"] = count
    with pytest.raises(ValueError, match="must be a nonnegative integer"):
        history.verify(value)


def test_committed_receipt_is_valid_and_reports_no_publication():
    committed = json.loads((ROOT / RECEIPT_PATH).read_text(encoding="utf-8"))
    assert history.verify(committed) == "honua-io/honua-server"
    assert committed["publishedRefs"] == []
    assert {source["api"].rsplit("honua-server/", 1)[-1] for source in committed["sources"]} == {
        "tags", "releases", "git/refs/tags"}


# --- derivation -----------------------------------------------------------------------------

def first_release_component(*, cite=RECEIPT_URI, sha=DIGEST, declared=None,
                            model="first-release", floor=FIRST_RELEASE_VERSION):
    """`admin.write` is introduced by the first release; `admin.read` keeps a numeric floor."""
    item = component()
    manifest = item["serverCompatibility"]["manifests"][0]
    entry = manifest["content"]["capabilities"]["admin.write"]
    entry.pop("minimumServerVersion", None)
    if declared is not None:
        entry["minimumServerVersion"] = declared
    if model is not None:
        entry["introductionModel"] = model
    entry["evidence"] = {"uri": cite, "sha256": sha}
    manifest["sha256"] = content_digest(manifest["content"])
    item["serverCompatibility"]["minimumServerVersion"] = floor
    item["serverCompatibility"]["declarations"][0]["minimumServerVersion"] = floor
    return item


AGREES = object()


def publisher_component(*, version=FIRST_RELEASE_VERSION, pin=True, artifact=AGREES):
    """The locked publisher entry: the named first release plus the artifact that ships it."""
    if artifact is AGREES:
        artifact = version
    entry = {"artifacts": [{"kind": "image", "coordinate": "ghcr.io/honua-io/honua-server",
                            "digest": DIGEST}]}
    if artifact is not None:
        entry["artifacts"][0]["version"] = artifact
    if version is not None:
        entry["releaseVersion"] = version
    if pin:
        entry["publicationHistory"] = {
            "path": RECEIPT_PATH, "uri": RECEIPT_URI, "sha256": DIGEST}
    return entry


def context(**kwargs):
    return release_context({"components": {PUBLISHER: publisher_component(**kwargs)}})


def test_first_release_capability_resolves_to_the_first_release():
    # admin.read declares 1.0.0; admin.write resolves to the first release 1.0.0; max is 1.0.0.
    assert check_component(first_release_component(), context()) == FIRST_RELEASE_VERSION


def test_first_release_floor_participates_in_the_maximum():
    """A later first release must raise the floor above an older numeric requirement."""
    assert check_component(
        first_release_component(floor="2.5.0"), context(version="2.5.0")) == "2.5.0"


def test_first_release_model_without_a_named_release_stays_unqualified():
    """Today's state: the candidate does not exist, so nothing resolves."""
    with pytest.raises(ValueError, match="does not name"):
        check_component(first_release_component(), context(version=None))


def test_first_release_model_without_a_locked_receipt_stays_unqualified():
    with pytest.raises(ValueError, match="no honua-server publication-history receipt"):
        check_component(first_release_component(), context(pin=False))


def test_first_release_model_must_cite_the_locked_receipt():
    other = f"https://github.com/honua-io/honua-release/blob/{'d' * 40}/{RECEIPT_PATH}"
    with pytest.raises(ValueError, match="does not cite"):
        check_component(first_release_component(cite=other), context())


def test_first_release_model_cannot_smuggle_a_different_number():
    with pytest.raises(ValueError, match="declares '0.1.0'"):
        check_component(first_release_component(declared="0.1.0"), context())


def test_named_first_release_must_be_the_locked_server_artifact():
    """A publisher claim of 2.0.0 beside a 1.0.0 server artifact publishes a table for a
    server that does not ship, so the derived floor is refused, not reconciled."""
    with pytest.raises(ValueError, match=r"names first honua-server release 2\.0\.0, but the "
                                         r"locked honua-server artifact is 1\.0\.0"):
        check_component(first_release_component(floor="2.0.0"),
                        context(version="2.0.0", artifact="1.0.0"))


@pytest.mark.parametrize("artifact", [None, "pre-release"])
def test_first_release_needs_a_released_server_artifact_to_bind_to(artifact):
    """Today's lock pins a pre-release server snapshot, so nothing resolves."""
    with pytest.raises(ValueError, match="pins no released honua-server artifact version"):
        check_component(first_release_component(), context(artifact=artifact))


def test_publisher_artifact_version_reads_a_manifest_row_too():
    """generate_platform_lock reads the manifest shape, where the version is on the component."""
    from sdk_baselines import publisher_artifact_version
    assert publisher_artifact_version({"version": "1.0.0"}) == "1.0.0"
    assert publisher_artifact_version({"artifactVersion": "1.0.0", "version": "pre-release"}) == "1.0.0"
    assert publisher_artifact_version({"version": "pre-release"}) is None
    assert publisher_artifact_version({}) is None


def test_absent_model_still_requires_a_numeric_floor():
    """Removing only the model, not the floor, must remain the pre-existing unqualified error."""
    with pytest.raises(ValueError, match="no server introduction floor"):
        check_component(first_release_component(model=None), context())


def test_context_is_absent_by_default():
    with pytest.raises(ValueError, match="unqualified"):
        check_component(first_release_component())


def test_lock_without_a_publisher_release_version_reports_every_sdk():
    lock = valid_lock()
    for name in ("honua-sdk-js", "honua-sdk-dotnet", "honua-sdk-python", "geospatial-mcp"):
        lock["components"][name] = {**lock["components"][name], **first_release_component()}
    lock["components"][PUBLISHER] = publisher_component(
        version=None, artifact=FIRST_RELEASE_VERSION)
    assert len(findings(lock)) == 4
    lock["components"][PUBLISHER]["releaseVersion"] = FIRST_RELEASE_VERSION
    assert findings(lock) == []


# --- freshness: a past observation cannot prove nothing was published since -------------------

def no_publication(repository="honua-io/honua-server", *, at="2026-09-08T00:00:00Z"):
    """A live enumeration stand-in; `collect` itself is the network call under test elsewhere."""
    return receipt(repository=repository, observedAt=at, observedDefaultBranchSha="a" * 40)


def test_a_stale_receipt_is_requalified_by_a_live_enumeration():
    fresh = history.confirm_current(receipt(), collector=lambda repo: no_publication(repo))
    assert fresh["observedAt"] == "2026-09-08T00:00:00Z"


def test_a_publication_after_the_receipt_withdraws_the_model():
    """The committed 2026-09-07 receipt stays syntactically valid forever; the live reading
    is what refuses once the publisher ships a tag."""
    published = no_publication()
    published["sources"][0]["count"] = 1
    published["publishedRefs"] = ["v1.0.0"]
    assert history.verify(receipt()) == "honua-io/honua-server"
    with pytest.raises(ValueError, match="prior publication ref"):
        history.confirm_current(receipt(), collector=lambda repo: published)


def test_a_live_reading_older_than_the_pin_is_refused():
    with pytest.raises(ValueError, match="observation clock is unreliable"):
        history.confirm_current(receipt(observedAt="2026-09-09T00:00:00Z"),
                                collector=lambda repo: no_publication())


def test_offline_verification_cannot_qualify_the_first_release_model(tmp_path):
    from verify_sdk_baseline_sources import offline_collector
    lock, _ = lock_with_history(tmp_path, receipt())
    with pytest.raises(ValueError, match="offline run cannot prove"):
        verify_publication_history(lock, tmp_path, offline_collector)


# --- the lock pin must match the committed bytes ---------------------------------------------

def lock_with_history(tmp_path, body, *, sha256=None, path=RECEIPT_PATH):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(body, indent=2).encode() + b"\n"
    target.write_bytes(raw)
    return {"components": {PUBLISHER: {"publicationHistory": {
        "path": path, "uri": RECEIPT_URI,
        "sha256": sha256 or "sha256:" + hashlib.sha256(raw).hexdigest()}}}}, raw


def test_publication_history_pin_binds_the_committed_bytes(tmp_path):
    lock, raw = lock_with_history(tmp_path, receipt())
    verify_publication_history(lock, tmp_path, no_publication)
    # Independently recomputed: flipping one byte of the receipt must break the pin.
    (tmp_path / RECEIPT_PATH).write_bytes(raw.replace(b"trunk", b"main"))
    with pytest.raises(ValueError, match="bytes disagree"):
        verify_publication_history(lock, tmp_path, no_publication)


def test_publication_history_pin_rejects_a_receipt_that_reports_a_tag(tmp_path):
    lock, _ = lock_with_history(tmp_path, receipt(publishedRefs=["v0.9.0"]))
    with pytest.raises(ValueError, match="prior publication ref"):
        verify_publication_history(lock, tmp_path, no_publication)


def test_publication_history_pin_rejects_a_missing_receipt(tmp_path):
    lock, _ = lock_with_history(tmp_path, receipt())
    (tmp_path / RECEIPT_PATH).unlink()
    with pytest.raises(ValueError, match="missing"):
        verify_publication_history(lock, tmp_path, no_publication)


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.json", "a/../../outside.json"])
def test_publication_history_pin_cannot_escape_the_repository(tmp_path, path):
    lock = {"components": {PUBLISHER: {"publicationHistory": {
        "path": path, "uri": RECEIPT_URI, "sha256": DIGEST}}}}
    with pytest.raises(ValueError, match="relative repository path"):
        verify_publication_history(lock, tmp_path, no_publication)


def test_source_verification_runs_the_publication_history_check(tmp_path):
    lock = valid_lock()
    for name in ("honua-sdk-js", "honua-sdk-dotnet", "honua-sdk-python", "geospatial-mcp"):
        lock["components"][name] = {**lock["components"][name], **first_release_component()}
    pinned, _ = lock_with_history(tmp_path, receipt(publishedRefs=["v0.9.0"]))
    pin = copy.deepcopy(pinned["components"][PUBLISHER]["publicationHistory"])
    for name in ("honua-sdk-js", "honua-sdk-dotnet", "honua-sdk-python", "geospatial-mcp"):
        lock["components"][name] = {**lock["components"][name],
                                    **first_release_component(sha=pin["sha256"])}
    lock["components"][PUBLISHER] = {**publisher_component(), "publicationHistory": pin}
    with pytest.raises(ValueError, match="prior publication ref"):
        verify_sources(lock, SourceReader(tmp_path), tmp_path, collector=no_publication)


# --- the lock schema must admit the fields the generator writes ------------------------------

def schema_errors(publisher):
    """Schema findings for the publisher component only; artifact rules are checked elsewhere."""
    lock = valid_lock()
    lock["components"][PUBLISHER] = {
        "source": {"repository": f"https://github.com/honua-io/{PUBLISHER}", "revision": REVISION},
        "lifecycleStatus": "GA", "supportTier": "ga", "artifactIdentityModel": "source-pinned",
        "contractVersions": {"admin": "v1"}, "schemaVersions": {}, "artifacts": [], **publisher}
    return [error for error in validate(lock).errors
            if f"components.{PUBLISHER}" in error and "artifacts[" not in error]


def test_lock_schema_admits_the_first_release_fields():
    """`$defs.component` sets additionalProperties:false, so a schema that does not name these
    rejects every lock the first-release model can be used in."""
    assert schema_errors({}) == []
    assert schema_errors({"releaseVersion": FIRST_RELEASE_VERSION, "publicationHistory": {
        "path": RECEIPT_PATH, "uri": RECEIPT_URI, "sha256": DIGEST}}) == []


@pytest.mark.parametrize("pin,expected", [
    ({"path": RECEIPT_PATH, "uri": "http://insecure/x", "sha256": DIGEST}, "^https://"),
    ({"path": "/etc/passwd", "uri": RECEIPT_URI, "sha256": DIGEST}, "path"),
    ({"path": RECEIPT_PATH, "uri": RECEIPT_URI, "sha256": "deadbeef"}, "sha256:"),
    ({"path": RECEIPT_PATH, "uri": RECEIPT_URI, "sha256": DIGEST, "extra": 1},
     "Additional properties"),
    ({"path": RECEIPT_PATH}, "required property"),
])
def test_lock_schema_constrains_the_publication_history_pin(pin, expected):
    errors = schema_errors({"releaseVersion": FIRST_RELEASE_VERSION, "publicationHistory": pin})
    assert errors and any(expected in error for error in errors), errors


def test_lock_schema_still_refuses_an_unknown_component_property():
    assert any("Additional properties" in error for error in schema_errors({"bogus": 1}))
