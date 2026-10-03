"""Prior-release resolution for the upgrade gate and rollback target (ruling R26, honua-release#376)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_upgrade as up  # noqa: E402

SIGNED_LOCK = [{"name": "platform-lock.json"}, {"name": "platform-lock.sigstore.json"},
               {"name": "finalized-manifest.yaml"}]
# The 2026-08-20 engineering snapshot exactly as published: a pre-release with a finalized manifest.
SNAPSHOT = {"tag_name": "honua-2026.1", "draft": False, "prerelease": True,
            "published_at": "2026-08-20T16:49:02Z",
            "assets": [{"name": "finalized-manifest.yaml"}, {"name": "gate-report.json"}]}


def _promoted(tag, published_at, assets=SIGNED_LOCK, **extra):
    return {"tag_name": tag, "draft": False, "prerelease": False, "published_at": published_at,
            "assets": list(assets), **extra}


def _command(pages):
    def command(*args):
        assert args[:3] == ("api", "--paginate", "--slurp")
        return json.dumps(pages)
    return command


def test_pre_release_snapshot_is_ignored():
    assert up.prior_platform_release([SNAPSHOT]) is None
    # Even with a signed lock attached, a pre-release is never the prior platform release.
    assert up.prior_platform_release([{**SNAPSHOT, "assets": SIGNED_LOCK}]) is None


def test_real_prior_release_is_selected_over_newer_snapshot():
    real = _promoted("honua-2026.1.0", "2026-10-20T00:00:00Z")
    snapshot = {**SNAPSHOT, "published_at": "2026-11-01T00:00:00Z"}
    assert up.prior_platform_release([snapshot, real]) is real


def test_newest_promoted_release_wins_across_pages():
    older = _promoted("honua-2026.1.0", "2026-10-20T00:00:00Z")
    newer = _promoted("honua-2026.1.1", "2026-11-03T00:00:00Z")
    releases = up.list_releases("honua-io/honua-release", _command([[SNAPSHOT, older], [newer]]))
    assert up.prior_platform_release(releases)["tag_name"] == "honua-2026.1.1"


@pytest.mark.parametrize("release", [
    _promoted("honua-2026.1.0", "2026-10-20T00:00:00Z", draft=True),
    _promoted("honua-2026.1.0", "2026-10-20T00:00:00Z", assets=[{"name": "platform-lock.json"}]),
    _promoted("honua-2026.1.0", "2026-10-20T00:00:00Z", assets=[{"name": "finalized-manifest.yaml"}]),
    _promoted("server-v1.2.3", "2026-10-20T00:00:00Z"),
])
def test_drafts_unsigned_locks_and_other_tags_are_not_platform_releases(release):
    assert up.prior_platform_release([release]) is None


def test_eligibility_bound_is_applied_before_selection():
    older = _promoted("honua-2026.1.0", "2026-10-20T00:00:00Z")
    newer = _promoted("honua-2026.1.1", "2026-11-03T00:00:00Z")
    selected = up.prior_platform_release([older, newer], lambda r: r["tag_name"] != "honua-2026.1.1")
    assert selected is older


def test_cli_reports_first_release_basis_when_only_the_snapshot_exists(monkeypatch, capsys):
    monkeypatch.setattr(up, "_gh", _command([[SNAPSHOT]]))
    assert up.main(["--resolve-prior-release", "honua-io/honua-release"]) == 0
    assert capsys.readouterr().out == "\n"


def test_cli_reports_first_release_basis_when_no_release_exists(monkeypatch, capsys):
    monkeypatch.setattr(up, "_gh", _command([[]]))
    assert up.main(["--resolve-prior-release", "honua-io/honua-release"]) == 0
    assert capsys.readouterr().out == "\n"


def test_cli_prints_the_real_prior_release(monkeypatch, capsys):
    monkeypatch.setattr(up, "_gh", _command([[SNAPSHOT, _promoted("honua-2026.1.0", "2026-10-20T00:00:00Z")]]))
    assert up.main(["--resolve-prior-release", "honua-io/honua-release"]) == 0
    assert capsys.readouterr().out == "honua-2026.1.0\n"


def test_cli_lookup_failure_is_not_a_first_release(monkeypatch, capsys):
    def unavailable(*args):
        raise up.ReleaseLookupError("HTTP 502")
    monkeypatch.setattr(up, "_gh", unavailable)
    assert up.main(["--resolve-prior-release", "honua-io/honua-release"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PRIOR_RELEASE_LOOKUP_FAILED" in captured.err


def test_upgrade_gate_and_rollback_target_share_the_resolver():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/gate-upgrade.yml").read_text()
    assert "gh release list" not in workflow
    assert workflow.count("tools/check_upgrade.py --resolve-prior-release") == 2
    import release_rollback_target
    assert release_rollback_target.prior_platform_release is up.prior_platform_release
