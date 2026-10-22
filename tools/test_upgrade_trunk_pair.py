import json
from pathlib import Path

import pytest
import yaml

import upgrade_trunk_pair as pair

A, B, C = "a" * 40, "b" * 40, "c" * 40
HELM = "d" * 40


def manifest(sha, digest_char, schema, *, amd64=True, helm=HELM):
    server = {"sha": sha, "image": f"ghcr.io/honua-io/honua-server:nightly-{sha[:7]}",
              "digest": "sha256:" + digest_char * 64, "dbSchema": str(schema)}
    if amd64:
        server["platformDigests"] = {"amd64": "sha256:" + digest_char * 63 + "0"}
    return {"components": {"honua-server": server, "honua-helm": {"sha": helm}}}


def test_previous_pin_skips_history_entries_with_the_candidates_own_server():
    candidate = manifest(C, "c", 128)
    history = [("1" * 40, manifest(C, "c", 128)), ("2" * 40, {"components": {}}),
               ("3" * 40, manifest(B, "b", 125)), ("4" * 40, manifest(A, "a", 106))]
    out = pair.resolve(candidate, yaml.safe_dump(candidate).encode(), history=history)
    assert out["ready"] == "1" and out["prior_source"] == "trunk-candidate"
    assert out["prior_source_revision"] == B and out["candidate_source_revision"] == C
    assert out["prior"].startswith("ghcr.io/honua-io/honua-server@sha256:")
    assert ":nightly" not in out["prior"] and ":nightly" not in out["candidate"]
    assert out["prior_schema"] == "125" and out["candidate_schema"] == "128"
    assert out["prior_selected_from"].startswith("platform-manifest.yaml@" + "3" * 40)
    assert out["helm_sha"] == HELM and out["prior_helm_sha"] == HELM and out["cross"] == "yes"


def test_prior_installs_with_the_chart_its_own_manifest_pinned():
    prior = manifest(A, "a", 106, amd64=False, helm="e" * 40)
    out = pair.resolve(manifest(C, "c", 128), b"c", prior_manifest=prior, prior_bytes=b"p")
    assert out["prior_helm_sha"] == "e" * 40 and out["helm_sha"] == HELM
    no_helm = {"components": {"honua-server": prior["components"]["honua-server"]}}
    assert pair.resolve(manifest(C, "c", 128), b"c", prior_manifest=no_helm, prior_bytes=b"p")["prior_helm_sha"] == HELM


def test_candidate_uses_the_amd64_child_and_falls_back_to_the_index_digest():
    with_child = pair.server_identity(manifest(C, "c", 128), "m")
    assert with_child["reference"].endswith("c" * 63 + "0")
    index_only = pair.server_identity(manifest(A, "a", 106, amd64=False), "m")
    assert index_only["reference"] == "ghcr.io/honua-io/honua-server@sha256:" + "a" * 64


def test_explicit_prior_manifest_is_labelled_and_its_bytes_bind_the_identity_digest():
    prior = manifest(A, "a", 106, amd64=False)
    prior_bytes = yaml.safe_dump(prior).encode()
    candidate = manifest(C, "c", 128)
    out = pair.resolve(candidate, b"candidate", prior_manifest=prior, prior_bytes=prior_bytes,
                       prior_label="honua-2026.1 pre-release snapshot")
    assert out["prior_selected_from"] == "honua-2026.1 pre-release snapshot"
    assert out["prior_manifest_digest"] == pair._bytes_digest(prior_bytes)
    assert out["candidate_manifest_digest"] == pair._bytes_digest(b"candidate")


@pytest.mark.parametrize("prior, message", [
    (manifest(A, "a", 130), "newer than candidate"),
    ({"components": {"honua-server": {"sha": A, "image": "ghcr.io/x/y:latest", "dbSchema": "1"}}},
     "no immutable image digest"),
    ({"components": {"honua-server": {"sha": "short", "digest": "sha256:" + "a" * 64}}}, "full commit sha"),
])
def test_unusable_pairs_refuse(prior, message):
    with pytest.raises(pair.PairError, match=message):
        pair.resolve(manifest(C, "c", 128), b"c", prior_manifest=prior, prior_bytes=b"p")


def test_same_server_is_not_a_cross_release_pair():
    out = pair.resolve(manifest(C, "c", 128), b"c", prior_manifest=manifest(C, "c", 128), prior_bytes=b"p")
    assert out["cross"] == "no"


def test_resolve_cli_reports_unavailable_as_ready_zero(tmp_path, capsys):
    (tmp_path / "m.yaml").write_text(yaml.safe_dump(manifest(C, "c", 128)))
    assert pair.main(["resolve", "--candidate-manifest", str(tmp_path / "m.yaml"),
                      "--prior-manifest", str(tmp_path / "missing.yaml"), "--prior-label", "honua-2026.1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "ready=0" and out[1].startswith("why=trunk-candidate pair unavailable")


def test_receipt_is_never_a_certified_release_pair_and_keeps_the_signed_lock_blocked(tmp_path):
    values = pair.resolve(manifest(C, "c", 128), b"c", prior_manifest=manifest(A, "a", 106), prior_bytes=b"p")
    (tmp_path / "pair.json").write_text(json.dumps(values))
    (tmp_path / "observed.json").write_text(json.dumps({"observedSchema": "128"}))
    (tmp_path / "install-identity.json").write_text(json.dumps({"imageID": "x"}))
    out = tmp_path / "receipt.json"
    assert pair.main(["receipt", "--pair", str(tmp_path / "pair.json"), "--scenario", "normal",
                      "--status", "pass", "--why", "ok", "--signed-lock-why", "no signed prior lock",
                      "--observed", str(tmp_path / "observed.json"), "--phase-dir", str(tmp_path),
                      "--output", str(out)]) == 0
    receipt = json.loads(out.read_text())
    assert receipt["schema"] == pair.SCHEMA
    assert receipt["priorSource"] == "trunk-candidate" and receipt["certifiedReleasePair"] is False
    assert receipt["signedLockRequirement"] == {"status": "blocked", "why": "no signed prior lock"}
    assert receipt["status"] == "pass" and receipt["phases"] == {"install": {"imageID": "x"}}
    assert receipt["prior"]["sourceRevision"] == A and receipt["candidate"]["sourceRevision"] == C


def test_receipt_normalises_unknown_status_to_fail(tmp_path):
    receipt = pair.build_receipt({}, scenario="normal", status="weird", why="", signed_lock_why="x",
                                 observed={}, phases={}, run="", commit="")
    assert receipt["status"] == "fail"


def test_workflow_runs_the_fallback_only_without_signed_locks_and_keeps_its_own_fragment():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/gate-upgrade.yml").read_text(encoding="utf-8")
    assert "if: steps.images.outputs.verified != '1'" in text
    assert "python tools/upgrade_trunk_pair.py resolve" in text
    assert "gate: upgrade-kind-trunk-candidate-${{ matrix.scenario }}" in text
    assert "prior-source: trunk-candidate" in text
    # The signed-lock fragment is forced back to blocked after a fallback run.
    assert 'STATUS=blocked\n            WHY="$SIGNED_LOCK_WHY (trunk-candidate fallback ran separately' in text
    assert "steps.smoke.outputs.mode != 'trunk-candidate'" in text
