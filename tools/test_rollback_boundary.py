"""A refused reader-incompatible script is a boundary only when the note names it."""
import contextlib
import os
import stat
from io import StringIO
from pathlib import Path

import pytest

from rollback_boundary import BOUNDARY, boundary_for, main, script_stems

STEM = "142_CreateAiFinancialAccounting"
OTHER = "144_ArcUnknownFuture"
PHRASE = "declared reader-incompatible, not expand"
HASH_REASON = "the recorded script text does not match its recorded hash"
REFUSAL = (
    "schema_reader_incompatible: applied migration "
    f"Honua.Server.Migrations.{STEM}.sql ({PHRASE})."
)
REVIEWED = (
    "-- honua:reader-incompatible reason=the previous reader cannot parse the ledger\n"
    "-- honua:compatibility-review\n"
)
COMBINED = (
    "schema_reader_incompatible: this server cannot verify compatibility with applied "
    f"migration(s): Honua.Server.Migrations.{STEM}.sql, Honua.Server.Migrations.{OTHER}.sql. "
    "Start a compatible server or restore the database backup matching this revision. "
    "Application rollback does not reverse database migrations. "
    "See the Rollback boundaries table in Upgrade and roll back. "
    "Forward-compatibility declaration(s) refused: "
    f"Honua.Server.Migrations.{STEM}.sql ({PHRASE}); "
    f"Honua.Server.Migrations.{OTHER}.sql ({PHRASE})."
)


def test_script_stems_keep_each_refused_declaration_once():
    text = (
        f"{REFUSAL}\n"
        "schema_reader_incompatible: 144_ArcUnknownFuture.sql\n"
        "schema_reader_incompatible: 1000_LaterLedger.sql"
    )
    assert script_stems(text) == (STEM, "144_ArcUnknownFuture", "1000_LaterLedger")


def test_a_named_reviewed_script_is_the_declaration_boundary():
    note = f"Restore instead of rolling the image across {STEM}."
    assert boundary_for(REFUSAL, note, {STEM: REVIEWED}) == BOUNDARY


def test_a_script_missing_from_the_note_is_not_a_boundary():
    assert boundary_for(REFUSAL, "restore the backup", {STEM: REVIEWED}) is None


def test_a_script_without_the_review_annotation_is_not_a_boundary():
    note = f"Restore across {STEM}."
    text = "-- honua:reader-incompatible reason=ledger\n"
    assert boundary_for(REFUSAL, note, {STEM: text}) is None


def test_an_expand_refusal_is_not_this_boundary():
    note = f"Restore across {STEM}."
    refusal = f"schema_reader_incompatible: applied migration {STEM}.sql (expand-only)."
    assert boundary_for(refusal, note, {STEM: REVIEWED}) is None


def test_one_undocumented_script_rejects_the_whole_refusal():
    note = f"Restore across {STEM}."
    refusal = (
        f"{REFUSAL} schema_reader_incompatible: {OTHER}.sql "
        "(declared reader-incompatible, not expand)."
    )
    scripts = {STEM: REVIEWED, OTHER: REVIEWED}
    assert boundary_for(refusal, note, scripts) is None


def test_a_combined_exception_accepts_only_when_every_reason_matches():
    note = f"Restore across {STEM} and {OTHER}."
    scripts = {STEM: REVIEWED, OTHER: REVIEWED}
    assert boundary_for(COMBINED, note, scripts) == BOUNDARY
    mixed = COMBINED.replace(f"{OTHER}.sql ({PHRASE})", f"{OTHER}.sql ({HASH_REASON})")
    assert boundary_for(mixed, note, scripts) is None


def test_separate_log_records_reject_a_hash_mismatch():
    note = f"Restore across {STEM} and {OTHER}."
    refusal = (
        f"{REFUSAL}\n"
        f"schema_reader_incompatible: applied migration {OTHER}.sql ({HASH_REASON})."
    )
    assert boundary_for(refusal, note, {STEM: REVIEWED, OTHER: REVIEWED}) is None


def test_a_listed_migration_without_a_reason_is_not_a_boundary():
    note = f"Restore across {STEM} and {OTHER}."
    refusal = (
        "schema_reader_incompatible: this server cannot verify compatibility with applied "
        f"migration(s): Honua.Server.Migrations.{STEM}.sql, Honua.Server.Migrations.{OTHER}.sql. "
        "Forward-compatibility declaration(s) refused: "
        f"Honua.Server.Migrations.{STEM}.sql ({PHRASE})."
    )
    assert boundary_for(refusal, note, {STEM: REVIEWED, OTHER: REVIEWED}) is None


