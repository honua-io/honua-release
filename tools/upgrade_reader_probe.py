"""Exercise a deployed reader against the candidate schema; emit observations only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from e2e.harness.seed.verify_edit import verify as verify_scratch_edit
from upgrade_lock_binding import BindingError, DIGEST, journal_digest


@dataclass(frozen=True)
class ReaderProbe:
    publication: dict[str, Any]
    candidate_journal_digest: str
    request: Callable[..., dict[str, Any]]
    read_journal: Callable[[], list[str]]


def probe_reader(options: ReaderProbe) -> dict[str, Any]:
    expected = options.candidate_journal_digest
    if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
        raise BindingError("candidate lock must declare an exact migration journal digest")
    before = journal_digest(options.read_journal())
    if before != expected:
        raise BindingError("database journal differs from the candidate-declared migration set")
    try:
        edit = verify_scratch_edit(options.publication, options.request)
    finally:
        after = journal_digest(options.read_journal())
        if after != before:
            raise BindingError("reader changed the candidate migration journal")
    return {"migrationJournalSha256": after, "scratchEdit": edit}
