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
    hits = classify("docs/clients.md", f"Honua works with ArcGIS Pro 3.3.\n\n{ATTRIBUTION}\n")
    hit = next(hit for hit in hits if hit.line == 1)
    assert (hit.token, hit.term, hit.cls, hit.category) == (
        "ArcGIS Pro", "ArcGIS Pro", "nominative", "compatibility-statement")
    assert {(hit.cls, hit.category) for hit in hits if hit.line == 3} == {("nominative", "attribution")}


def test_same_sentence_without_attribution_is_avoidable():
    hit = only(classify("docs/clients.md", "Honua works with ArcGIS Pro 3.3.\n"), "ArcGIS Pro")
    assert (hit.cls, hit.category) == ("avoidable", "docs-copy")


def test_class_named_after_mark_is_avoidable():
    hit = only(classify("src/Layers/EsriFeatureLayerAdapter.cs",
                        "public sealed class EsriFeatureLayerAdapter : ILayerAdapter"),
               "EsriFeatureLayerAdapter")
    assert (hit.cls, hit.category) == ("avoidable", "identifier")


# ------------------------------------------------------------------------------------------ nominative limits

def test_heading_that_leads_with_the_mark_is_avoidable_even_when_attributed():
    hit = only(classify("docs/clients.md", f"## ArcGIS Pro support\n\n{ATTRIBUTION}\n"), "ArcGIS Pro")
    assert hit.cls == "avoidable"


@pytest.mark.parametrize("path,text", [
    ("site/clients.html", "<h1>ArcGIS Pro works with Honua</h1>"),
    ("site/clients.html", "<title>ArcGIS Pro works with Honua</title>"),
    ("site/clients.html", '<h2 class="hero"><strong>ArcGIS Pro</strong> works with Honua</h2>'),
    ("docs/clients.adoc", "== ArcGIS Pro works with Honua"),
    ("docs/clients.rst", "ArcGIS Pro works with Honua\n==========================="),
    ("docs/clients.md", "ArcGIS Pro works with Honua\n---"),
])
def test_headings_in_every_prose_format_that_lead_with_the_mark_are_avoidable(path, text):
    hit = only(classify(path, f"{text}\n\n{ATTRIBUTION}\n"), "ArcGIS Pro")
    assert hit.cls == "avoidable"


def test_compatibility_sentences_that_are_not_headings_stay_nominative():
    for path, text in [("site/clients.html", "<p>ArcGIS Pro works with Honua.</p>"),
                       ("docs/clients.rst", "ArcGIS Pro works with Honua.\nIt loads layers as well."),
                       ("docs/clients.md", "ArcGIS Pro works with Honua.\n\n---")]:
        assert only(classify(path, f"{text}\n\n{ATTRIBUTION}\n"), "ArcGIS Pro").cls == "nominative", path


def test_endorsement_language_is_avoidable_even_when_attributed():
    hit = only(classify("docs/clients.md", f"Honua is an official ArcGIS Online partner.\n\n{ATTRIBUTION}\n"),
               "ArcGIS Online")
    assert hit.cls == "avoidable"


def test_mark_inside_a_compound_name_is_never_nominative():
    hits = classify("docs/clients.md", f"Honua works with the esri-compat harness.\n\n{ATTRIBUTION}\n")
    hit = only(hits, "esri")
    assert (hit.cls, hit.category) == ("avoidable", "repo-or-package-name")


def test_compatibility_matrix_rows_are_nominative_when_the_file_is_attributed():
    text = f"# {ATTRIBUTION}\nclients:\n  - name: ArcGIS Pro\n    versions: [\"3.3\"]\n"
    hit = only(classify("matrix/clients.yaml", text), "ArcGIS Pro")
    assert hit.cls == "nominative"
    assert only(classify("matrix/clients.yaml", "clients:\n  - name: ArcGIS Pro\n"), "ArcGIS Pro").cls == \
        "avoidable"


def test_a_matrix_page_exempts_only_its_table_rows():
    text = f"| Client | Versions |\n| --- | --- |\n| ArcGIS Pro | 3.3 |\n\nChoose ArcGIS Online today.\n\n{ATTRIBUTION}\n"
    hits = classify("docs/matrix-notes.md", text)
    assert only(hits, "ArcGIS Pro").cls == "nominative"
    assert only(hits, "ArcGIS Online").cls == "avoidable"
    html = f"<table><tr><td>ArcGIS Pro</td><td>3.3</td></tr></table>\n<p>Choose ArcGIS Online.</p>\n{ATTRIBUTION}\n"
    hits = classify("site/compatibility.html", html)
    assert only(hits, "ArcGIS Pro").cls == "nominative"
    assert only(hits, "ArcGIS Online").cls == "avoidable"


