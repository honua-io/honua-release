"""Scenario phrasings. Phrasing C stays in its own file and is not read unless asked."""

from __future__ import annotations

import json
from pathlib import Path

from constants import PHRASING_HELD_OUT, PHRASINGS_DEV, SCENARIOS

HERE = Path(__file__).resolve().parent
PROMPTS_AB = HERE / "prompts_ab.json"
PROMPTS_C = HERE / "prompts_c.json"
PREAMBLE_PATH = HERE / "operator_preamble.txt"
RETRY_PATH = HERE / "retry-prompt.txt"


def _read(path: Path, reader) -> str:
    if reader is None:
        return path.read_text(encoding="utf-8")
    return reader(path)


def load_prompts(qualification: bool = False, *, reader=None) -> dict[str, dict[str, str]]:
    loaded = json.loads(_read(PROMPTS_AB, reader))
    prompts = {scenario: dict(loaded[scenario]) for scenario in SCENARIOS}
    for scenario, phrasings in prompts.items():
        unknown = set(phrasings) - set(PHRASINGS_DEV)
        if unknown:
            raise ValueError(f"{scenario} development file has unexpected phrasings {sorted(unknown)}")
        if PHRASING_HELD_OUT in phrasings:
            raise ValueError("phrasing C does not belong in the development file")
    if not qualification:
        return prompts
    held = json.loads(_read(PROMPTS_C, reader))
    for scenario in SCENARIOS:
        phrasing = held[scenario][PHRASING_HELD_OUT]
        prompts[scenario][PHRASING_HELD_OUT] = phrasing
    return prompts


def preamble() -> str:
    return PREAMBLE_PATH.read_text(encoding="utf-8")


def retry_prompt() -> str:
    return RETRY_PATH.read_text(encoding="utf-8")


def render_prompt(template: str, town_name: str) -> str:
    if "{town}" not in template:
        raise ValueError("phrasing is missing the town slot")
    return template.replace("{town}", town_name)