@pytest.mark.parametrize("note", [
    "Restore across 142_CreateAiFinancialAccountingV2.",
    "See x142_CreateAiFinancialAccounting before the window.",
])
def test_a_different_identifier_does_not_document_the_stem(note):
    assert boundary_for(REFUSAL, note, {STEM: REVIEWED}) is None


@pytest.mark.parametrize("note", [
    f"`{STEM}`",
    f"See {STEM}.sql in the table.",
])
def test_markdown_and_sql_suffix_still_name_the_stem(note):
    assert boundary_for(REFUSAL, note, {STEM: REVIEWED}) == BOUNDARY


@pytest.mark.parametrize(("note_text", "expected"), [
    (f"Restore across {STEM}.\n", BOUNDARY),
    ("Restore the backup.\n", "no"),
])
def test_cli_grades_a_local_note(tmp_path, note_text, expected):
    assert _grade_local(tmp_path, note_text) == expected


def test_helm_and_compose_grade_a_refusal_through_this_module():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/gate-upgrade.yml").read_text(encoding="utf-8")
    harness = (root / "e2e/harness/upgrade-chaos.sh").read_text(encoding="utf-8")
    assert "tools/rollback_boundary.py" in workflow
    assert "tools/rollback_boundary.py" in harness
    assert "--refusal-file" in workflow and "--refusal-file" in harness
    graded = 'if [ "$decl" = reader-incompatible-declaration ]; then'
    assert graded in workflow and graded in harness


def test_cli_refuses_a_note_without_its_scripts(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("note\n", encoding="utf-8")
    refusal = tmp_path / "refusal.txt"
    refusal.write_text(REFUSAL, encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--refusal-file", str(refusal), "--note-file", str(note)])


def test_remote_grade_reads_raw_contents(tmp_path, monkeypatch):
    note = tmp_path / "remote-note.md"
    script = tmp_path / "remote.sql"
    note.write_text(f"Restore across {STEM}.\n", encoding="utf-8")
    script.write_text(REVIEWED, encoding="utf-8")
    log = _install_fake_gh(tmp_path, monkeypatch, (
        "url=$4\n"
        "case \"$url\" in\n"
        f"  *upgrade-and-rollback.md*) cat {note} ;;\n"
        f"  *{STEM}.sql*) cat {script} ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    ))
    refusal = tmp_path / "refusal.txt"
    refusal.write_text(REFUSAL, encoding="utf-8")
    captured = _cli(["--refusal-file", str(refusal), "--revision", "abc123"])
    assert captured == BOUNDARY
    recorded = log.read_text(encoding="utf-8")
    assert "--fail" not in recorded
    assert "Accept: application/vnd.github.raw" in recorded
    assert "repos/honua-io/honua-server/contents/" in recorded


def test_remote_grade_fails_closed_when_the_request_fails(tmp_path, monkeypatch):
    _install_fake_gh(tmp_path, monkeypatch, "exit 1\n")
    refusal = tmp_path / "refusal.txt"
    refusal.write_text(REFUSAL, encoding="utf-8")
    captured = _cli(["--refusal-file", str(refusal), "--revision", "abc123"])
    assert captured == "no"


def _grade_local(tmp_path: Path, note_text: str) -> str:
    note = tmp_path / "note.md"
    note.write_text(note_text, encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / f"{STEM}.sql").write_text(REVIEWED, encoding="utf-8")
    refusal = tmp_path / "refusal.txt"
    refusal.write_text(REFUSAL, encoding="utf-8")
    return _cli(["--refusal-file", str(refusal), "--note-file", str(note),
                  "--scripts-dir", str(scripts)])


def _cli(argv: list[str]) -> str:
    buffer = StringIO()
    with contextlib.redirect_stdout(buffer):
        assert main(argv) == 0
    return buffer.getvalue().strip()


def _install_fake_gh(tmp_path: Path, monkeypatch, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    fake = bin_dir / "gh"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" >> {log}\n"
        f"printf '\\n' >> {log}\n"
        "for arg in \"$@\"; do [ \"$arg\" = \"--fail\" ] && exit 2; done\n"
        f"{body}",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log
