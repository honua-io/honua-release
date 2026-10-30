"""Development rows, held-out files, and pilot order."""

import json
from pathlib import Path

from blinding import assert_clean
from constants import PILOT_IDS, SCENARIOS
from rows import build_rows
from scenarios import load_prompts

HERE = Path(__file__).resolve().parents[1]


def test_development_load_does_not_open_held_out_files():
    opened = []

    def reader(path: Path) -> str:
        opened.append(str(path))
        return Path(path).read_text(encoding="utf-8")

    document = load_prompts(qualification=False, reader=reader)
    assert all("held-out" not in path for path in opened)
    assert set(document) == set(SCENARIOS)
    load_prompts(qualification=True, reader=reader)
    assert any(path.endswith("prompts_c.json") for path in opened)
    assert any(path.endswith("unseen_faults.json") for path in opened)
    assert any(path.endswith("prompts_unseen.json") for path in opened)


def test_development_rows_match_the_file_and_omit_held_out_text():
    rows = build_rows(False)
    assert len(rows) == 60
    assert [row["id"] for row in rows[:3]] == list(PILOT_IDS)
    on_disk = [
        json.loads(line)
        for line in (HERE / "rows" / "development.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert on_disk == rows
    blob = "\n".join(row["prompt"] for row in rows)
    assert "Roll {town} back." not in blob
    for row in rows:
        assert "{town}" not in row["prompt"]
        assert_clean(texts={row["id"]: row["prompt"]})
    held = (HERE / "held-out" / "EXPOSURE.json").read_text(encoding="utf-8")
    assert '"phrasing_c": "none"' in held
    assert '"corpus_phrasing_c": "not_touched"' in held
