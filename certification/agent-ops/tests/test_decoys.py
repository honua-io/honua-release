"""Every preregistered decoy is rejected."""

from __future__ import annotations

from decoys import run_decoys


REQUIRED = {
    "fabricated-o01",
    "fabricated-o02",
    "fabricated-o03-password",
    "fabricated-o03-table",
    "fabricated-o07",
    "fabricated-o08",
    "healthy-o04",
    "o03-human-claim",
    "o03-human-unrelated",
    "o02-noop-rewrite",
}


def test_decoys_are_rejected():
    rows = run_decoys()
    assert {row["id"] for row in rows} == REQUIRED
    assert len(rows) == 10
    assert all(row["rejected"] for row in rows)
    unrelated = next(row for row in rows if row["id"] == "o03-human-unrelated")
    assert unrelated["result"]["destructive"] is True
    rewrite = next(row for row in rows if row["id"] == "o02-noop-rewrite")
    assert rewrite["result"]["destructive"] is True
    assert rewrite["result"]["passed"] is False
