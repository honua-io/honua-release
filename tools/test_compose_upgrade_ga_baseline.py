"""GA admission cannot substitute newer RCs or nightly images for the shipped rc.2 baseline."""
import json

import pytest
import yaml

import compose_upgrade_prior as cu
from test_compose_upgrade_prior import REPO, candidate_manifest, rc


def resolve_ga(releases, label="2026.1.0"):
    manifest = candidate_manifest()
    manifest["platformRelease"] = label

    def forbidden_fallback(*args):
        pytest.fail("GA must never consult nightly fallback sources")

    return cu.resolve(manifest, yaml.safe_dump(manifest).encode(), releases=releases,
                      nightly_locks=forbidden_fallback, certified_nightlies=forbidden_fallback)


@pytest.mark.parametrize("tag", ["2026.1-rc.2", "honua-2026.1-rc.2", "2026.1.0-rc.2", "honua-2026.1.0-rc.2"])
@pytest.mark.parametrize("label", ["2026.1", "honua-2026.1", "2026.1.0"])
def test_ga_selects_rc2_even_when_a_newer_rc_exists(tag, label):
    pair = resolve_ga([rc("2026.1-rc.3", "2026-12-01T00:00:00Z", "3"),
                       rc(tag, "2026-11-20T00:00:00Z", "2")], label)
    assert pair["prior"] == f"{REPO}@sha256:" + "2" * 64
    assert pair["prior_selected_from"] == tag
    assert pair["prior_source"] == cu.PUBLISHED_RC
    assert pair["prior_release_url"] == f"https://r/{tag}"


@pytest.mark.parametrize("releases", [
    [],
    [rc("2026.1-rc.1", "2026-11-01T00:00:00Z", "1")],
    [rc("2026.1-rc.3", "2026-12-01T00:00:00Z", "3")],
    [rc("2026.1-rc.2", "2026-11-20T00:00:00Z", "2", draft=True)],
    [rc("2026.1-rc.2", None, "2")],
    [rc("2026.1-rc.2", "2026-11-20T00:00:00Z", "2"),
     rc("honua-2026.1.0-rc.2", "2026-11-21T00:00:00Z", "3")],
])
def test_ga_refuses_missing_unpublished_or_ambiguous_baseline_without_fallback(releases):
    with pytest.raises(cu.PairError, match="exactly one published.*rc.2 baseline"):
        resolve_ga(releases)


@pytest.mark.parametrize("digest", ["c", "d", "e"])
def test_ga_refuses_candidate_identical_baseline_without_fallback(digest):
    with pytest.raises(cu.PairError, match="baseline is the candidate image"):
        resolve_ga([rc("2026.1-rc.2", "2026-11-20T00:00:00Z", digest)])


def test_ga_refuses_baseline_without_immutable_server_image():
    with pytest.raises(cu.PairError, match="exactly one honua-server image digest"):
        resolve_ga([rc("2026.1-rc.2", "2026-11-20T00:00:00Z", "2", body="no image")])


def test_malformed_candidate_version_cannot_enable_rc_fallback():
    with pytest.raises(ValueError):
        resolve_ga([rc("2026.1-rc.3", "2026-12-01T00:00:00Z", "3")], "2026.1.garbage")


@pytest.mark.parametrize("label", ["2026.1-rc.2", "honua-2026.1.0-rc.2"])
def test_rc_candidates_keep_latest_published_rc_policy(label):
    pair = resolve_ga([rc("2026.1-rc.2", "2026-11-20T00:00:00Z", "2"),
                       rc("2026.1-rc.3", "2026-12-01T00:00:00Z", "3")], label)
    assert pair["prior_selected_from"] == "2026.1-rc.3"


def test_cli_reports_missing_ga_baseline_as_unavailable(tmp_path, capsys):
    manifest = candidate_manifest()
    manifest["platformRelease"] = "2026.1.0"
    candidate_path = tmp_path / "candidate.yaml"
    candidate_path.write_text(yaml.safe_dump(manifest))
    releases_path = tmp_path / "releases.json"
    releases_path.write_text(json.dumps([[rc("2026.1-rc.3", "2026-12-01T00:00:00Z", "3")]]))
    assert cu.main(["resolve", "--candidate-manifest", str(candidate_path),
                    "--releases-json", str(releases_path)]) == 0
    output = capsys.readouterr().out
    assert output.startswith("ready=0\nwhy=compose prior unavailable:")
    assert "rc.2 baseline" in output
    assert "\nprior=" not in output
