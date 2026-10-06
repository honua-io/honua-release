"""Third-party trademark classifier and lint (honua-release#425)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_vendor_terms as vt  # noqa: E402

ATTRIBUTION = ("Esri, ArcGIS, and the Esri product names used here are trademarks, registered trademarks, "
               "or service marks of Esri in the United States and other countries. They identify the "
               "products Honua is compatible with; Honua is not affiliated with, sponsored by, or endorsed "
               "by Esri.")
VOCABULARY = vt.load_vocabulary()
INTEROP = vt.load_interop()
# The R30 confidential terms, assembled so this file's source carries none of them literally (a confidential
# hit can be neither baselined nor allowlisted, so the tests cannot carry them either).
PRO = vt.DESKTOP_CLIENT_NAME
SCRIPTING = "arc" "py"
PROJECT, TOOLBOX, PYTHON_TOOLBOX = ".ap" "rx", ".at" "bx", ".p" "yt"
INSTALL_PATH = "C:\\Program Files\\" "ArcGIS" "\\Pro\\bin\\Python\\python.exe"
LICENCE_VARIABLE, LICENCE_SERVER = "ESRI" "_LICENSE_HOST", "2700" "0@lic.example.test"
CLIENT_CLASS = "ArcGis" "ProLane"
LICENCE_MANAGER = "ArcGis" "LicenseManager"


def classify(path: str, text: str, allowlist=()) -> list[vt.Hit]:
    return vt.Classifier("honua-test", VOCABULARY, list(allowlist)).classify_text(path, text)


def only(hits: list[vt.Hit], token: str) -> vt.Hit:
    matching = [hit for hit in hits if hit.token == token]
    assert len(matching) == 1, [hit.as_dict() for hit in hits]
    return matching[0]


# ------------------------------------------------------------------------------------------ the classes

def test_spec_identifier_is_spec():
    hit = only(classify("src/Query/GeometryType.cs", 'var geometryType = "esriGeometryPoint";'),
               "esriGeometryPoint")
    assert (hit.cls, hit.category) == ("spec", "spec-identifier")


def test_compatibility_sentence_with_attribution_is_nominative():
    hits = classify("docs/clients.md", f"Honua works with {PRO} 3.3.\n\n{ATTRIBUTION}\n")
    hit = next(hit for hit in hits if hit.line == 1)
    assert (hit.token, hit.term, hit.cls, hit.category) == (PRO, PRO, "nominative", "compatibility-statement")
    assert {(hit.cls, hit.category) for hit in hits if hit.line == 3} == {("nominative", "attribution")}


def test_same_sentence_without_attribution_is_avoidable():
    hit = only(classify("docs/clients.md", "Honua works with ArcGIS Online.\n"), "ArcGIS Online")
    assert (hit.cls, hit.category) == ("avoidable", "docs-copy")


def test_the_desktop_client_without_attribution_is_confidential():
    hit = only(classify("docs/clients.md", f"Honua works with {PRO} 3.3.\n"), PRO)
    assert (hit.cls, hit.category) == ("confidential", "desktop-client")


def test_class_named_after_mark_is_avoidable():
    hit = only(classify("src/Layers/EsriFeatureLayerAdapter.cs",
                        "public sealed class EsriFeatureLayerAdapter : ILayerAdapter"),
               "EsriFeatureLayerAdapter")
    assert (hit.cls, hit.category) == ("avoidable", "identifier")


# ------------------------------------------------------------------------------------------ nominative limits

def test_heading_that_leads_with_the_mark_is_avoidable_even_when_attributed():
    hit = only(classify("docs/clients.md", f"## ArcGIS Online support\n\n{ATTRIBUTION}\n"), "ArcGIS Online")
    assert hit.cls == "avoidable"
    # the desktop client leading a heading is not a compatibility statement, so it is confidential
    hit = only(classify("docs/clients.md", f"## {PRO} support\n\n{ATTRIBUTION}\n"), PRO)
    assert (hit.cls, hit.category) == ("confidential", "desktop-client")


@pytest.mark.parametrize("path,text", [
    ("site/clients.html", "<h1>ArcGIS Online works with Honua</h1>"),
    ("site/clients.html", "<title>ArcGIS Online works with Honua</title>"),
    ("site/clients.html", '<h2 class="hero"><strong>ArcGIS Online</strong> works with Honua</h2>'),
    ("docs/clients.adoc", "== ArcGIS Online works with Honua"),
    ("docs/clients.rst", "ArcGIS Online works with Honua\n==============================="),
    ("docs/clients.md", "ArcGIS Online works with Honua\n---"),
])
def test_headings_in_every_prose_format_that_lead_with_the_mark_are_avoidable(path, text):
    hit = only(classify(path, f"{text}\n\n{ATTRIBUTION}\n"), "ArcGIS Online")
    assert hit.cls == "avoidable"


def test_compatibility_sentences_that_are_not_headings_stay_nominative():
    for path, text in [("site/clients.html", f"<p>{PRO} works with Honua.</p>"),
                       ("docs/clients.rst", f"{PRO} works with Honua.\nIt loads layers as well."),
                       ("docs/clients.md", f"{PRO} works with Honua.\n\n---")]:
        assert only(classify(path, f"{text}\n\n{ATTRIBUTION}\n"), PRO).cls == "nominative", path


def test_endorsement_language_is_avoidable_even_when_attributed():
    hit = only(classify("docs/clients.md", f"Honua is an official ArcGIS Online partner.\n\n{ATTRIBUTION}\n"),
               "ArcGIS Online")
    assert (hit.cls, hit.category) == ("avoidable", "endorsement-claim")


@pytest.mark.parametrize("claim, token", [
    ("Honua is endorsed by Esri.", "Esri"),
    ("Honua works with ArcGIS Online as an Esri partner.", "Esri"),
    ("Honua is affiliated with Esri and sponsored by ArcGIS Online.", "ArcGIS Online"),
])
def test_endorsement_claims_are_never_nominative_even_in_an_attributed_file(claim, token):
    # the endorsement check runs before the attribution and compatibility checks, on every line
    hits = classify("docs/clients.md", f"{claim}\n\n{ATTRIBUTION}\n")
    claimed = [hit for hit in hits if hit.line == 1]
    assert claimed and {(hit.cls, hit.category) for hit in claimed} == {("avoidable", "endorsement-claim")}
    assert only(claimed, token)
    # the notice's own disclaimer is not a claim
    assert {(hit.cls, hit.category) for hit in hits if hit.line == 3} == {("nominative", "attribution")}


@pytest.mark.parametrize("denial", [
    "Honua is not affiliated with, endorsed by, or sponsored by Esri.",
    "These runs are not official Esri certification evidence.",
    "Honua has never been endorsed by Esri nor sponsored by it.",
])
def test_a_denial_is_not_an_endorsement_claim(denial):
    assert only(classify("docs/a.md", denial + "\n"), "Esri").category == "docs-copy"


def test_a_claim_next_to_a_denial_is_still_a_claim():
    hit = only(classify("docs/a.md", "Honua is not sponsored by Esri but is an official partner of it.\n"), "Esri")
    assert hit.category == "endorsement-claim"


def test_a_claim_appended_to_the_notice_is_still_a_claim():
    hits = classify("docs/clients.md", f"{ATTRIBUTION} ArcGIS Online is endorsed by Esri.\n")
    assert only(hits, "ArcGIS Online").category == "endorsement-claim"


def test_a_wrapped_notice_is_the_attribution_not_a_claim():
    wrapped = ("> Esri, ArcGIS, and the Esri product names used here are trademarks, registered trademarks, or\n"
               "> service marks of Esri in the United States and other countries. They identify the products\n"
               "> Honua is compatible with; Honua is not affiliated with, sponsored by, or endorsed\n"
               "> by Esri.\n")
    hits = classify("docs/clients.md", f"Honua works with ArcGIS Online.\n\n{wrapped}")
    assert only(hits, "ArcGIS Online").cls == "nominative"
    assert {(hit.cls, hit.category) for hit in hits if hit.line > 1} == {("nominative", "attribution")}


def test_mark_inside_a_compound_name_is_never_nominative():
    hits = classify("docs/clients.md", f"Honua works with the esri-compat harness.\n\n{ATTRIBUTION}\n")
    hit = only(hits, "esri")
    assert (hit.cls, hit.category) == ("avoidable", "repo-or-package-name")


def test_compatibility_matrix_rows_are_nominative_when_the_file_is_attributed():
    text = f"# {ATTRIBUTION}\nclients:\n  - name: {PRO}\n    versions: [\"3.3\"]\n  - name: ArcGIS Online\n"
    hits = classify("matrix/clients.yaml", text)
    assert (only(hits, PRO).cls, only(hits, PRO).category) == ("nominative", "label")
    assert only(hits, "ArcGIS Online").cls == "nominative"
    unstamped = classify("matrix/clients.yaml", f"clients:\n  - name: {PRO}\n  - name: ArcGIS Online\n")
    assert only(unstamped, "ArcGIS Online").cls == "avoidable"
    assert (only(unstamped, PRO).cls, only(unstamped, PRO).category) == ("confidential", "desktop-client")


def test_a_matrix_or_compatibility_path_alone_makes_nothing_nominative():
    # the attribution must be in the file (and, for data, in its header); the path is no hint
    for path in ("compatibility-matrix.yaml", "docs/compatibility/clients.yaml", "matrix/clients.json"):
        assert only(classify(path, "clients:\n  - name: ArcGIS Online\n"), "ArcGIS Online").cls == "avoidable"
    trailing = f"clients:\n  - name: ArcGIS Online\n# {ATTRIBUTION}\n"
    assert only(classify("compatibility-matrix.yaml", trailing), "ArcGIS Online").cls == "avoidable"
    prose = f"Choose ArcGIS Online today.\n\n{ATTRIBUTION}\n"
    assert only(classify("docs/compatibility-matrix.md", prose), "ArcGIS Online").cls == "avoidable"


def test_a_matrix_page_exempts_only_its_table_rows():
    text = f"| Client | Versions |\n| --- | --- |\n| {PRO} | 3.3 |\n\nChoose ArcGIS Online today.\n\n{ATTRIBUTION}\n"
    for path in ("docs/matrix-notes.md", "docs/clients.md"):
        hits = classify(path, text)
        assert only(hits, PRO).cls == "nominative", path
        assert only(hits, "ArcGIS Online").cls == "avoidable", path
    html = f"<table><tr><td>{PRO}</td><td>3.3</td></tr></table>\n<p>Choose ArcGIS Online.</p>\n{ATTRIBUTION}\n"
    hits = classify("site/compatibility.html", html)
    assert only(hits, PRO).cls == "nominative"
    assert only(hits, "ArcGIS Online").cls == "avoidable"
    # a cell that is not just the product is not a label
    hits = classify("docs/clients.md", f"| Buy ArcGIS Online now | x |\n\n{ATTRIBUTION}\n")
    assert only(hits, "ArcGIS Online").cls == "avoidable"


def test_fenced_code_in_docs_is_not_prose():
    text = f"Honua works with {PRO}.\n\n```bash\npip install honua-esri-assess\n```\n\n{ATTRIBUTION}\n"
    hits = classify("docs/clients.md", text)
    assert only(hits, PRO).cls == "nominative"
    assert (only(hits, "esri").category) == "repo-or-package-name"


# ------------------------------------------------------------------------------------------ categories

@pytest.mark.parametrize("path, line, token, category", [
    ("src/Tiles/Cache.cs", "// mirrors the Esri tile scheme", "Esri", "comment"),
    ("src/server.py", "x = 1  # arcgis instance root", "arcgis", "comment"),
    ("src/Honua.Esri/Startup.cs", "namespace Honua.Esri.Compat;", "Esri", "namespace-or-import"),
    ("tests/test_layers.py", "def test_esri_layer_roundtrip():", "test_esri_layer_roundtrip", "test-name"),
    ("web/map.ts", 'import { featureLayer } from "esri-leaflet";', "esri", "third-party-reference"),
    ("tools/portal.py", "from arcgis.gis import GIS", "arcgis", "third-party-reference"),
    ("src/routes.ts", 'const root = "https://example.test/arcgis/rest/services";', "arcgis", "url"),
    ("src/routes.ts", 'const lane = "esri-dotnet";', "esri", "string-literal"),
    ("data/clients.json", '{"client": "ArcGIS Maps SDK for .NET"}', "ArcGIS Maps SDK for .NET",
     "product-name-label"),
    ("package.json", '  "name": "@honua/esri-tools",', "esri", "repo-or-package-name"),
])
def test_avoidable_categories(path, line, token, category):
    hit = only(classify(path, line), token)
    assert (hit.cls, hit.category) == ("avoidable", category)


def test_block_comments_span_lines():
    hits = classify("src/a.cs", "/*\n * Esri parity notes\n */\nvar a = 1;\n")
    assert only(hits, "Esri").category == "comment"


def test_marks_only_count_where_a_word_starts():
    assert classify("src/a.cs", "var SourceSrid = 4326; // Desrire") == []
    assert only(classify("src/a.cs", "bool isEsriLayer = true;"), "isEsriLayer").cls == "avoidable"


def test_product_names_without_either_substring_are_found():
    hit = only(classify("docs/a.md", "Open the map in ArcMap.\n"), "ArcMap")
    assert (hit.term, hit.cls) == ("ArcMap", "avoidable")


def test_path_components_count_once_per_prefix():
    files = [("tests/esri-leaflet/a.spec.ts", b"ok\n"), ("tests/esri-leaflet/b.spec.ts", b"ok\n")]
    hits, _, scanned = vt.scan(files, "honua-test", VOCABULARY, [])
    assert scanned == 2
    assert [(hit.path, hit.line, hit.category) for hit in hits] == [("tests/esri-leaflet", 0, "path")]


def test_every_mark_in_one_path_component_is_a_hit():
    files = [("esri-arcgis-tools/a.txt", b"ok\n"), ("esri-arcgis-tools/b.txt", b"ok\n")]
    hits, _, _ = vt.scan(files, "honua-test", VOCABULARY, [])
    assert [(hit.path, hit.token) for hit in hits] == [("esri-arcgis-tools", "esri"),
                                                      ("esri-arcgis-tools", "arcgis")]


def test_large_text_files_are_scanned():
    data = b"x = 1\n" * (4 * 1024 * 1024) + b"var layer = new EsriLayer();\n"
    hits, skipped, scanned = vt.scan([("src/big.cs", data)], "honua-test", VOCABULARY, [])
    assert (scanned, dict(skipped)) == (1, {})
    assert [(hit.line, hit.token) for hit in hits] == [(4 * 1024 * 1024 + 1, "EsriLayer")]


def test_symlink_names_are_classified_without_following_the_target(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "notes.txt").write_text("ok\n")
    (repo / "EsriDocs").symlink_to("notes.txt")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    for files in (vt.working_tree_files(repo), vt.git_ref_files(repo, "HEAD")):
        hits, skipped, scanned = vt.scan(files, "honua-test", VOCABULARY, [])
        assert [(hit.path, hit.category) for hit in hits] == [("EsriDocs", "path")]
        assert (dict(skipped), scanned) == ({"symlink": 1}, 1)


def test_generated_binary_and_bundled_files_are_skipped_but_counted():
    bundle = b"import{a as esriGeometryToGeoJSON}from'./x.js';" + b"var a=1;" * 2000
    files = [("package-lock.json", b'"esri-leaflet": {}'), ("logo.png", b"\x89PNG\0esri"),
             ("site/assets/app-3f2a.js", bundle)]
    hits, skipped, scanned = vt.scan(files, "honua-test", VOCABULARY, [])
    assert (hits, dict(skipped), scanned) == ([], {"generated": 1, "binary": 1, "minified": 1}, 0)


# ------------------------------------------------------------------------------------------ allowlist

def _allowlist(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps({"schema": vt.ALLOWLIST_SCHEMA, "entries": entries}))
    return path


def test_allowlist_entry_needs_a_reason_and_an_owner(tmp_path):
    with pytest.raises(SystemExit, match="reason, owner"):
        vt.load_allowlist(_allowlist(tmp_path, [{"repo": "honua-test", "paths": ["src/**"]}]))


def test_allowlisted_avoidable_hits_are_excepted_and_do_not_count(tmp_path):
    allowlist = vt.load_allowlist(_allowlist(tmp_path, [
        {"repo": "honua-test", "paths": ["src/legacy/**"], "reason": "wire-compat shim", "owner": "@owner"}]))
    files = [("src/legacy/Shim.cs", b"class EsriShim {}\n"), ("src/New.cs", b"class EsriNew {}\n")]
    hits, skipped, scanned = vt.scan(files, "honua-test", VOCABULARY, allowlist)
    report = vt.build_report("honua-test", "0" * 40, hits, skipped, scanned)
    assert report["counts"]["avoidable"] == 2
    assert report["counts"]["avoidableExcepted"] == 1
    assert report["avoidableByFile"] == {"src/New.cs": 1}


def test_glob_match():
    assert vt.glob_match("tools/vendor-terms/**", "tools/vendor-terms/reports/a.md")
    assert vt.glob_match("**/*.md", "README.md")
    assert not vt.glob_match("src/*.cs", "src/a/b.cs")


# ------------------------------------------------------------------------------------------ lint + baseline

def _report(files: dict[str, int]) -> dict:
    avoidable = [{"path": path, "line": n + 1, "token": "Esri", "category": "identifier"}
                 for path, count in files.items() for n in range(count)]
    return {"repo": "honua-test", "avoidableByFile": files, "avoidable": avoidable}


def _baseline(files: dict[str, int]) -> dict:
    return {"schema": vt.BASELINE_SCHEMA, "repo": "honua-test", "sha": "0" * 40,
            "total": sum(files.values()), "files": files}


def test_lint_passes_at_or_below_the_baseline():
    assert vt.lint(_report({"src/a.cs": 2}), _baseline({"src/a.cs": 2, "src/b.cs": 1})) == []


def test_a_new_avoidable_use_fails_the_lint():
    problems = vt.lint(_report({"src/a.cs": 2, "src/new.cs": 1}), _baseline({"src/a.cs": 2}))
    assert len(problems) == 2
    assert problems[0].startswith("src/new.cs: 1 avoidable vendor-term uses, baseline allows 0")
    assert "src/new.cs:1  identifier  Esri" in problems[0]


def test_a_baseline_that_grows_fails():
    problems = vt.baseline_growth(_baseline({"src/a.cs": 3}), _baseline({"src/a.cs": 2}))
    assert problems[0] == "baseline.honua-test: total grew 2 -> 3"
    assert problems[1] == "baseline.honua-test: src/a.cs grew 2 -> 3"


def test_a_baseline_that_moves_counts_to_a_new_file_fails():
    problems = vt.baseline_growth(_baseline({"src/a.cs": 1, "src/b.cs": 1}), _baseline({"src/a.cs": 2}))
    assert [p.split(";")[0] for p in problems] == ["baseline.honua-test: src/b.cs is a new entry (1)"]


def test_a_net_shrink_does_not_buy_growth_in_another_entry():
    # the total shrinks by one, but 98 uses move to another file
    problems = vt.baseline_growth(_baseline({"src/old.cs": 1, "src/new.cs": 98}), _baseline({"src/old.cs": 100}))
    assert [p.split(";")[0] for p in problems] == ["baseline.honua-test: src/new.cs is a new entry (98)"]
    problems = vt.baseline_growth(_baseline({"src/new.cs": 98}), _baseline({"src/old.cs": 100}))
    assert [p.split(";")[0] for p in problems] == ["baseline.honua-test: src/new.cs is a new entry (98)"]
    # between existing entries too: the total holds but one entry grows
    problems = vt.baseline_growth(_baseline({"src/a.cs": 1, "src/b.cs": 3}), _baseline({"src/a.cs": 2, "src/b.cs": 2}))
    assert problems == ["baseline.honua-test: src/b.cs grew 2 -> 3"]


def test_a_baseline_that_shrinks_passes():
    assert vt.baseline_growth(_baseline({"src/a.cs": 1}), _baseline({"src/a.cs": 2})) == []
    assert vt.baseline_growth(_baseline({"src/a.cs": 1}), _baseline({"src/a.cs": 2, "src/b.cs": 4})) == []


def test_a_rename_is_a_new_entry_and_fails():
    # no redistribution at all: a renamed file carries its uses only once they are gone
    problems = vt.baseline_growth(_baseline({"src/Layer.cs": 2}), _baseline({"src/EsriLayer.cs": 3}))
    assert [p.split(";")[0] for p in problems] == ["baseline.honua-test: src/Layer.cs is a new entry (2)"]
    problems = vt.baseline_growth(_baseline({"tests/leaflet/a.ts": 1}),
                                  _baseline({"tests/esri-leaflet/a.ts": 1, "tests/esri-leaflet": 1}))
    assert [p.split(";")[0] for p in problems] == ["baseline.honua-test: tests/leaflet/a.ts is a new entry (1)"]


def test_a_baseline_total_must_be_the_sum_of_its_entries():
    tampered = _baseline({"src/a.cs": 1}) | {"total": 50}
    assert vt.baseline_growth(tampered, None) == [
        "baseline.honua-test: total 50 is not the sum of its entries (1)"]


# ------------------------------------------------------------------------------------------ CLI end to end

def _git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.test", "PATH": "/usr/bin:/bin", "HOME": str(cwd)}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=env).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "Query.cs").write_text('var t = "esriGeometryPoint"; // Esri parity\n')
    (repo / ".gitignore").write_text("out/\n")
    (repo / "out").mkdir()
    (repo / "out" / "EsriBuild.cs").write_text("class EsriBuild {}\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    return repo


def test_lint_cli_fails_on_a_new_avoidable_use(tmp_path, capsys):
    repo = _repo(tmp_path)
    baseline = tmp_path / "baseline.json"
    common = ["--root", str(repo), "--repo", "honua-test", "--allowlist", ""]
    assert vt.main(["baseline", *common, "--out", str(baseline)]) == 0
    assert json.loads(baseline.read_text())["files"] == {"src/Query.cs": 1}  # .gitignore'd out/ unscanned
    assert vt.main(["lint", *common, "--baseline", str(baseline)]) == 0

    (repo / "src" / "Layer.cs").write_text("public class EsriLayer {}\n")
    capsys.readouterr()
    assert vt.main(["lint", *common, "--baseline", str(baseline)]) == 1
    assert "src/Layer.cs:1  identifier  EsriLayer" in capsys.readouterr().out


def test_scan_cli_reads_a_commit_without_checking_it_out(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "Query.cs").write_text("// uncommitted EsriEdit\n")
    report_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"
    assert vt.main(["scan", "--root", str(repo), "--repo", "honua-test", "--allowlist", "", "--git-ref",
                    "HEAD", "--json", str(report_path), "--markdown", str(markdown_path)]) == 0
    report = json.loads(report_path.read_text())
    assert report["counts"] == {"spec": 1, "spec-interop": 0, "nominative": 0, "avoidable": 1, "confidential": 0,
                                "avoidableExcepted": 0}
    assert report["avoidable"][0]["token"] == "Esri"
    assert "src/Query.cs:1  comment  Esri" in markdown_path.read_text()


def test_check_baselines_cli_fails_when_a_committed_baseline_grows(tmp_path):
    root = tmp_path / "release"
    (root / "tools" / "vendor-terms").mkdir(parents=True)
    baseline = root / "tools" / "vendor-terms" / "baseline.honua-test.json"
    baseline.write_text(json.dumps(_baseline({"src/a.cs": 2})))
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seed")
    assert vt.main(["check-baselines", "--base-ref", "HEAD", "--repo-root", str(root)]) == 0
    baseline.write_text(json.dumps(_baseline({"src/a.cs": 3})))
    assert vt.main(["check-baselines", "--base-ref", "HEAD", "--repo-root", str(root)]) == 1
    baseline.write_text(json.dumps(_baseline({"src/a.cs": 1})))
    assert vt.main(["check-baselines", "--base-ref", "HEAD", "--repo-root", str(root)]) == 0


# ------------------------------------------------------------------------------------------ vocabulary

def test_every_vocabulary_identifier_cites_its_source_section():
    document = json.loads(vt.VOCABULARY.read_text())
    assert document["jsonKeys"] == [] and document["pathSegments"] == []
    for entry in document["identifiers"]:
        assert entry["source"] in document["sources"], entry
        assert entry["section"] and entry["sectionTitle"], entry
    assert VOCABULARY["esriFieldTypeOID"]["source"] == "gsr-1.0"
    assert (VOCABULARY["esriFieldTypeOID"]["section"], VOCABULARY["esriFieldTypeOID"]["sectionTitle"]) == (
        "4.2.3", "Layer/Table Example")
    assert VOCABULARY["esriSpatialRelIntersects"]["section"] == "4.2.4.2"
    assert set(document["sources"]) == {"gsr-1.0"}
    assert all(entry["source"] == "gsr-1.0" for entry in document["identifiers"])
    assert "esriFieldTypeGUID" not in VOCABULARY  # later ArcGIS REST, not GSR 1.0 (R36)
    assert "arcgis" not in {entry["identifier"].lower() for entry in document["identifiers"]}


def test_policy_carries_the_attribution_the_classifier_and_these_tests_use():
    policy = (vt.ROOT / "docs" / "THIRD-PARTY-TRADEMARKS.md").read_text()
    quoted = " ".join(line.lstrip("> ").strip() for line in policy.splitlines() if line.startswith(">"))
    assert quoted == ATTRIBUTION
    assert vt.is_attributed(policy)


def test_committed_allowlist_loads():
    entries = vt.load_allowlist(vt.ALLOWLIST)
    assert entries and all(entry.reason and entry.owner for entry in entries)


# ------------------------------------------------------------------------------------------ confidential (R30)

@pytest.mark.parametrize("path, line, token, category", [
    ("tools/replay.py", f"import {SCRIPTING}", SCRIPTING, "desktop-scripting"),
    ("tools/replay.py", f"{SCRIPTING}.mp.ArcGISProject(path)", SCRIPTING, "desktop-scripting"),
    ("src/Scan.py", f"class {SCRIPTING.title()}Scanner:", f"{SCRIPTING.title()}Scanner", "desktop-scripting"),
    ("docs/howto.md", f"Run the {SCRIPTING} replay.\n\n{ATTRIBUTION}", SCRIPTING, "desktop-scripting"),
    ("src/Load.cs", f'var project = "maps/site{PROJECT}";', PROJECT, "desktop-file"),
    ("src/Load.cs", f'var tools = "gp/Tools{TOOLBOX}";', TOOLBOX, "desktop-file"),
    ("README.md", f"Open Replay{PYTHON_TOOLBOX} first.", PYTHON_TOOLBOX, "desktop-file"),
    ("src/Runner.cs", f'var py = @"{INSTALL_PATH}";', "ArcGIS", "runner-detail"),
    ("env/runner.env", f"{LICENCE_VARIABLE}={LICENCE_SERVER}", LICENCE_VARIABLE, "runner-detail"),
    ("env/runner.env", f"LM_HOST={LICENCE_SERVER}", LICENCE_SERVER, "runner-detail"),
    ("src/Lic.cs", f"var m = new {LICENCE_MANAGER}();", LICENCE_MANAGER, "runner-detail"),
    (".github/workflows/x.yml", "    runs-on: [self-hosted, windows, esri-desktop]", "esri", "runner-detail"),
    ("src/Lanes.cs", f"public sealed class {CLIENT_CLASS} {{}}", CLIENT_CLASS, "desktop-client"),
    ("src/Lanes.cs", f'const string Client = "{PRO}";', PRO, "desktop-client"),
    ("tests/test_lanes.py", f'LANE = "{PRO.lower().replace(" ", "-")}"', "arcgis", "desktop-client"),
])
def test_every_confidential_pattern_is_detected(path, line, token, category):
    hit = only(classify(path, line), token)
    assert (hit.cls, hit.category) == ("confidential", category)


@pytest.mark.parametrize("line", [
    "import pytest  # .pytest_cache stays ignored",
    "Honua ArcGIS Professional Services",
    "var SourceSrid = 4326;",
    "public record EsriSampleLicenseMetadata(string Attribution);",
])
def test_near_misses_are_not_confidential(line):
    assert all(hit.cls != "confidential" for hit in classify("src/a.py", line))


def test_confidential_path_components_are_detected():
    files = [(f"scripts/{PRO.lower().replace(' ', '-')}/run.ps1", b"ok\n"), (f"maps/site{PROJECT}", b"ok\n")]
    hits, _, _ = vt.scan(files, "honua-test", VOCABULARY, [])
    assert {(hit.path, hit.cls, hit.category) for hit in hits} == {
        (f"scripts/{PRO.lower().replace(' ', '-')}", "confidential", "desktop-client"),
        (f"maps/site{PROJECT}", "confidential", "desktop-file")}


def test_the_nominative_exception_requires_the_attribution_block():
    sentence = f"Honua works with {PRO} 3.3."
    assert only(classify("docs/c.md", f"{sentence}\n\n{ATTRIBUTION}\n"), PRO).cls == "nominative"
    first_clause_only = ATTRIBUTION.split(" They identify")[0]
    for text in (sentence, f"{sentence}\n\n{first_clause_only}\n"):
        hit = only(classify("docs/c.md", text), PRO)
        assert (hit.cls, hit.category) == ("confidential", "desktop-client")
    # attributed, but code, inline code or an endorsement claim is no compatibility statement
    for line in (f"`{PRO}` works with Honua.", f"Honua works with {PRO}, an official partner."):
        assert only(classify("docs/c.md", f"{line}\n\n{ATTRIBUTION}\n"), PRO).cls == "confidential"
    assert only(classify("src/c.py", f'# {ATTRIBUTION}\nCLIENT = "{PRO}"  # works with Honua\n'),
                PRO).cls == "confidential"


def test_a_confidential_hit_cannot_be_allowlisted(tmp_path):
    allowlist = vt.load_allowlist(_allowlist(tmp_path, [
        {"repo": "honua-test", "paths": ["tools/**"], "reason": "r", "owner": "@o"}]))
    files = [("tools/replay.py", f"import {SCRIPTING}\nclass EsriShim: pass\n".encode())]
    hits, skipped, scanned = vt.scan(files, "honua-test", VOCABULARY, allowlist)
    report = vt.build_report("honua-test", "0" * 40, hits, skipped, scanned)
    assert report["counts"]["confidential"] == 1 and report["counts"]["avoidableExcepted"] == 1
    assert report["confidentialByFile"] == {"tools/replay.py": 1}
    assert "exception" not in report["confidential"][0]


def test_a_confidential_hit_cannot_be_baselined(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "tools").mkdir()
    (repo / "tools" / "replay.py").write_text(f"import {SCRIPTING}\n")
    baseline = tmp_path / "baseline.json"
    common = ["--root", str(repo), "--repo", "honua-test", "--allowlist", "", "--known", ""]
    assert vt.main(["baseline", *common[:-2], "--out", str(baseline)]) == 0
    written = json.loads(baseline.read_text())
    assert "tools/replay.py" not in written["files"]  # avoidable uses only
    capsys.readouterr()
    assert vt.main(["lint", *common, "--baseline", str(baseline)]) == 1
    out = capsys.readouterr().out
    assert "confidential (R30) — never baselined, never allowlisted: tools/replay.py:1  desktop-scripting" in out
    # a hand-edited baseline listing the file does not help either
    baseline.write_text(json.dumps(written | {"files": written["files"] | {"tools/replay.py": 5},
                                               "total": written["total"] + 5}))
    assert vt.main(["lint", *common, "--baseline", str(baseline)]) == 1


def test_baselines_and_ledgers_refuse_confidential_paths():
    report = {"repo": "honua-test", "sha": "0" * 40, "avoidableByFile": {f"site{PROJECT}/a.md": 1},
              "confidentialByFile": {f"site{PROJECT}": 1}}
    with pytest.raises(SystemExit, match="confidential path"):
        vt.baseline_from(report)
    with pytest.raises(SystemExit, match="confidential path"):
        vt.known_from(report, "2026-10-03", ["honua-io/honua-test#1"])


def _known(files: dict[str, int]) -> dict:
    return {"schema": vt.KNOWN_SCHEMA, "repo": "honua-test", "sha": "0" * 40, "asOf": "2026-10-03",
            "burnDown": ["honua-io/honua-test#1"], "total": sum(files.values()), "files": files}


def test_known_confidential_uses_are_listed_and_fail_the_gate(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "tools").mkdir()
    (repo / "tools" / "replay.py").write_text(f"import {SCRIPTING}\n")
    baseline, known = tmp_path / "baseline.json", tmp_path / "known.json"
    common = ["--root", str(repo), "--repo", "honua-test", "--allowlist", "", "--baseline", str(baseline)]
    assert vt.main(["baseline", *common[:-2], "--out", str(baseline)]) == 0
    assert vt.main(["confidential-known", *common[:-2], "--as-of", "2026-10-03", "--burn-down",
                    "honua-io/honua-test#1", "--out", str(known)]) == 0
    assert json.loads(known.read_text())["files"] == {"tools/replay.py": 1}
    capsys.readouterr()
    # listed with its reason; the repository's own validate run passes on the ledger ...
    assert vt.main(["lint", *common, "--known", str(known)]) == 0
    out = capsys.readouterr().out
    assert "::warning::confidential (R30), known since 2026-10-03" in out and "honua-io/honua-test#1" in out
    # ... the reusable gate is red with the same reason
    assert vt.main(["lint", *common, "--known", str(known), "--fail-on-known-confidential"]) == 1
    assert "::error::confidential (R30), known since 2026-10-03" in capsys.readouterr().out
    # a confidential use the ledger does not list fails even the validate run
    (repo / "tools" / "replay.py").write_text(f"import {SCRIPTING}\nimport {SCRIPTING}.mp\n")
    assert vt.main(["lint", *common, "--known", str(known)]) == 1
    assert "never baselined, never allowlisted" in capsys.readouterr().out


def test_a_confidential_known_ledger_may_only_shrink(tmp_path):
    root = tmp_path / "release"
    (root / "tools" / "vendor-terms").mkdir(parents=True)
    ledger = root / "tools" / "vendor-terms" / "confidential-known.honua-test.json"
    ledger.write_text(json.dumps(_known({"a.json": 2})))
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seed")
    assert vt.main(["check-baselines", "--base-ref", "HEAD", "--repo-root", str(root)]) == 0
    for grown in ({"a.json": 3}, {"a.json": 1, "b.json": 1}):
        ledger.write_text(json.dumps(_known(grown)))
        assert vt.main(["check-baselines", "--base-ref", "HEAD", "--repo-root", str(root)]) == 1
    ledger.write_text(json.dumps(_known({"a.json": 1})))
    assert vt.main(["check-baselines", "--base-ref", "HEAD", "--repo-root", str(root)]) == 0


def test_a_ledger_needs_a_date_and_its_burn_down(tmp_path):
    path = tmp_path / "known.json"
    path.write_text(json.dumps(_known({}) | {"asOf": "soon"}))
    with pytest.raises(SystemExit, match="asOf date"):
        vt.load_known(path, "honua-test")


def test_the_committed_report_carries_no_confidential_detail():
    files = [("tools/replay.py", f"import {SCRIPTING}\n".encode()),
             (f"scripts/{PRO.lower().replace(' ', '-')}/EsriRun.ps1", b"$x = 'EsriLane'\n"),
             ("src/a.cs", b"class EsriA {}\n")]
    hits, skipped, scanned = vt.scan(files, "honua-test", VOCABULARY, [])
    markdown = vt.render_markdown(vt.build_report("honua-test", "0" * 40, hits, skipped, scanned))
    assert not vt.is_confidential_text(markdown)
    assert "| confidential (R30) | 2 |" in markdown and "src/a.cs:1  identifier  EsriA" in markdown
    assert "2 avoidable hits in 1 file whose path is itself confidential" in markdown


def test_this_tooling_carries_no_confidential_term():
    for path in ("tools/check_vendor_terms.py", "tools/test_check_vendor_terms.py", "docs/THIRD-PARTY-TRADEMARKS.md",
                 ".github/workflows/gate-vendor-terms.yml"):
        text = (vt.ROOT / path).read_text()
        hits = vt.Classifier("honua-release", VOCABULARY, []).classify_text(path, text)
        assert [hit.as_dict() for hit in hits if hit.cls == "confidential"] == [], path


# ------------------------------------------------------------------------------------------ interop vocabulary (R36)

def test_interop_identifiers_are_spec_interop_and_reported_separately():
    hits = classify("src/Fields.cs", 'var a = "esriFieldTypeGUID"; var b = "esriGeometryPoint";')
    assert (only(hits, "esriFieldTypeGUID").cls, only(hits, "esriFieldTypeGUID").category) == (
        "spec-interop", "interop-identifier")
    assert only(hits, "esriGeometryPoint").cls == "spec"
    report = vt.build_report("honua-test", "0" * 40, hits, vt.Counter(), 1)
    assert (report["counts"]["spec"], report["counts"]["spec-interop"]) == (1, 1)
    assert report["topInteropIdentifiers"] == {"esriFieldTypeGUID": 1}


@pytest.mark.parametrize("identifier, category", [
    ("esriGeometryNull", "non-wire-identifier"),
    ("esriSpatialRelWithinDistance", "non-wire-identifier"),
    ("esriSpatialRelBeyondDistance", "non-wire-identifier"),
    ("esriMosaicByAttribute", "wrong-wire-value"),
])
def test_identifiers_that_are_not_wire_values_are_flagged_for_renaming(identifier, category):
    hit = only(classify("src/Wire.cs", f'var v = "{identifier}";'), identifier)
    assert (hit.cls, hit.category) == ("avoidable", category)
    assert INTEROP.refused["esriMosaicByAttribute"]["expected"] == "esriMosaicAttribute"
    assert "esriMosaicAttribute" in VOCABULARY


def test_the_committed_interop_vocabulary_cites_every_entry():
    document = json.loads(vt.INTEROP.read_text())
    assert len(document["identifiers"]) == 76
    for entry in document["identifiers"]:
        assert entry["reference"].startswith("https://developers.arcgis.com/"), entry
        assert entry["family"] and entry["field"] and isinstance(entry["listedVerbatim"], bool), entry
    assert not set(INTEROP.identifiers) & set(VOCABULARY), "an identifier is in GSR 1.0 and interop both"
    assert "not GeoServices REST Specification 1.0" in document["description"]


def _interop(tmp_path: Path, identifiers: list[dict], refused=()) -> Path:
    path = tmp_path / "interop.json"
    path.write_text(json.dumps({"schema": vt.INTEROP_SCHEMA, "identifiers": identifiers, "refused": list(refused)}))
    return path


def test_the_interop_vocabulary_grows_only_with_a_cited_entry(tmp_path):
    cited = {"identifier": "esriNATravelDirectionFromFacility", "family": "travel direction",
             "field": "travelDirection",
             "reference": "https://developers.arcgis.com/rest/services-reference/enterprise/service-area/"}
    assert "esriNATravelDirectionFromFacility" in vt.load_interop(_interop(tmp_path, [cited])).identifiers
    for uncited in ({k: v for k, v in cited.items() if k != "reference"}, cited | {"reference": "see the docs"},
                    cited | {"reference": "https://example.test/esri"}, cited | {"field": ""}):
        with pytest.raises(SystemExit, match="cited entry"):
            vt.load_interop(_interop(tmp_path, [uncited]))
    with pytest.raises(SystemExit, match="expected wire value"):
        vt.load_interop(_interop(tmp_path, [], [{"identifier": "esriX", "category": "wrong-wire-value",
                                                 "reason": "r"}]))


# ------------------------------------------------------------------------------------------ certification labels (R37)

def test_stamped_certification_data_makes_product_labels_nominative():
    stamped = json.dumps({"schema": "honua.protocol-certification-requirements/v1", "trademarkNotice": ATTRIBUTION,
                          "requirements": [{"canonical_client": "ArcGIS Maps SDK for .NET"},
                                            {"canonical_client": PRO},
                                            {"canonical_client": f"{PRO}/{SCRIPTING}"}]}, indent=2)
    hits = classify("certification/requirements.json", stamped)
    assert (only(hits, "ArcGIS Maps SDK for .NET").cls, only(hits, "ArcGIS Maps SDK for .NET").category) == (
        "nominative", "certification-label")
    labels = [hit for hit in hits if hit.token == PRO]
    assert [(hit.cls, hit.category) for hit in labels] == [("nominative", "certification-label"),
                                                         ("confidential", "desktop-client")]
    assert only(hits, SCRIPTING).cls == "confidential"  # the label is allowed, the detail is not
    # the generator's schema marker counts outside certification/ too
    assert only(classify("docs/gis/data/requirements.json", stamped), "ArcGIS Maps SDK for .NET").category == \
        "certification-label"


def test_unstamped_certification_data_stays_avoidable():
    unstamped = json.dumps({"schema": "honua.protocol-certification-requirements/v1",
                            "requirements": [{"canonical_client": "ArcGIS Maps SDK for .NET"},
                                             {"canonical_client": PRO}]}, indent=2)
    hits = classify("certification/requirements.json", unstamped)
    assert (only(hits, "ArcGIS Maps SDK for .NET").cls, only(hits, "ArcGIS Maps SDK for .NET").category) == (
        "avoidable", "product-name-label")
    assert only(hits, PRO).cls == "confidential"
    # the notice outside the header (not a top-level trademarkNotice) is no stamp
    nested = json.dumps({"requirements": [{"note": ATTRIBUTION, "canonical_client": "ArcGIS Maps SDK for .NET"}]},
                        indent=2)
    assert only(classify("certification/requirements.json", nested), "ArcGIS Maps SDK for .NET").cls == "avoidable"


def test_a_stamped_yaml_certification_file_uses_its_header_comment():
    text = f"# {ATTRIBUTION}\nlanes:\n  - client: ArcGIS Maps SDK for .NET 200.x\n"
    assert only(classify("certification/lanes.yaml", text), "ArcGIS Maps SDK for .NET").category == \
        "certification-label"


# ------------------------------------------------------------------------------------------ allowlist policy

def test_wildcard_allowlist_entries_are_refused(tmp_path):
    for entry in ({"repo": "*", "paths": ["src/**"]}, {"repo": "honua-test", "paths": ["**"]},
                  {"repo": "honua-test", "paths": ["src/a.cs", "**/*"]}):
        with pytest.raises(SystemExit, match="wildcard"):
            vt.load_allowlist(_allowlist(tmp_path, [entry | {"reason": "r", "owner": "@o"}]))


def test_the_site_root_alias_is_allowlisted_only_in_the_two_route_tables():
    entries = [entry for entry in vt.load_allowlist(vt.ALLOWLIST) if entry.repo == "honua-server"]
    alias = next(entry for entry in entries if "site-root alias" in entry.reason or "/rest" in entry.reason)
    assert alias.tokens == ("arcgis",) and len(alias.paths) == 2
    assert all(path.endswith(".cs") and "*" not in path for path in alias.paths)
    assert alias.covers("honua-server", alias.paths[0], "arcgis")
    assert not alias.covers("honua-server", "src/Honua.Server/Other.cs", "arcgis")
    assert not alias.covers("honua-server", alias.paths[0], "Esri")


def test_the_recorded_leaflet_check_name_is_allowlisted_only_in_the_server_fixture():
    """The c19f29d fixture quotes a real Actions job name. The exception does not cover any other token or path."""
    path = "tools/fixtures/candidate-resolution-2026-10-06/honua-server-c19f29d.json"
    token = "Esri Leaflet"
    entries = [entry for entry in vt.load_allowlist(vt.ALLOWLIST) if entry.covers("honua-release", path, token)]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.tokens == (token,)
    assert entry.paths == (path,)
    assert entry.reason and entry.owner
    assert not entry.covers("honua-release", path, "ArcGIS")
    assert not entry.covers("honua-release", path, "esri")
    other = "tools/fixtures/candidate-resolution-2026-10-06/honua-sdk-js-984425f.json"
    assert not entry.covers("honua-release", other, token)


def test_the_gate_pins_its_tooling_and_fails_on_known_confidential_uses():
    workflow = (vt.ROOT / ".github" / "workflows" / "gate-vendor-terms.yml").read_text()
    assert "--fail-on-known-confidential" in workflow
    assert "^[0-9a-f]{40}$" in workflow and "required: true" in workflow.split("tools_ref:")[2].split("outputs:")[0]
    assert "trunk" not in workflow
