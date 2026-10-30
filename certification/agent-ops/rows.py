"""One development row per scenario, phrasing and trial. Campaign E local set is 60 rows."""

from __future__ import annotations

import json
from pathlib import Path

from constants import PHRASINGS_DEV, PILOT_IDS, SCENARIOS
from scenarios import load_prompts, render_prompt
from state import town_for

HERE = Path(__file__).resolve().parent
DEFAULT_ROWS = HERE / "rows" / "development.jsonl"


def build_row(scenario: str, phrasing: str, trial: int, prompts: dict) -> dict:
    town = town_for(scenario, trial)
    template = prompts[scenario][phrasing]
    return {
        "id": f"{scenario}-{phrasing}-{trial}",
        "scenario": scenario,
        "phrasing": phrasing,
        "trial": trial,
        "town": town["name"],
        "town_slug": town["slug"],
        "prompt": render_prompt(template, town["name"]),
    }


def development_keys() -> list[tuple[str, str, int]]:
    keys = [(scenario, phrasing, trial) for scenario in SCENARIOS for phrasing in PHRASINGS_DEV for trial in (1, 2, 3)]
    pilot = []
    rest = []
    for key in keys:
        row_id = f"{key[0]}-{key[1]}-{key[2]}"
        if row_id in PILOT_IDS:
            pilot.append(key)
        else:
            rest.append(key)
    order = {row_id: index for index, row_id in enumerate(PILOT_IDS)}
    pilot.sort(key=lambda key: order[f"{key[0]}-{key[1]}-{key[2]}"])
    return pilot + rest


def build_rows(qualification: bool = False) -> list[dict]:
    prompts = load_prompts(qualification=qualification)
    if qualification:
        raise RuntimeError("development rows stay on phrasings A and B")
    return [build_row(scenario, phrasing, trial, prompts) for scenario, phrasing, trial in development_keys()]


def write_development(path: Path | None = None) -> int:
    target = path or DEFAULT_ROWS
    rows = build_rows(False)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, sort_keys=True) for row in rows]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)
