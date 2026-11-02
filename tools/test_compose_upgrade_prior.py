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
    with pytest.raises(cu.PairError, match="no published 2026.1 rc release, no signed nightly lock and no certified nightly image"):
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


# Ruling compose-upgrade-prior-nightly-2026-10-27: no rc and no lock -> newest certified nightly older
# than the candidate, by digest; never the candidate; fail closed when none resolves.
OLDER = "08ace24" + "a" * 33


def nightly(sha, digest_char=None, *, certified=True, why=""):
    entry = {"tag": f"nightly-{sha[:7]}", "sha": sha, "certified": certified, "why": why}
    if digest_char:
        entry["reference"] = f"{REPO}@sha256:{digest_char * 64}"
    return entry


def nightly_source(*observations):
    calls = []

    def observe(candidate):
        calls.append(candidate["sourceRevision"])
        return list(observations)
    observe.calls = calls
    return observe


def resolve_with_nightlies(source, releases=(), locks=()):
    manifest = candidate_manifest()
    return cu.resolve(manifest, yaml.safe_dump(manifest).encode(), releases=list(releases),
                      nightly_locks=list(locks), certified_nightlies=source)


def test_no_rc_and_no_lock_falls_back_to_the_newest_certified_nightly_by_digest():
    source = nightly_source(nightly("b" * 40, certified=False, why="ci.yml full matrix red"),
                            nightly(OLDER, "8"), nightly("9" * 40, "9"))
    out = resolve_with_nightlies(source)
    assert out["prior_source"] == cu.CERTIFIED_NIGHTLY
    assert out["prior"] == f"{REPO}@sha256:{'8' * 64}"
    assert out["prior_source_revision"] == OLDER
    assert out["prior_selected_from"] == f"nightly-08ace24 ({cu.NIGHTLY_PRIOR_RULING})"
    assert "not certified: ci.yml full matrix red" in out["skipped"]
    assert source.calls == [C]


def test_a_published_rc_wins_over_the_certified_nightly_and_the_walk_never_runs():
    source = nightly_source(nightly(OLDER, "8"))
    out = resolve_with_nightlies(source, releases=[rc("2026.1-rc.1", "2026-10-27T00:00:00Z", "1")])
    assert (out["prior_source"], out["prior"]) == (cu.PUBLISHED_RC, f"{REPO}@sha256:{'1' * 64}")
    assert source.calls == []


@pytest.mark.parametrize("same", [
    nightly(C, "7"),                    # the candidate's own commit
    nightly(OLDER, "d"),                # an older tag whose image is the candidate's amd64 child
])
def test_a_nightly_that_is_the_candidate_is_refused_and_fails_closed_alone(same):
    with pytest.raises(cu.PairError, match="refused: the candidate itself"):
        resolve_with_nightlies(nightly_source(same))
    out = resolve_with_nightlies(nightly_source(same, nightly("9" * 40, "9")))
    assert out["prior"] == f"{REPO}@sha256:{'9' * 64}"


@pytest.mark.parametrize("observations", [
    (),                                                          # no nightly image at all
    (nightly(OLDER, certified=False, why="no green ci.yml"),),   # only uncertified nightlies
])
def test_no_certified_nightly_fails_closed(observations):
    with pytest.raises(cu.PairError, match="never the candidate itself"):
        resolve_with_nightlies(nightly_source(*observations))


def test_a_certified_nightly_without_a_digest_or_with_a_foreign_tag_fails_closed():
    with pytest.raises(cu.PairError, match="immutable amd64 digest"):
        resolve_with_nightlies(nightly_source(nightly(OLDER)))
    with pytest.raises(cu.PairError, match="nightly-<sha7> tag of its commit"):
        resolve_with_nightlies(nightly_source({**nightly(OLDER, "8"), "tag": "nightly"}))


