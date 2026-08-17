"""Regression tests for immutable GitHub Actions supply-chain pins."""

from pathlib import Path

import check_action_pins as pins


def _write(root: Path, relative: str, body: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_accepts_full_sha_with_version_comment_and_local_actions(tmp_path: Path):
    _write(
        tmp_path,
        ".github/workflows/valid.yml",
        "jobs:\n  test:\n    steps:\n"
        "      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1\n"
        "      - uses: ./.github/actions/local\n",
    )

    assert pins.validate_tree(tmp_path) == []


def test_rejects_tag_branch_short_sha_and_missing_version_comment(tmp_path: Path):
    _write(
        tmp_path,
        ".github/workflows/invalid.yml",
        "jobs:\n  test:\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: owner/action@main # v1\n"
        "      - uses: owner/action@0123456789ab # v1\n"
        "      - uses: owner/action@0123456789abcdef0123456789abcdef01234567\n",
    )

    violations = pins.validate_tree(tmp_path)

    assert len(violations) == 4
    assert any("full 40-character commit SHA" in item.message for item in violations)
    assert any("human-readable version comment" in item.message for item in violations)


def test_docker_actions_require_digest_and_version_comment(tmp_path: Path):
    digest = "a" * 64
    _write(
        tmp_path,
        ".github/actions/example/action.yaml",
        "runs:\n  using: composite\n  steps:\n"
        "    - uses: docker://alpine:3.20\n"
        f"    - uses: docker://alpine@sha256:{digest} # 3.20.3\n",
    )

    violations = pins.validate_tree(tmp_path)

    assert len(violations) == 1
    assert "immutable sha256 digest" in violations[0].message


def test_rejects_a_stale_version_comment_left_behind_by_a_bump(tmp_path: Path):
    """A SHA bumped without its comment reads as the OLD version to a human reviewer."""
    bumped = "3" * 40
    _write(
        tmp_path,
        ".github/workflows/bumped.yml",
        "jobs:\n  test:\n    steps:\n"
        f"      - uses: actions/checkout@{bumped} # v7.0.1\n",
    )
    _write(
        tmp_path,
        ".github/workflows/stale.yml",
        "jobs:\n  test:\n    steps:\n"
        f"      - uses: actions/checkout@{bumped}      # v4.3.1 - for the composite action\n",
    )

    violations = pins.validate_tree(tmp_path)

    assert [item.path.name for item in violations] == ["bumped.yml", "stale.yml"]
    assert all("one pinned SHA must carry one version comment" in item.message for item in violations)


def test_reports_only_the_drifted_line_not_its_conforming_siblings(tmp_path: Path):
    """One stale comment must name one line — otherwise the real offender drowns in the log."""
    bumped = "3" * 40
    conforming = "".join(
        f"      - uses: actions/checkout@{bumped} # v7.0.1\n" for _ in range(5)
    )
    _write(tmp_path, ".github/workflows/fleet.yml", f"jobs:\n  test:\n    steps:\n{conforming}")
    _write(
        tmp_path,
        ".github/workflows/stale.yml",
        f"jobs:\n  test:\n    steps:\n      - uses: actions/checkout@{bumped} # v4.3.1\n",
    )

    violations = pins.validate_tree(tmp_path)

    assert [(item.path.name, item.line) for item in violations] == [("stale.yml", 4)]
    assert "'# v7.0.1' elsewhere" in violations[0].message


def test_rejects_one_version_comment_claiming_two_different_shas(tmp_path: Path):
    """A tag names exactly one commit, so two SHAs under one label means a fabricated comment."""
    _write(
        tmp_path,
        ".github/workflows/forked.yml",
        "jobs:\n  test:\n    steps:\n"
        f"      - uses: actions/checkout@{'a' * 40} # v7.0.1\n"
        f"      - uses: actions/checkout@{'b' * 40} # v7.0.1\n",
    )

    violations = pins.validate_tree(tmp_path)

    assert len(violations) == 2
    assert all("one version comment must resolve to one SHA" in item.message for item in violations)


def test_consistent_pins_and_unrelated_actions_are_left_alone(tmp_path: Path):
    """Different actions may share neither SHA nor label space, and repeats must stay silent."""
    _write(
        tmp_path,
        ".github/workflows/consistent.yml",
        "jobs:\n  test:\n    steps:\n"
        f"      - uses: actions/checkout@{'a' * 40} # v7.0.1\n"
        f"      - uses: actions/checkout@{'a' * 40} # v7.0.1 - and a trailing note\n"
        f"      - uses: actions/setup-python@{'b' * 40} # v7.0.1\n",
    )

    assert pins.validate_tree(tmp_path) == []


def test_scans_reusable_workflows_and_composite_actions(tmp_path: Path):
    _write(
        tmp_path,
        ".github/workflows/caller.yml",
        "jobs:\n  call:\n    uses: owner/repo/.github/workflows/reusable.yml@main\n",
    )
    _write(
        tmp_path,
        ".github/actions/example/action.yml",
        "runs:\n  using: composite\n  steps:\n    - uses: owner/action@v1\n",
    )

    violations = pins.validate_tree(tmp_path)

    assert {item.path.name for item in violations} == {"caller.yml", "action.yml"}
