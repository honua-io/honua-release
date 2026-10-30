"""Phrasings A and B are the development set. C stays unread unless asked for."""

from __future__ import annotations

import json
from pathlib import Path

from blinding import hits
from rows import DEFAULT_ROWS, PILOT_KEYS, build_rows, development_keys
from scenarios import load_prompts, render_prompt

HERE = Path(__file__).resolve().parents[1]


def test_development_file_is_not_opened_for_held_out_phrasing():
    seen = []

    def reader(path: Path) -> str:
        seen.append(path.name)
        return path.read_text(encoding="utf-8")

    prompts = load_prompts(False, reader=reader)
    assert seen == ["prompts_ab.json"]
    assert all("C" not in phrasings for phrasings in prompts.values())


def test_qualification_flag_is_what_opens_the_held_out_file():
    seen = []

    def reader(path: Path) -> str:
        seen.append(path.name)
        return path.read_text(encoding="utf-8")

    prompts = load_prompts(True, reader=reader)
    assert seen == ["prompts_ab.json", "prompts_c.json"]
    assert prompts["O08"]["C"].startswith("Update {town}.")


def test_development_rows_shape_and_pilot_order():
    rows = build_rows(False)
    assert len(rows) == 48
    assert [(row["scenario"], row["phrasing"], row["trial"]) for row in rows[:3]] == list(PILOT_KEYS)
    assert development_keys()[0] == ("O01", "A", 1)
    ids = [row["id"] for row in rows]
    assert ids[:3] == ["O01-A-1", "O02-A-1", "O03-A-1"]
    assert len(ids) == len(set(ids))
    on_disk = [json.loads(line) for line in DEFAULT_ROWS.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert on_disk == rows
    held = json.loads((HERE / "prompts_c.json").read_text(encoding="utf-8"))
    blob = DEFAULT_ROWS.read_text(encoding="utf-8")
    for scenario, phrasings in held.items():
        sentence = render_prompt(phrasings["C"], "Kestrel")
        assert sentence not in blob
        assert hits(phrasings["C"]) == []
        assert hits(sentence) == []
    for row in rows:
        assert row["prompt"]
        assert row["town"] in row["prompt"]
        assert hits(row["prompt"]) == []
        assert "{town}" not in row["prompt"]
