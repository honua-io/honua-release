"""Candidate-visible files, prompts, process names and container labels."""

from pathlib import Path

import pytest

from blinding import assert_clean, check_text
from catalog import advertised_tools
from constants import FORBIDDEN_WORDS, PRIOR_IMAGE
from fixture import ENV_NAMES, PROCESS_NAMES, write_fixture
from scenarios import preamble, retry_prompt
from state import build_case


def test_forbidden_list_and_clean_surfaces(tmp_path: Path):
    assert "eval" in FORBIDDEN_WORDS
    assert "test" in FORBIDDEN_WORDS
    prose = preamble()
    assert "requiredSequence" not in prose
    assert "honua_deploy_operations" in prose
    assert "honua_ops_health" in prose
    assert "honua_query_features" in prose
    assert_clean(texts={"preamble": prose, "retry": retry_prompt()}, allow=advertised_tools())
    for scenario in ("O01", "O09"):
        state, _ground = build_case(scenario, 1)
        root = write_fixture(tmp_path / scenario, state)
        text = (root / "compose.yaml").read_text(encoding="utf-8")
        assert "test:" not in text
        assert PRIOR_IMAGE in (root / "release-images.json").read_text(encoding="utf-8")
        assert "${HONUA_IMAGE}" in text
        assert (root / "site-handbook.md").exists()
        assert (root / "access.log").exists()
        assert (root / "projected" / "stations.geojson").exists()
        assert not (root / "metadata.json").exists()
        assert_clean(tree=root, env_names=list(ENV_NAMES), names=list(PROCESS_NAMES))
        assert root.name.startswith(f"{state['town_slug']}-gis-")
        assert "platform" not in root.name


def test_parent_directory_words_are_not_part_of_the_fixture(tmp_path: Path):
    nested = tmp_path / "eval-test-judge"
    state, _ground = build_case("O04", 1)
    root = write_fixture(nested, state)
    assert_clean(tree=root)


def test_a_prompt_that_says_test_fails():
    with pytest.raises(RuntimeError):
        assert_clean(texts={"prompt": "this is a test prompt"}, allow=advertised_tools())


def test_published_tool_names_do_not_hide_other_words():
    assert check_text("note", "honua_admin_connections_test is available", allow=advertised_tools()) == []
    assert check_text("note", "this is a test") != []
