"""Scenario prompts. Held-out files stay unread unless qualification is requested."""

from __future__ import annotations

import json
from pathlib import Path

from catalog import advertised_tools
from constants import HELD_OUT_SCENARIOS, PHRASING_HELD_OUT, SCENARIOS

HERE = Path(__file__).resolve().parent
PROMPTS_AB = HERE / "prompts_ab.json"
HELD_OUT = HERE / "held-out"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_prompts(qualification: bool = False, reader=None) -> dict:
    """Development loading never opens the held-out directory."""
    opener = reader or (lambda path: Path(path).read_text(encoding="utf-8"))
    document = json.loads(opener(PROMPTS_AB))
    if not qualification:
        return document
    for name in ("prompts_c.json", "prompts_unseen.json", "unseen_faults.json"):
        extra = json.loads(opener(HELD_OUT / name))
        if name == "unseen_faults.json":
            document["_unseen_faults"] = extra
            continue
        for scenario, phrasings in extra.items():
            document.setdefault(scenario, {}).update(phrasings)
    return document


def render_prompt(template: str, town: str) -> str:
    if "{town}" not in template:
        raise ValueError("phrasing is missing the town slot")
    return template.format(town=town)


def preamble() -> str:
    prose = (HERE / "operator_preamble.txt").read_text(encoding="utf-8").rstrip()
    names = "\n".join(f"- {name}" for name in advertised_tools())
    return prose + "\n" + names + "\n"


def retry_prompt() -> str:
    return (HERE / "retry-prompt.txt").read_text(encoding="utf-8").strip() + "\n"


def exposure_record() -> dict:
    return _read(HELD_OUT / "EXPOSURE.json")


def is_held_out_row(row: dict) -> bool:
    return row.get("phrasing") == PHRASING_HELD_OUT or row.get("scenario") in HELD_OUT_SCENARIOS or row.get("fault") == "unseen"