def test_fenced_code_in_docs_is_not_prose():
    text = f"Honua works with ArcGIS Pro.\n\n```bash\npip install honua-esri-assess\n```\n\n{ATTRIBUTION}\n"
    hits = classify("docs/clients.md", text)
    assert only(hits, "ArcGIS Pro").cls == "nominative"
    assert (only(hits, "esri").category) == "repo-or-package-name"


# ------------------------------------------------------------------------------------------ categories

@pytest.mark.parametrize("path, line, token, category", [
    ("src/Tiles/Cache.cs", "// mirrors the Esri tile scheme", "Esri", "comment"),
    ("src/server.py", "x = 1  # arcgis instance root", "arcgis", "comment"),
    ("src/Honua.Esri/Startup.cs", "namespace Honua.Esri.Compat;", "Esri", "namespace-or-import"),
    ("tests/test_layers.py", "def test_esri_layer_roundtrip():", "test_esri_layer_roundtrip", "test-name"),
    ("web/map.ts", 'import { featureLayer } from "esri-leaflet";', "esri", "third-party-reference"),
    ("tools/replay.py", "import arcpy", "arcpy", "third-party-reference"),
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
    assert problems[1].startswith("baseline.honua-test: src/a.cs grew 2 -> 3;")


def test_a_baseline_that_moves_counts_without_a_rename_fails():
    problems = vt.baseline_growth(_baseline({"src/a.cs": 1, "src/b.cs": 1}), _baseline({"src/a.cs": 2}))
    assert [p.split(";")[0] for p in problems] == ["baseline.honua-test: src/b.cs grew 0 -> 1"]


def test_a_net_shrink_does_not_buy_growth_in_another_entry():
    # the total shrinks by one, but 98 uses move to a path that is not a rename of anything
    problems = vt.baseline_growth(_baseline({"src/old.cs": 1, "src/new.cs": 98}), _baseline({"src/old.cs": 100}))
    assert [p.split(";")[0] for p in problems] == ["baseline.honua-test: src/new.cs grew 0 -> 98"]
    # removing the old entry is not enough either: the new path must be the old one with marks renamed
    problems = vt.baseline_growth(_baseline({"src/new.cs": 98}), _baseline({"src/old.cs": 100}))
    assert [p.split(";")[0] for p in problems] == ["baseline.honua-test: src/new.cs grew 0 -> 98"]


def test_a_baseline_that_shrinks_passes_including_a_rename():
    assert vt.baseline_growth(_baseline({"src/a.cs": 1}), _baseline({"src/a.cs": 2})) == []
    # EsriLayer.cs -> Layer.cs: the path hit disappears, the remaining content hits move with the file
    assert vt.baseline_growth(_baseline({"src/Layer.cs": 2}), _baseline({"src/EsriLayer.cs": 3})) == []
    # a marked directory renamed: every file under it moves
    assert vt.baseline_growth(_baseline({"tests/leaflet/a.ts": 1, "tests/leaflet/b.ts": 2}),
                              _baseline({"tests/esri-leaflet/a.ts": 1, "tests/esri-leaflet/b.ts": 2,
                                         "tests/esri-leaflet": 1})) == []


def test_a_rename_may_not_carry_more_than_the_removed_entry():
    problems = vt.baseline_growth(_baseline({"src/Layer.cs": 3, "src/b.cs": 0}),
                                  _baseline({"src/EsriLayer.cs": 2, "src/b.cs": 1}))
    assert [p.split(";")[0] for p in problems] == ["baseline.honua-test: src/Layer.cs grew 0 -> 3"]


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
    assert report["counts"] == {"spec": 1, "nominative": 0, "avoidable": 1, "avoidableExcepted": 0}
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
    assert VOCABULARY["esriFieldTypeGUID"]["source"] == "wire-supplement"
    assert "arcgis" not in {entry["identifier"].lower() for entry in document["identifiers"]}


def test_policy_carries_the_attribution_the_classifier_and_these_tests_use():
    policy = (vt.ROOT / "docs" / "THIRD-PARTY-TRADEMARKS.md").read_text()
    quoted = " ".join(line.lstrip("> ").strip() for line in policy.splitlines() if line.startswith(">"))
    assert quoted == ATTRIBUTION
    assert vt.is_attributed(policy)


def test_committed_allowlist_loads():
    entries = vt.load_allowlist(vt.ALLOWLIST)
    assert entries and all(entry.reason and entry.owner for entry in entries)
