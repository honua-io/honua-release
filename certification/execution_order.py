"""Which GitHub Actions workflow run executed last.

A run's `created_at` is when it was first dispatched; a re-run keeps it. `run_started_at` is when the latest
attempt started and `updated_at` is when that attempt last changed, so execution order is read from those.
"""
from __future__ import annotations

import datetime as dt
import re

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")


class AmbiguousOrder(ValueError):
    """The runs' execution order cannot be established."""


def instant(value: object) -> dt.datetime | None:
    """A GitHub UTC timestamp as an aware datetime, or None when it is missing or not a real instant."""
    text = str(value or "")
    if not _TIMESTAMP.fullmatch(text):
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _execution(run: dict) -> tuple[dt.datetime, dt.datetime]:
    started, updated = instant(run.get("run_started_at")), instant(run.get("updated_at"))
    if started is None or updated is None or updated < started:
        raise AmbiguousOrder(f"run {run.get('id')} lacks a valid run_started_at/updated_at; "
                             "execution order is unknown")
    return started, updated


def contenders(runs: list[dict]) -> list[dict]:
    """The run whose latest attempt started last, followed by every run it does not supersede.

    A run supersedes another when it started strictly after the other's last update; anything else is an
    overlap or a tie. A missing or invalid timestamp raises AmbiguousOrder, since that run's place is unknown."""
    if not runs:
        raise AmbiguousOrder("no runs to order")
    executions = [(_execution(run), run) for run in runs]
    (latest_start, _), latest = max(executions, key=lambda item: item[0][0])
    return [latest] + [run for (_, updated), run in executions if run is not latest and not updated < latest_start]


def latest_execution(runs: list[dict]) -> dict:
    """The run that executed last; an overlap or a tie with it raises AmbiguousOrder."""
    found = contenders(runs)
    if len(found) > 1:
        raise AmbiguousOrder(f"runs {found[0].get('id')} and {found[1].get('id')} overlap or tie; "
                             "execution order is ambiguous")
    return found[0]