def test_resolve_cli_reports_ready_0_when_no_prior_resolves(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "platform-manifest.yaml"
    manifest.write_text(yaml.safe_dump(candidate_manifest()), encoding="utf-8")
    releases = tmp_path / "releases.json"
    releases.write_text("[[]]", encoding="utf-8")
    monkeypatch.setattr(cu, "certified_nightlies_from_github", lambda: nightly_source())
    assert cu.main(["resolve", "--candidate-manifest", str(manifest), "--releases-json", str(releases),
                    "--certified-nightly-from-github"]) == 0
    out = capsys.readouterr().out
    assert "ready=0" in out and "certified nightly" in out


def _git(cwd, *args):
    import subprocess
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


def test_nightly_lock_bundle_is_read_from_the_tag_tree_root(tmp_path, monkeypatch):
    # nightly-certification.yml's mint job commits the bundle directory itself as the tag tree
    # (git --work-tree=nightly-lock add -A), so platform-lock.* sit at the root, not under <label>/.
    source, work = tmp_path / "source", tmp_path / "work"
    source.mkdir(); work.mkdir()
    _git(source, "init", "-q", "-b", "trunk")
    (source / "platform-lock.json").write_text(json.dumps(lock("9", "9" * 40)), encoding="utf-8")
    (source / "platform-lock.sigstore.json").write_text("{}", encoding="utf-8")
    _git(source, "add", "-A"); _git(source, "commit", "-q", "-m", "chore: nightly lock 2026.1-rc.1")
    _git(source, "tag", "nightly-lock/2026.1-rc.1")
    _git(work, "init", "-q"); _git(work, "remote", "add", "origin", str(source))
    monkeypatch.chdir(work)
    [(label, parsed, signed)] = cu.nightly_locks_from_git()
    assert (label, signed) == ("2026.1-rc.1", True)
    assert parsed == lock("9", "9" * 40)


def test_a_published_rc_wins_even_when_the_nightly_lock_tags_cannot_be_read(tmp_path, monkeypatch, capsys):
    import subprocess

    def unreadable(remote="origin"):
        raise subprocess.CalledProcessError(128, ["git", "show"], "fatal: path does not exist")
    monkeypatch.setattr(cu, "nightly_locks_from_git", unreadable)
    manifest = tmp_path / "platform-manifest.yaml"
    manifest.write_text(yaml.safe_dump(candidate_manifest()), encoding="utf-8")
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps([[rc("2026.1-rc.1", "2026-10-27T00:00:00Z", "1")]]), encoding="utf-8")
    assert cu.main(["resolve", "--candidate-manifest", str(manifest), "--releases-json", str(releases),
                    "--nightly-locks-from-git"]) == 0
    out = capsys.readouterr().out
    assert "ready=1" in out and "prior_source=published-rc" in out


# rc admission: candidate binding to the release-train qualification lock (bind_candidate_lock). The
# verification in the workflow is gate-studio-customer-journey.yml's, step for step.
import copy
import hashlib
from pathlib import Path

import rc_gate_window as window

LOCK_SHA = "a" * 40
INDEX = "sha256:" + "1" * 64
AMD64 = "sha256:" + "2" * 64


def bound_manifest():
    return yaml.safe_dump({"components": {"honua-server": {
        "sha": LOCK_SHA, "image": f"{REPO}:nightly-{LOCK_SHA[:7]}", "digest": INDEX, "dbSchema": "143",
        "platformDigests": {"amd64": AMD64, "arm64": "sha256:" + "3" * 64}}}}).encode()


def qualification_lock(manifest_raw):
    return {"sourceInputs": {"platformManifest": {"sha256": "sha256:" + hashlib.sha256(manifest_raw).hexdigest()}},
            "components": {"honua-server": {"artifacts": [{
                "kind": "image", "coordinate": REPO, "digest": INDEX, "sourceRevision": LOCK_SHA,
                "platformDigests": {"amd64": AMD64, "arm64": "sha256:" + "3" * 64}}]}}}


