"""A reader must serve edits without changing the candidate database journal."""
from dataclasses import replace
import json

import pytest

from upgrade_lock_binding import BindingError, journal_digest
from upgrade_reader_probe import ReaderProbe, probe_reader


CANDIDATE_JOURNAL = ["Honua.Migrations.001_Base.sql", "Honua.Migrations.183_Candidate.sql"]


def fixture_probe():
    stored = {"objectid": 42, "note": "original"}
    calls = []

    def request(path, values, method="GET"):
        calls.append(path)
        if path.endswith("/applyEdits"):
            stored.update(json.loads(values["updates"])[0]["attributes"])
            return {"updateResults": [{"objectId": 42, "success": True}]}
        if path.endswith("/query"):
            return {"features": [{"attributes": dict(stored), "geometry": {"x": 1, "y": 2}}]}
        return {"objectIdField": "objectid", "capabilities": "Query,Create,Update,Delete"}

    options = ReaderProbe(
        publication={"serviceName": "maui-inspections", "storageMode": "managed", "layerId": 7},
        candidate_journal_digest=journal_digest(CANDIDATE_JOURNAL),
        request=request,
        read_journal=lambda: list(CANDIDATE_JOURNAL),
    )
    return options, stored, calls


def test_reader_edits_and_restores_without_migrating():
    options, stored, calls = fixture_probe()
    result = probe_reader(options)
    assert stored == {"objectid": 42, "note": "original"}
    assert len(calls) == 6
    assert result["migrationJournalSha256"] == journal_digest(CANDIDATE_JOURNAL)
    assert result["scratchEdit"]["mutationReadBack"] is True
    assert result["scratchEdit"]["originalNoteRestored"] is True
    assert "live" not in result and "launchStack" not in result and "status" not in result


@pytest.mark.parametrize("journal", [[], ["old.sql"], ["a.sql", "a.sql"], None])
def test_wrong_or_missing_candidate_schema_refuses_before_mutation(journal):
    options, _, calls = fixture_probe()
    with pytest.raises(BindingError):
        probe_reader(replace(options, read_journal=lambda: journal))
    assert calls == []


def test_reader_attempting_migration_cannot_pass():
    options, stored, _ = fixture_probe()
    journals = iter([CANDIDATE_JOURNAL, CANDIDATE_JOURNAL + ["unexpected.sql"]])
    with pytest.raises(BindingError, match="reader changed"):
        probe_reader(replace(options, read_journal=lambda: next(journals)))
    assert stored["note"] == "original"


def test_unavailable_or_refusing_reader_cannot_count_as_compatible():
    options, _, _ = fixture_probe()
    reads = []

    def unavailable(*args, **kwargs):
        raise ConnectionError("reader refused startup")

    def journal():
        reads.append(True)
        return list(CANDIDATE_JOURNAL)

    with pytest.raises(ConnectionError):
        probe_reader(replace(options, request=unavailable, read_journal=journal))
    assert len(reads) == 2


def test_http_success_without_persisted_edit_cannot_pass_and_restores():
    options, stored, _ = fixture_probe()
    original_request = options.request

    def drops_edit(path, values, method="GET"):
        if path.endswith("/applyEdits"):
            return {"updateResults": [{"objectId": 42, "success": True}]}
        return original_request(path, values, method)

    with pytest.raises(ValueError, match="read back"):
        probe_reader(replace(options, request=drops_edit))
    assert stored["note"] == "original"


@pytest.mark.parametrize("journal", [[], None, ["bad\nname.sql"], CANDIDATE_JOURNAL[:1],
                                   CANDIDATE_JOURNAL + CANDIDATE_JOURNAL])
def test_missing_invalid_or_wrong_database_journal_refuses_before_edit(journal):
    options, stored, calls = fixture_probe()
    with pytest.raises(BindingError):
        probe_reader(replace(options, read_journal=lambda: journal))
    assert calls == []
    assert stored["note"] == "original"


@pytest.mark.parametrize("declared", [None, "", "sha256:abc", "sha256:" + "A" * 64])
def test_missing_candidate_lock_journal_digest_refuses_before_edit(declared):
    options, _, calls = fixture_probe()
    with pytest.raises(BindingError, match="candidate lock"):
        probe_reader(replace(options, candidate_journal_digest=declared))
    assert calls == []
