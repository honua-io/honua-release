"""Development rows: one JSON object per scenario, phrasing, and trial."""

from __future__ import annotations

import json
from pathlib import Path

from constants import PHRASINGS_DEV, SCENARIOS
from scenarios import load_prompts, render_prompt
from state import town_for

HERE = Path(__file__).resolve().parent
DEFAULT_ROWS = HERE / "rows" / "development.jsonl"
PILOT_KEYS = (("O01", "A", 1), ("O02", "A", 1), ("O03", "A", 1))


def development_keys() -> list[tuple[str, str, int]]:
    """Pilot trio first, then the remaining scenario × phrasing A/B × trial cells."""
    rest = [
        (scenario, phrasing, trial)
        for scenario in SCENARIOS
        for phrasing in PHRASINGS_DEV
        for trial in (1, 2, 3)
        if (scenario, phrasing, trial) not in PILOT_KEYS
    ]
    return list(PILOT_KEYS) + rest


def build_row(scenario: str, phrasing: str, trial: int, prompts: dict[str, dict[str, str]]) -> dict:
    town = town_for(scenario, trial)
    prompt = render_prompt(prompts[scenario][phrasing], town["name"])
    return {
        "id": f"{scenario}-{phrasing}-{trial}",
        "scenario": scenario,
        "phrasing": phrasing,
        "trial": trial,
        "town": town["name"],
        "town_slug": town["slug"],
        "prompt": prompt,
    }


def build_rows(qualification: bool = False) -> list[dict]:
    prompts = load_prompts(qualification=qualification)
    keys = development_keys()
    if qualification:
        extra = [
            (scenario, "C", trial)
            for scenario in SCENARIOS
            for trial in (1, 2, 3)
        ]
        keys = keys + extra
    return [build_row(scenario, phrasing, trial, prompts) for scenario, phrasing, trial in keys]


def write_development(path: Path | None = None) -> Path:
    target = path or DEFAULT_ROWS
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in build_rows(False)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