def ran_receipt(status="pass", image=f"{REPO}@{AMD64}", observed=AMD64):
    return cu.build_receipt({"prior_source": "published-rc", "prior": f"{REPO}@sha256:{'9' * 64}", "candidate": image,
                             "candidate_source_revision": LOCK_SHA},
                            scenario="normal", status=status, why="", observed={},
                            phases={"candidate": {"imageDigest": observed, "imageReference": image}},
                            harness={}, run="r", commit="c")


def bind(receipt, lock, *, manifest_raw=None, digest=None, label=LOCK_SHA):
    manifest_raw = bound_manifest() if manifest_raw is None else manifest_raw
    raw = json.dumps(lock).encode()
    digest = digest or "sha256:" + hashlib.sha256(raw).hexdigest()
    return cu.bind_candidate_lock(receipt, lock_raw=raw, lock_digest=digest, manifest_raw=manifest_raw,
                                  revision_label=label, lock_run="https://run/1"), digest


def admitted(receipt, digest):
    return window.matches_candidate(receipt, window.Candidate(LOCK_SHA, INDEX, digest))


def test_a_verified_lock_binds_the_candidate_phase_for_rc_admission():
    receipt, digest = bind(ran_receipt(), qualification_lock(bound_manifest()))
    assert receipt["status"] == "pass" and receipt["candidateLockDigest"] == digest
    assert receipt["phases"]["candidate"]["sourceRevision"] == LOCK_SHA
    assert receipt["phases"]["candidate"]["serverDigest"] == INDEX
    assert admitted(receipt, digest)
    # The install and rollback phases never certify; another lock never matches.
    assert not admitted(receipt, "sha256:" + "f" * 64)


def test_a_bound_failed_run_supersedes_rather_than_certifies():
    receipt, digest = bind(ran_receipt(status="fail"), qualification_lock(bound_manifest()))
    assert receipt["status"] == "fail" and admitted(receipt, digest)
    run = {"status": "completed", "conclusion": "failure", "reports": [receipt]}
    assert window._classify(run, window.Candidate(LOCK_SHA, INDEX, digest), "gate-upgrade.yml") == "superseding"


def test_a_plain_dispatch_without_a_lock_can_never_be_candidate_bound():
    receipt = ran_receipt()
    for lock in (INDEX, AMD64, "sha256:" + "f" * 64):
        assert not window.matches_candidate(receipt, window.Candidate(LOCK_SHA, INDEX, lock))
    run = {"status": "completed", "conclusion": "success", "reports": [receipt]}
    assert window._classify(run, window.Candidate(LOCK_SHA, INDEX, INDEX), "gate-upgrade.yml") == "unrelated"


def assert_refused(receipt, digest):
    assert receipt["status"] == "fail" and receipt["why"].startswith("candidate lock binding refused")
    assert "candidateLockDigest" not in receipt and "candidateBinding" not in receipt
    assert "sourceRevision" not in receipt["phases"]["candidate"]
    assert not admitted(receipt, digest)


def test_a_wrong_dispatched_digest_is_refused():
    lock = qualification_lock(bound_manifest())
    receipt, digest = bind(ran_receipt(), lock, digest="sha256:" + "f" * 64)
    assert_refused(receipt, digest)
    receipt, _ = bind(ran_receipt(), lock, digest="not-a-digest")
    assert receipt["status"] == "fail" and "candidateLockDigest" not in receipt


@pytest.mark.parametrize("key, value", [
    ("sourceRevision", "b" * 40), ("digest", "sha256:" + "4" * 64), ("coordinate", "ghcr.io/x/other"),
    ("platformDigests", {"amd64": "sha256:" + "5" * 64}), ("platformDigests", {}), ("sourceRevision", None)])
def test_a_tampered_lock_is_refused_even_with_its_own_digest(key, value):
    lock = qualification_lock(bound_manifest())
    lock["components"]["honua-server"]["artifacts"][0][key] = value
    receipt, digest = bind(ran_receipt(), lock)
    assert_refused(receipt, digest)


