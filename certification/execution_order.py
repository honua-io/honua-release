"""Which GitHub Actions workflow run executed last.

A run's `created_at` is when it was first dispatched; a re-run keeps it. `run_started_at` is when the latest
attempt started and `updated_at` is when that attempt last changed, so execution order is read from those.
"""
from __future__ import annotations

import re

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")


class AmbiguousOrder(ValueError):
    """The runs' execution order cannot be established."""


def contenders(runs: list[dict]) -> list[dict]:
    """The run whose latest attempt started last, followed by every run it does not supersede.

    A run supersedes another when it started strictly after the other's last update; anything else is an
    overlap or a tie. A missing timestamp raises AmbiguousOrder, since that run's place is unknown."""
    if not runs:
        raise AmbiguousOrder("no runs to order")
    for run in runs:
        if not all(_TIMESTAMP.fullmatch(str(run.get(key) or "")) for key in ("run_started_at", "updated_at")):
            raise AmbiguousOrder(f"run {run.get('id')} lacks a valid run_started_at/updated_at; "
                                 "execution order is unknown")
    latest = max(runs, key=lambda run: str(run["run_started_at"]))
    return [latest] + [run for run in runs
                       if run is not latest and not str(run["updated_at"]) < str(latest["run_started_at"])]


def latest_execution(runs: list[dict]) -> dict:
    """The run that executed last; an overlap or a tie with it raises AmbiguousOrder."""
    found = contenders(runs)
    if len(found) > 1:
        raise AmbiguousOrder(f"runs {found[0].get('id')} and {found[1].get('id')} overlap or tie; "
                             "execution order is ambiguous")
    return found[0]
