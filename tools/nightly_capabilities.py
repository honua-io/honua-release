"""Age optional diagnostic nightly observations at the capability gate's evaluation clock."""
from __future__ import annotations

import copy
import datetime as dt

import source_facts  # establishes the certification module path
from execution_order import instant

MAX_AGE_DAYS = 14
OBSERVATIONS = ('interop', 'cite', 'geobenchResults')


def collection_window(matrix: dict, now: dt.datetime) -> tuple:
    policy = matrix.get('evidenceFreshness') or {}
    anchor = instant((matrix.get('nightlyEvidence') or {}).get('collectedAt'))
    limit = policy.get('maxAgeDays')
    valid_limit = type(limit) in (int, float) and 0 < limit <= MAX_AGE_DAYS
    valid_anchor = anchor is not None and anchor == instant(policy.get('anchor'))
    if not valid_limit or not valid_anchor or anchor > now:
        return None, None
    return anchor, dt.timedelta(days=limit)


def age_row(row: dict, window: tuple, now: dt.datetime) -> None:
    anchor, limit = window
    freshness = row.get('freshness') or {}
    observed = instant(freshness.get('runDate'))
    if anchor is None or observed is None or observed > anchor:
        freshness['state'] = 'unknown'
    elif now - anchor > limit or now - observed > limit:
        freshness['state'] = 'stale'
    freshness['ageDays'] = round((now - observed).total_seconds() / 86400, 3) if observed else None
    row['freshness'] = freshness
    if 'suite' in row and freshness.get('state') != 'fresh':
        row['passRate'] = 0.0


def assess(matrix: dict, now: dt.datetime | None = None) -> dict:
    if 'nightlyEvidence' not in matrix:
        return matrix
    now = now or dt.datetime.now(dt.timezone.utc)
    result = copy.deepcopy(matrix)
    window = collection_window(result, now)
    for entry in result.get('capabilities', []):
        for name in OBSERVATIONS:
            for row in entry.get(name) or []:
                age_row(row, window, now)
    return result


def observations(entry: dict) -> dict:
    return {name: entry.get(name) or [] for name in OBSERVATIONS}


def cite_observed_at(entry: dict) -> str | None:
    dates = [instant((row.get('freshness') or {}).get('runDate')) for row in entry.get('cite') or []]
    return min(dates).isoformat().replace('+00:00', 'Z') if dates and all(dates) else None