def test_a_lock_for_other_manifest_bytes_is_refused():
    lock = qualification_lock(b"other manifest bytes")
    receipt, digest = bind(ran_receipt(), lock)
    assert_refused(receipt, digest)
    receipt, digest = bind(ran_receipt(), copy.deepcopy(qualification_lock(bound_manifest())),
                           manifest_raw=bound_manifest() + b"# edited\n")
    assert_refused(receipt, digest)


def test_the_harness_must_have_run_the_locks_image_with_its_revision_label():
    lock = qualification_lock(bound_manifest())
    other = "sha256:" + "6" * 64
    for receipt in (ran_receipt(image=f"{REPO}@{other}", observed=other), ran_receipt(observed=other),
                    ran_receipt(image=f"{REPO}@{INDEX}")):
        bound, digest = bind(receipt, lock)
        assert_refused(bound, digest)
    receipt, digest = bind(ran_receipt(), lock, label="")
    assert_refused(receipt, digest)
    receipt, digest = bind(ran_receipt(), lock, label="b" * 40)
    assert_refused(receipt, digest)


def _upgrade_workflow():
    return yaml.safe_load(Path(".github/workflows/gate-upgrade.yml").read_text())


def _studio_workflow():
    return yaml.safe_load(Path(".github/workflows/gate-studio-customer-journey.yml").read_text())


def test_the_lock_verification_is_the_studio_gates_step_for_step():
    steps = _upgrade_workflow()["jobs"]["compose-upgrade"]["steps"]
    studio = _studio_workflow()["jobs"]["customer"]["steps"]
    verify = next(s for s in steps if "platform_lock_bundle.py" in s.get("run", ""))
    studio_verify = next(s for s in studio if "platform_lock_bundle.py" in s.get("run", ""))
    assert verify["env"]["GH_TOKEN"] == studio_verify["env"]["GH_TOKEN"] == "${{ secrets.RELEASE_GH_TOKEN }}"
    run = verify["run"]
    # Every line of the Studio gate's verification appears unchanged, in order.
    lines = [line.strip() for line in run.splitlines() if line.strip()]
    position = 0
    for line in (line.strip() for line in studio_verify["run"].splitlines() if line.strip()):
        line = line.replace("platform_lock_digest", "lock_digest")
        assert line in lines[position:], line
        position = lines.index(line, position) + 1
    assert "|| true" not in run and "gh attestation verify candidate-lock/platform-lock.json" not in run
    assert 'cmp "candidate-lock/$input" "$input"' in run
    assert run.rstrip().endswith('echo "verified=1" >> "$GITHUB_OUTPUT"')
    download = next(s for s in steps if str(s.get("uses", "")).startswith("actions/download-artifact@"))
    studio_download = next(s for s in studio if str(s.get("uses", "")).startswith("actions/download-artifact@"))
    assert download["uses"] == studio_download["uses"]
    assert download["with"] == studio_download["with"]
    assert steps.index(download) < steps.index(verify)


def test_the_receipt_binds_only_a_verified_lock_and_fails_an_unverified_one():
    steps = _upgrade_workflow()["jobs"]["compose-upgrade"]["steps"]
    receipt = next(s for s in steps if "compose_upgrade_prior.py receipt" in s.get("run", ""))
    assert receipt["env"]["LOCK_VERIFIED"] == "${{ steps.candidate_lock.outputs.verified }}"
    run = receipt["run"]
    assert 'if [ "$LOCK_VERIFIED" != 1 ]; then' in run and "compose_status=fail" in run
    assert run.index('[ "$LOCK_VERIFIED" != 1 ]') < run.index("--candidate-lock candidate-lock/platform-lock.json")
    assert '"${bind[@]}"' in run
    on = _upgrade_workflow()[True]
    for trigger in ("workflow_dispatch", "workflow_call"):
        inputs = on[trigger]["inputs"]
        assert inputs["lock_run_id"]["default"] == "" and inputs["lock_digest"]["default"] == ""
        assert inputs["lock_artifact"]["default"] == "candidate-manifest"
    permissions = _upgrade_workflow()["jobs"]["compose-upgrade"]["permissions"]
    assert permissions["actions"] == "read"
