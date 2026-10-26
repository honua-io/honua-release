"""Prior resolution, verdict classification and receipt for the GA Compose upgrade lane (D9, #950)."""
import json

import pytest
import yaml

import compose_upgrade_prior as cu

C = "c" * 40
REPO = "ghcr.io/honua-io/honua-server"


def candidate_manifest():
    return {"components": {"honua-server": {
        "sha": C, "image": f"{REPO}:nightly-{C[:7]}", "digest": "sha256:" + "c" * 64, "dbSchema": "136",
        "platformDigests": {"amd64": "sha256:" + "d" * 64, "arm64": "sha256:" + "e" * 64}}}}


def rc(tag, published, digest_char, *, draft=False, body=None):
    if body is None:
        body = ("## Components\n\n| Component | Version | Pinned commit | Artifact / image |\n|---|---|---|---|\n"
                f"| honua-console | pre-release | `abc` | ghcr.io/honua-io/honua-console@sha256:{'f' * 64} |\n"
                f"| honua-server | pre-release | `see image tag` | {REPO}:nightly-x @ `sha256:{digest_char * 64}` |\n")
    return {"tag_name": tag, "published_at": published, "draft": draft, "html_url": f"https://r/{tag}", "body": body}


def lock(digest_char, revision):
    return {"components": {"honua-server": {"artifacts": [{
        "kind": "image", "coordinate": REPO, "digest": "sha256:" + digest_char * 64, "sourceRevision": revision,
        "platformDigests": {"amd64": "sha256:" + digest_char * 64}}]}}}


def resolve(releases=(), locks=()):
    manifest = candidate_manifest()
    return cu.resolve(manifest, yaml.safe_dump(manifest).encode(), releases=list(releases), nightly_locks=list(locks))


def test_latest_published_rc_image_is_the_prior_by_digest():
    out = resolve([rc("2026.1-rc.1", "2026-10-27T00:00:00Z", "1"), rc("2026.1-rc.2", "2026-10-29T00:00:00Z", "2"),
                   rc("honua-2026.1", "2026-12-01T00:00:00Z", "9"),           # not an rc tag
                   rc("2026.1-rc.3", "2026-12-02T00:00:00Z", "3", draft=True)])  # draft never counts
    assert out["ready"] == "1" and out["prior_source"] == "published-rc"
    assert out["prior"] == f"{REPO}@sha256:" + "2" * 64 and out["prior_selected_from"] == "2026.1-rc.2"
    assert out["candidate"] == f"{REPO}@sha256:" + "d" * 64, "candidate is the amd64 child"
    assert out["candidate_schema"] == "136" and out["platform"] == "linux/amd64"
    assert ":nightly" not in out["prior"]


@pytest.mark.parametrize("same", ["c", "d"])  # index digest or amd64 child: the same image
def test_an_rc_that_ships_the_candidate_image_is_skipped_never_used_as_prior(same):
    out = resolve([rc("2026.1-rc.2", "2026-10-29T00:00:00Z", same), rc("2026.1-rc.1", "2026-10-27T00:00:00Z", "1")])
    assert out["prior_selected_from"] == "2026.1-rc.1" and "2026.1-rc.2 (same image" in out["skipped"]
    with pytest.raises(cu.PairError, match="never the candidate itself"):
        resolve([rc("2026.1-rc.2", "2026-10-29T00:00:00Z", same)])


def test_an_rc_without_an_immutable_server_digest_fails_closed():
    broken = rc("2026.1-rc.2", "2026-10-29T00:00:00Z", "2", body=f"| honua-server | x | y | {REPO}:nightly-x |\n")
    with pytest.raises(cu.PairError, match="does not name exactly one honua-server image digest"):
        resolve([broken, rc("2026.1-rc.1", "2026-10-27T00:00:00Z", "1")])


