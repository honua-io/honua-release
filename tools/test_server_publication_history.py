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


def context(*, version=FIRST_RELEASE_VERSION, pin=True):
    publisher = {}
    if version is not None:
        publisher["releaseVersion"] = version
    if pin:
        publisher["publicationHistory"] = {
            "path": RECEIPT_PATH, "uri": RECEIPT_URI, "sha256": DIGEST}
    return release_context({"components": {PUBLISHER: publisher}})


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
    lock["components"][PUBLISHER] = {"publicationHistory": {
        "path": RECEIPT_PATH, "uri": RECEIPT_URI, "sha256": DIGEST}}
    assert len(findings(lock)) == 4
    lock["components"][PUBLISHER]["releaseVersion"] = FIRST_RELEASE_VERSION
    assert findings(lock) == []


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
    verify_publication_history(lock, tmp_path)
    # Independently recomputed: flipping one byte of the receipt must break the pin.
    (tmp_path / RECEIPT_PATH).write_bytes(raw.replace(b"trunk", b"main"))
    with pytest.raises(ValueError, match="bytes disagree"):
        verify_publication_history(lock, tmp_path)


def test_publication_history_pin_rejects_a_receipt_that_reports_a_tag(tmp_path):
    lock, _ = lock_with_history(tmp_path, receipt(publishedRefs=["v0.9.0"]))
    with pytest.raises(ValueError, match="prior publication ref"):
        verify_publication_history(lock, tmp_path)


def test_publication_history_pin_rejects_a_missing_receipt(tmp_path):
    lock, _ = lock_with_history(tmp_path, receipt())
    (tmp_path / RECEIPT_PATH).unlink()
    with pytest.raises(ValueError, match="missing"):
        verify_publication_history(lock, tmp_path)


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.json", "a/../../outside.json"])
def test_publication_history_pin_cannot_escape_the_repository(tmp_path, path):
    lock = {"components": {PUBLISHER: {"publicationHistory": {
        "path": path, "uri": RECEIPT_URI, "sha256": DIGEST}}}}
    with pytest.raises(ValueError, match="relative repository path"):
        verify_publication_history(lock, tmp_path)


def test_source_verification_runs_the_publication_history_check(tmp_path):
    lock = valid_lock()
    for name in ("honua-sdk-js", "honua-sdk-dotnet", "honua-sdk-python", "geospatial-mcp"):
        lock["components"][name] = {**lock["components"][name], **first_release_component()}
    pinned, _ = lock_with_history(tmp_path, receipt(publishedRefs=["v0.9.0"]))
    pin = copy.deepcopy(pinned["components"][PUBLISHER]["publicationHistory"])
    for name in ("honua-sdk-js", "honua-sdk-dotnet", "honua-sdk-python", "geospatial-mcp"):
        lock["components"][name] = {**lock["components"][name],
                                    **first_release_component(sha=pin["sha256"])}
    lock["components"][PUBLISHER] = {"publicationHistory": pin,
                                     "releaseVersion": FIRST_RELEASE_VERSION}
    with pytest.raises(ValueError, match="prior publication ref"):
        verify_sources(lock, SourceReader(tmp_path), tmp_path)
