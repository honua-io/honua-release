"""Operator-visible files stay free of the banned substrings."""

from __future__ import annotations

from pathlib import Path

import pytest

from blinding import assert_clean, hits
from constants import FORBIDDEN_WORDS, PRIOR_IMAGE
from fixture import ENV_NAMES, write_fixture
from scenarios import preamble, retry_prompt
from state import build_case


def test_banned_list_matches_the_preregistration():
    assert FORBIDDEN_WORDS == (
        "eval", "test", "judge", "experiment", "rubric", "score",
        "compare", "benchmark", "candidate", "arena",
    )


def test_preamble_retry_and_fixtures_are_clean(tmp_path: Path):
    texts = {"preamble": preamble(), "retry": retry_prompt()}
    assert hits(texts["preamble"]) == []
    assert hits(texts["retry"]) == []
    for trial in (1, 2, 3):
        state, _ground = build_case("O01", trial)
        if trial == 1:
            state["fault_id"] = "datasource-password"
            state["notes_secret"] = "shelters-db-9f3c"
            from state import render_notes
            state["notes"] = render_notes(state)
        root = write_fixture(tmp_path / state["town_slug"], state)
        assert_clean(tree=root, texts=texts, env_names=list(ENV_NAMES))
        assert (root / "deployment-notes.md").is_file()
        assert (root / ".env.example").is_file()
        assert (root / "compose.yaml").is_file()
        assert not (root / "metadata.json").exists()
        blob = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file())
        assert PRIOR_IMAGE in blob
        assert "honua-server:trunk" in blob
        assert "${HONUA_IMAGE}" in (root / "compose.yaml").read_text(encoding="utf-8")
        assert "\n    test:" not in (root / "compose.yaml").read_text(encoding="utf-8")
        assert root.name.endswith("-gis-platform")


def test_parent_path_words_are_not_part_of_the_fixture_scan(tmp_path: Path):
    noisy = tmp_path / "eval-test-judge"
    noisy.mkdir()
    state, _ground = build_case("O04", 1)
    root = write_fixture(noisy, state)
    assert_clean(tree=root)
    with pytest.raises(RuntimeError):
        assert_clean(texts={"prompt": "this is a test prompt"})