def test_else_the_previous_signed_nightly_lock_is_the_prior():
    locks = [("2026.1-rc.2", lock("2", "2" * 40), True), ("2026.1-rc.10", lock("a", "a" * 40), True),
             ("2026.1-rc.11", lock("d", C), True)]  # rc.11 is the candidate itself
    out = resolve(locks=locks)
    assert out["prior_source"] == "nightly-lock" and out["prior_selected_from"] == "nightly-lock/2026.1-rc.10"
    assert out["prior"] == f"{REPO}@sha256:" + "a" * 64 and out["prior_source_revision"] == "a" * 40
    assert "nightly-lock/2026.1-rc.11 (same image" in out["skipped"]


def test_a_published_rc_wins_over_a_nightly_lock_and_an_unsigned_lock_fails_closed():
    out = resolve([rc("2026.1-rc.1", "2026-10-27T00:00:00Z", "1")], [("2026.1-rc.9", lock("9", "9" * 40), True)])
    assert out["prior_source"] == "published-rc"
    with pytest.raises(cu.PairError, match="no signature bundle"):
        resolve(locks=[("2026.1-rc.9", lock("9", "9" * 40), False)])


def test_neither_source_is_unavailable_never_a_fallback(tmp_path, capsys):
    with pytest.raises(cu.PairError, match="no published 2026.1 rc release and no signed nightly lock"):
        resolve()
    manifest = tmp_path / "platform-manifest.yaml"
    manifest.write_text(yaml.safe_dump(candidate_manifest()))
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps([[{"tag_name": "honua-2026.1", "published_at": "2026-08-20T00:00:00Z",
                                      "prerelease": True, "body": ""}]]))
    assert cu.main(["resolve", "--candidate-manifest", str(manifest), "--releases-json", str(releases)]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == "ready=0" and printed[1].startswith("why=compose prior unavailable: no published 2026.1 rc")
    assert not any(line.startswith("prior=") for line in printed)


@pytest.mark.parametrize("scenario,code,recorded,why,expected", [
    ("normal", 0, "pass", "ok", "pass"),
    ("normal", 1, "fail", "seeded rows did not survive the upgrade", "fail"),
    ("normal", 0, None, "", "fail"),                      # exit 0 without a recorded pass is not a pass
    ("normal", 1, "blocked", "prior image equals the candidate", "blocked"),
    ("negative-control", 1, "fail", "seeded rows did not survive the upgrade (2 -> 1)", "pass"),
    ("negative-control", 0, "pass", "ok", "fail"),        # the gate missed the injected defect
    ("negative-control", 2, None, "", "fail"),            # a crash is not the gate's own assertion
    ("negative-control", 1, "fail", "candidate did not advance", "fail"),  # wrong assertion fired
])
def test_classify_requires_the_gates_own_verdict(scenario, code, recorded, why, expected):
    harness = {"scenarios": [{"scenario": "compose-upgrade-rollback", "status": recorded, "why": why}]} if recorded else {}
    status, _ = cu.classify(scenario, code, harness, "seeded rows did not survive the upgrade"
                            if scenario == "negative-control" else "")
    assert status == expected


def test_receipt_has_the_kind_pair_shape_and_says_it_is_the_required_ga_path(tmp_path):
    receipt = cu.build_receipt({"prior_source": "published-rc", "prior": "p@sha256:x", "candidate": "c@sha256:y"},
                               scenario="normal", status="weird", why="w", observed={"a": 1},
                               phases={"install": {}}, harness={}, run="r", commit="c")
    assert receipt["status"] == "fail" and receipt["schema"] == "honua.upgrade-compose-receipt/v1"
    assert receipt["certification"]["role"] == "ga-compose-upgrade-path"
    assert receipt["certification"]["verdict"] == "required" and receipt["platform"] == "linux/amd64"
    assert receipt["certifiedReleasePair"] is False and receipt["topology"] == "docker-compose"
    for key in ("priorSource", "scenario", "why", "prior", "candidate", "observed", "phases", "workflow"):
        assert key in receipt
