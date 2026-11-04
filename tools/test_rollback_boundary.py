"""A refused reader-incompatible script is a boundary only when the note names it."""
import contextlib
from io import StringIO
from pathlib import Path

import pytest

from rollback_boundary import BOUNDARY, boundary_for, main, script_stems

STEM = "142_CreateAiFinancialAccounting"
REFUSAL = (
    "schema_reader_incompatible: applied migration "
    f"Honua.Server.Migrations.{STEM}.sql (declared reader-incompatible, not expand)."
)
REVIEWED = (
    "-- honua:reader-incompatible reason=the previous reader cannot parse the ledger\n"
    "-- honua:compatibility-review\n"
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
    refusal = REFUSAL + " schema_reader_incompatible: 144_ArcUnknownFuture.sql (declared reader-incompatible, not expand)."
    scripts = {STEM: REVIEWED, "144_ArcUnknownFuture": REVIEWED}
    assert boundary_for(refusal, note, scripts) is None


def test_cli_reads_the_local_note_and_script(tmp_path):
    note = tmp_path / "note.md"
    note.write_text(f"Restore across {STEM}.\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / f"{STEM}.sql").write_text(REVIEWED, encoding="utf-8")
    refusal = tmp_path / "refusal.txt"
    refusal.write_text(REFUSAL, encoding="utf-8")
    captured = _cli(["--refusal-file", str(refusal), "--note-file", str(note),
                     "--scripts-dir", str(scripts)])
    assert captured == BOUNDARY


def test_cli_prints_no_when_the_note_omits_the_stem(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("Restore the backup.\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / f"{STEM}.sql").write_text(REVIEWED, encoding="utf-8")
    refusal = tmp_path / "refusal.txt"
    refusal.write_text(REFUSAL, encoding="utf-8")
    captured = _cli(["--refusal-file", str(refusal), "--note-file", str(note),
                     "--scripts-dir", str(scripts)])
    assert captured == "no"


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


def _cli(argv: list[str]) -> str:
    buffer = StringIO()
    with contextlib.redirect_stdout(buffer):
        assert main(argv) == 0
    return buffer.getvalue().strip()
