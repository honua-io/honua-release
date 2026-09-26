"""#233: the first-release introduction model and the receipt that is allowed to establish it.

Expected values here are computed independently of the tools: the receipt fixtures are written
by hand, the digests are taken with hashlib in the test, and the derived floor is asserted as a
literal. No assertion snapshots whatever the current implementation happens to print.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import jsonschema
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


def server_image(version):
    return {"kind": "image", "coordinate": "ghcr.io/honua-io/honua-server", "version": version,
            "sourceRevision": REVISION, "digest": "sha256:" + "e" * 64,
            "platformDigests": {"amd64": "sha256:" + "e" * 64}, "architectures": ["amd64"]}


def publisher(*, version=FIRST_RELEASE_VERSION, pin=True, sha=DIGEST,
              artifact_version=..., max_age_days=14):
    entry = {}
    if version is not None:
        entry["releaseVersion"] = version
    if pin:
        entry["publicationHistory"] = {"path": RECEIPT_PATH, "uri": RECEIPT_URI, "sha256": sha,
                                       "maxAgeDays": max_age_days}
    shipped = version if artifact_version is ... else artifact_version
    entry["artifacts"] = ([server_image(shipped)] if shipped is not None else [])
    return entry


def context(**kwargs):
    return release_context({"components": {PUBLISHER: publisher(**kwargs)}})


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
    lock["components"][PUBLISHER] = publisher(version=None, artifact_version=None)
    assert len(findings(lock)) == 4
    lock["components"][PUBLISHER] = publisher()
    assert findings(lock) == []


def test_named_release_must_be_the_artifact_that_ships():
    """A releaseVersion beside a differently versioned server artifact publishes a phantom floor."""
    with pytest.raises(ValueError, match="no locked publisher artifact declares"):
        check_component(first_release_component(), context(artifact_version="2.0.0"))
    with pytest.raises(ValueError, match="no locked publisher artifact declares"):
        check_component(first_release_component(), context(artifact_version=None))


# --- the lock pin must match the committed bytes ---------------------------------------------

def lock_with_history(tmp_path, body, *, sha256=None, path=RECEIPT_PATH):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(body, indent=2).encode() + b"\n"
    target.write_bytes(raw)
    return {"components": {PUBLISHER: {"publicationHistory": {
        "path": path, "uri": RECEIPT_URI,
        "sha256": sha256 or "sha256:" + hashlib.sha256(raw).hexdigest(),
        "maxAgeDays": 3650}}}}, raw


def live(repository="honua-io/honua-server", *, at="2099-01-01T00:00:00Z", refs=()):
    """A live enumeration stand-in; `collect` itself is the network call, tested by its callers."""
    value = receipt(repository=repository, observedAt=at, observedDefaultBranchSha="a" * 40)
    if refs:
        value["sources"][0]["count"] = len(refs)
        value["publishedRefs"] = sorted(refs)
    return value


def test_publication_history_pin_binds_the_committed_bytes(tmp_path):
    lock, raw = lock_with_history(tmp_path, receipt())
    verify_publication_history(lock, tmp_path, live)
    # Independently recomputed: flipping one byte of the receipt must break the pin.
    (tmp_path / RECEIPT_PATH).write_bytes(raw.replace(b"trunk", b"main"))
    with pytest.raises(ValueError, match="bytes disagree"):
        verify_publication_history(lock, tmp_path, live)


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
    lock["components"][PUBLISHER] = publisher(sha=pin["sha256"], max_age_days=3650)
    with pytest.raises(ValueError, match="prior publication ref"):
        verify_sources(lock, SourceReader(tmp_path), tmp_path)


# --- review hardening: exact endpoints, real counts, bounded freshness, schema ------------------

def test_a_plausible_url_on_another_host_is_not_a_github_enumeration():
    """The verifier never fetches these URLs, so only the exact GitHub collections are evidence."""
    value = receipt()
    value["sources"][0]["api"] = "https://example.invalid/repos/honua-io/honua-server/tags"
    with pytest.raises(ValueError, match="not one of this publisher's GitHub publication"):
        history.verify(value)


def test_endpoint_of_another_repository_is_rejected():
    value = receipt()
    value["sources"][1]["api"] = "https://api.github.com/repos/honua-io/honua-sdk-js/releases"
    with pytest.raises(ValueError, match="not one of this publisher's GitHub publication"):
        history.verify(value)


def test_the_same_namespace_cannot_stand_in_for_a_missing_one():
    value = receipt()
    value["sources"][1]["api"] = value["sources"][0]["api"]
    with pytest.raises(ValueError, match="enumerated more than once"):
        history.verify(value)


@pytest.mark.parametrize("count", ["100", "0", None, 1.0, True, -1, [], {}])
def test_a_count_that_is_not_a_nonnegative_integer_is_rejected(count):
    """A string or null count would otherwise be summed as zero and read as emptiness."""
    value = receipt()
    value["sources"][0]["count"] = count
    with pytest.raises(ValueError, match="nonnegative integer"):
        history.verify(value)


def test_a_nonzero_integer_count_still_reports_prior_publication():
    value = receipt()
    value["sources"][0]["count"] = 100
    with pytest.raises(ValueError, match="100 prior publication ref"):
        history.verify(value)


def test_refs_must_be_strings():
    with pytest.raises(ValueError, match="list the refs"):
        history.verify(receipt(publishedRefs=[{"name": "v1"}]))


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def test_a_stale_enumeration_cannot_carry_the_first_release_premise():
    """Emptiness proven on 2026-09-07 says nothing about the repository fourteen days later."""
    value = receipt()  # observed 2026-09-07T04:46:49Z
    assert history.verify(value, now=NOW, max_age_days=30) == "honua-io/honua-server"
    with pytest.raises(ValueError, match="enumerated 14 days ago against a 7-day bound"):
        history.verify(value, now=NOW, max_age_days=7)


def test_an_observation_from_the_future_is_rejected():
    with pytest.raises(ValueError, match="observed in the future"):
        history.verify(receipt(observedAt="2027-01-01T00:00:00Z"), now=NOW, max_age_days=3650)


@pytest.mark.parametrize("bound", [0, -1, "14", 14.0, True])
def test_freshness_bound_must_be_a_positive_number_of_days(bound):
    with pytest.raises(ValueError, match="positive number of days"):
        history.verify(receipt(), now=NOW, max_age_days=bound)


def test_lock_pin_must_bound_the_enumeration_age(tmp_path):
    lock, _ = lock_with_history(tmp_path, receipt())
    del lock["components"][PUBLISHER]["publicationHistory"]["maxAgeDays"]
    with pytest.raises(ValueError, match="positive maxAgeDays"):
        verify_publication_history(lock, tmp_path)


def test_lock_pin_enforces_the_bound_it_declares(tmp_path):
    lock, _ = lock_with_history(tmp_path, receipt(observedAt="2024-01-01T00:00:00Z"))
    lock["components"][PUBLISHER]["publicationHistory"]["maxAgeDays"] = 1
    with pytest.raises(ValueError, match="re-enumerate it at the cut"):
        verify_publication_history(lock, tmp_path)


# --- the lock schema must actually admit the fields the generator writes -----------------------

def schema_check(lock):
    schema = json.loads((ROOT / "schemas/platform-lock.v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(lock, schema)


def locked_first_release(**overrides):
    lock = valid_lock()
    entry = {**lock["components"]["sdk"], **publisher()}
    entry.update(overrides)
    lock["components"][PUBLISHER] = entry
    return lock


def test_schema_admits_the_generated_first_release_fields():
    """Regression: additionalProperties=false once rejected every lock that could use the model."""
    schema_check(locked_first_release())


@pytest.mark.parametrize("mutate,label", [
    (lambda c: c.__setitem__("releaseVersion", "2026.1"), "CalVer release version"),
    (lambda c: c["publicationHistory"].pop("maxAgeDays"), "pin without a freshness bound"),
    (lambda c: c["publicationHistory"].__setitem__("uri", "http://insecure"), "non-HTTPS receipt uri"),
    (lambda c: c["publicationHistory"].__setitem__("smuggled", 1), "unknown pin field"),
])
def test_schema_constrains_the_first_release_fields(mutate, label):
    lock = locked_first_release()
    mutate(lock["components"][PUBLISHER])
    with pytest.raises(jsonschema.ValidationError):
        schema_check(lock)


def test_only_the_publisher_declares_the_first_release_fields():
    lock = locked_first_release()
    lock["components"]["sdk"]["releaseVersion"] = "1.0.0"
    assert any("only honua-server declares a first-release releaseVersion" in error
               for error in validate(lock).errors)


def test_lock_validator_requires_the_named_release_to_ship():
    lock = locked_first_release()
    lock["components"][PUBLISHER]["artifacts"] = [server_image("2.0.0")]
    assert any("released version of a locked publisher artifact" in error
               for error in validate(lock).errors)


# --- a bound on staleness is not a proof of emptiness -----------------------------------------

def test_a_publication_inside_the_freshness_bound_still_breaks_the_premise(tmp_path):
    """maxAgeDays only limits how old the pin may be. A tag published one day after a receipt
    written yesterday is well inside any sane bound, and only a live reading catches it."""
    lock, _ = lock_with_history(tmp_path, receipt())
    fresh = json.loads((tmp_path / RECEIPT_PATH).read_text())
    history.verify(fresh, now=datetime.strptime(fresh["observedAt"], "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc) + timedelta(days=1), max_age_days=30)
    with pytest.raises(ValueError, match="prior publication ref"):
        verify_publication_history(lock, tmp_path,
                                   lambda repo: live(repo, refs=["v1.0.0"]))


def test_the_live_enumeration_is_what_qualifies_the_first_release_model(tmp_path):
    lock, _ = lock_with_history(tmp_path, receipt())
    assert verify_publication_history(lock, tmp_path, live) is None


def test_a_live_reading_older_than_the_pin_is_refused():
    with pytest.raises(ValueError, match="observation clock is unreliable"):
        history.confirm_current(receipt(observedAt="2099-06-01T00:00:00Z"),
                                collector=lambda repo: live())


def test_offline_verification_cannot_qualify_the_first_release_model(tmp_path):
    from verify_sdk_baseline_sources import offline_collector
    lock, _ = lock_with_history(tmp_path, receipt())
    with pytest.raises(ValueError, match="offline run cannot prove"):
        verify_publication_history(lock, tmp_path, offline_collector)


def test_the_offline_source_root_path_refuses_a_first_release_lock(tmp_path, capsys):
    import verify_sdk_baseline_sources as verifier
    lock = valid_lock()
    for name in ("honua-sdk-js", "honua-sdk-dotnet", "honua-sdk-python", "geospatial-mcp"):
        lock["components"][name] = {**lock["components"][name], **first_release_component()}
    # main() resolves the pin against the repository root, so pin the committed receipt.
    committed = "sha256:" + hashlib.sha256((ROOT / RECEIPT_PATH).read_bytes()).hexdigest()
    for name in ("honua-sdk-js", "honua-sdk-dotnet", "honua-sdk-python", "geospatial-mcp"):
        lock["components"][name] = {**lock["components"][name],
                                    **first_release_component(sha=committed)}
    lock["components"][PUBLISHER] = publisher(sha=committed, max_age_days=3650)
    path = tmp_path / "lock.yaml"
    path.write_text(json.dumps(lock), encoding="utf-8")
    assert verifier.main([str(path), "--source-root", str(tmp_path)]) == 1
    assert "offline run cannot prove" in capsys.readouterr().out
