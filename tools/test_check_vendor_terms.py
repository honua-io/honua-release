"""Third-party trademark classifier and lint (honua-release#425)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_vendor_terms as vt  # noqa: E402

ATTRIBUTION = ("Es" "ri" ", " "Arc" "GIS" ", and the " "Es" "ri" " product names used here are trademarks, registered trademarks, "
               "or service marks of " "Es" "ri" " in the United States and other countries. They identify the "
               "products Honua is compatible with; Honua is not affiliated with, sponsored by, or endorsed "
               "by " "Es" "ri" ".")
VOCABULARY = vt.load_vocabulary()
INTEROP = vt.load_interop()
# The R30 confidential terms, assembled so this file's source carries none of them literally (a confidential
# hit can be neither baselined nor allowlisted, so the tests cannot carry them either).
PRO = vt.DESKTOP_CLIENT_NAME
SCRIPTING = "arc" "py"
MARK_A = "Es" "ri"
MARK_B = "Arc" "GIS"
PRODUCT_ONLINE = MARK_B + " Online"
PROJECT, TOOLBOX, PYTHON_TOOLBOX = ".ap" "rx", ".at" "bx", ".p" "yt"
INSTALL_PATH = "C:\\Program Files\\" "Arc" "GIS" "\\Pro\\bin\\Python\\python.exe"
LICENCE_VARIABLE, LICENCE_SERVER = "ES" "RI" "_LICENSE_HOST", "2700" "0@lic.example.test"
CLIENT_CLASS = "Arc" "Gis" "ProLane"
LICENCE_MANAGER = "Arc" "Gis" "LicenseManager"


def classify(path: str, text: str, allowlist=()) -> list[vt.Hit]:
    return vt.Classifier("honua-test", VOCABULARY, list(allowlist)).classify_text(path, text)


@pytest.mark.parametrize("prefix,suffix", [("", "Factory"), ("my", ""), ("", "_factory")])
def test_visual_workflow_mark_is_found_inside_identifiers_and_paths(prefix, suffix):
    identifier = prefix + "Model" + "Builder" + suffix
    classifier = vt.Classifier("honua-test", VOCABULARY, [])
    assert classifier.classify_text("source.py", f"name = {identifier}\n")
    assert classifier.classify_path(f"src/{identifier}.py")


def only(hits: list[vt.Hit], token: str) -> vt.Hit:
    matching = [hit for hit in hits if hit.token == token]
    assert len(matching) == 1, [hit.as_dict() for hit in hits]
    return matching[0]


# ------------------------------------------------------------------------------------------ the classes

def test_spec_identifier_is_spec():
    hit = only(classify("src/Query/GeometryType.cs", 'var geometryType = "' 'es' 'ri' 'GeometryPoint";'),
               "es" "ri" "GeometryPoint")
    assert (hit.cls, hit.category) == ("spec", "spec-identifier")


def test_compatibility_sentence_with_attribution_is_nominative():
    hits = classify("docs/clients.md", f"Honua works with {PRO} 3.3.\n\n{ATTRIBUTION}\n")
    hit = next(hit for hit in hits if hit.line == 1)
    assert (hit.token, hit.term, hit.cls, hit.category) == (PRO, PRO, "nominative", "compatibility-statement")
    assert {(hit.cls, hit.category) for hit in hits if hit.line == 3} == {("nominative", "attribution")}


def test_same_sentence_without_attribution_is_avoidable():
    hit = only(classify("docs/clients.md", "Honua works with " "Arc" "GIS" " Online.\n"), "Arc" "GIS" " Online")
    assert (hit.cls, hit.category) == ("avoidable", "docs-copy")


def test_the_desktop_client_without_attribution_is_confidential():
    hit = only(classify("docs/clients.md", f"Honua works with {PRO} 3.3.\n"), PRO)
    assert (hit.cls, hit.category) == ("confidential", "desktop-client")


def test_class_named_after_mark_is_avoidable():
    hit = only(classify("src/Layers/" "Es" "ri" "FeatureLayerAdapter.cs",
                        "public sealed class " "Es" "ri" "FeatureLayerAdapter : ILayerAdapter"),
               "Es" "ri" "FeatureLayerAdapter")
    assert (hit.cls, hit.category) == ("avoidable", "identifier")


# ------------------------------------------------------------------------------------------ nominative limits

def test_heading_that_leads_with_the_mark_is_avoidable_even_when_attributed():
    hit = only(classify("docs/clients.md", f"## {PRODUCT_ONLINE} support\n\n{ATTRIBUTION}\n"), PRODUCT_ONLINE)
    assert hit.cls == "avoidable"
    # the desktop client leading a heading is not a compatibility statement, so it is confidential
    hit = only(classify("docs/clients.md", f"## {PRO} support\n\n{ATTRIBUTION}\n"), PRO)
    assert (hit.cls, hit.category) == ("confidential", "desktop-client")


@pytest.mark.parametrize("path,text", [
    ("site/clients.html", "<h1>" "Arc" "GIS" " Online works with Honua</h1>"),
    ("site/clients.html", "<title>" "Arc" "GIS" " Online works with Honua</title>"),
    ("site/clients.html", '<h2 class="hero"><strong>' 'Arc' 'GIS' ' Online</strong> works with Honua</h2>'),
    ("docs/clients.adoc", "== " "Arc" "GIS" " Online works with Honua"),
    ("docs/clients.rst", "Arc" "GIS" " Online works with Honua\n==============================="),
    ("docs/clients.md", "Arc" "GIS" " Online works with Honua\n---"),
])
def test_headings_in_every_prose_format_that_lead_with_the_mark_are_avoidable(path, text):
    hit = only(classify(path, f"{text}\n\n{ATTRIBUTION}\n"), "Arc" "GIS" " Online")
    assert hit.cls == "avoidable"


def test_compatibility_sentences_that_are_not_headings_stay_nominative():
    for path, text in [("site/clients.html", f"<p>{PRO} works with Honua.</p>"),
                       ("docs/clients.rst", f"{PRO} works with Honua.\nIt loads layers as well."),
                       ("docs/clients.md", f"{PRO} works with Honua.\n\n---")]:
        assert only(classify(path, f"{text}\n\n{ATTRIBUTION}\n"), PRO).cls == "nominative", path


def test_endorsement_language_is_avoidable_even_when_attributed():
    hit = only(classify("docs/clients.md", f"Honua is an official {PRODUCT_ONLINE} partner.\n\n{ATTRIBUTION}\n"),
               "Arc" "GIS" " Online")
    assert (hit.cls, hit.category) == ("avoidable", "endorsement-claim")


@pytest.mark.parametrize("claim, token", [
    ("Honua is endorsed by " "Es" "ri" ".", "Es" "ri"),
    ("Honua works with " "Arc" "GIS" " Online as an " "Es" "ri" " partner.", "Es" "ri"),
    ("Honua is affiliated with " "Es" "ri" " and sponsored by " "Arc" "GIS" " Online.", "Arc" "GIS" " Online"),
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
    "Honua is not affiliated with, endorsed by, or sponsored by " "Es" "ri" ".",
    "These runs are not official " "Es" "ri" " certification evidence.",
    "Honua has never been endorsed by " "Es" "ri" " nor sponsored by it.",
])
def test_a_denial_is_not_an_endorsement_claim(denial):
    assert only(classify("docs/a.md", denial + "\n"), "Es" "ri").category == "docs-copy"


def test_a_claim_next_to_a_denial_is_still_a_claim():
    hit = only(classify("docs/a.md", "Honua is not sponsored by " "Es" "ri" " but is an official partner of it.\n"), "Es" "ri")
    assert hit.category == "endorsement-claim"


def test_a_claim_appended_to_the_notice_is_still_a_claim():
    hits = classify("docs/clients.md", f"{ATTRIBUTION} {PRODUCT_ONLINE} is endorsed by {MARK_A}.\n")
    assert only(hits, "Arc" "GIS" " Online").category == "endorsement-claim"


def test_a_wrapped_notice_is_the_attribution_not_a_claim():
    wrapped = ("> " "Es" "ri" ", " "Arc" "GIS" ", and the " "Es" "ri" " product names used here are trademarks, registered trademarks, or\n"
               "> service marks of " "Es" "ri" " in the United States and other countries. They identify the products\n"
               "> Honua is compatible with; Honua is not affiliated with, sponsored by, or endorsed\n"
               "> by " "Es" "ri" ".\n")
    hits = classify("docs/clients.md", f"Honua works with {PRODUCT_ONLINE}.\n\n{wrapped}")
    assert only(hits, "Arc" "GIS" " Online").cls == "nominative"
    assert {(hit.cls, hit.category) for hit in hits if hit.line > 1} == {("nominative", "attribution")}


def test_mark_inside_a_compound_name_is_never_nominative():
    hits = classify("docs/clients.md", f"Honua works with the {MARK_A.lower()}-compat harness.\n\n{ATTRIBUTION}\n")
    hit = only(hits, "es" "ri")
    assert (hit.cls, hit.category) == ("avoidable", "repo-or-package-name")


def test_compatibility_matrix_rows_are_nominative_when_the_file_is_attributed():
    text = f"# {ATTRIBUTION}\nclients:\n  - name: {PRO}\n    versions: [\"3.3\"]\n  - name: {PRODUCT_ONLINE}\n"
    hits = classify("matrix/clients.yaml", text)
    assert (only(hits, PRO).cls, only(hits, PRO).category) == ("nominative", "label")
    assert only(hits, "Arc" "GIS" " Online").cls == "nominative"
    unstamped = classify("matrix/clients.yaml", f"clients:\n  - name: {PRO}\n  - name: {PRODUCT_ONLINE}\n")
    assert only(unstamped, "Arc" "GIS" " Online").cls == "avoidable"
    assert (only(unstamped, PRO).cls, only(unstamped, PRO).category) == ("confidential", "desktop-client")


def test_a_matrix_or_compatibility_path_alone_makes_nothing_nominative():
    # the attribution must be in the file (and, for data, in its header); the path is no hint
    for path in ("compatibility-matrix.yaml", "docs/compatibility/clients.yaml", "matrix/clients.json"):
        assert only(classify(path, "clients:\n  - name: " "Arc" "GIS" " Online\n"), "Arc" "GIS" " Online").cls == "avoidable"
    trailing = f"clients:\n  - name: {PRODUCT_ONLINE}\n# {ATTRIBUTION}\n"
    assert only(classify("compatibility-matrix.yaml", trailing), "Arc" "GIS" " Online").cls == "avoidable"
    prose = f"Choose {PRODUCT_ONLINE} today.\n\n{ATTRIBUTION}\n"
    assert only(classify("docs/compatibility-matrix.md", prose), "Arc" "GIS" " Online").cls == "avoidable"


def test_a_matrix_page_exempts_only_its_table_rows():
    text = f"| Client | Versions |\n| --- | --- |\n| {PRO} | 3.3 |\n\nChoose {PRODUCT_ONLINE} today.\n\n{ATTRIBUTION}\n"
    for path in ("docs/matrix-notes.md", "docs/clients.md"):
        hits = classify(path, text)
        assert only(hits, PRO).cls == "nominative", path
        assert only(hits, "Arc" "GIS" " Online").cls == "avoidable", path
    html = f"<table><tr><td>{PRO}</td><td>3.3</td></tr></table>\n<p>Choose {PRODUCT_ONLINE}.</p>\n{ATTRIBUTION}\n"
    hits = classify("site/compatibility.html", html)
    assert only(hits, PRO).cls == "nominative"
    assert only(hits, "Arc" "GIS" " Online").cls == "avoidable"
    # a cell that is not just the product is not a label
    hits = classify("docs/clients.md", f"| Buy {PRODUCT_ONLINE} now | x |\n\n{ATTRIBUTION}\n")
    assert only(hits, "Arc" "GIS" " Online").cls == "avoidable"


def test_fenced_code_in_docs_is_not_prose():
    text = f"Honua works with {PRO}.\n\n```bash\npip install honua-{MARK_A.lower()}-assess\n```\n\n{ATTRIBUTION}\n"
    hits = classify("docs/clients.md", text)
    assert only(hits, PRO).cls == "nominative"
    assert (only(hits, "es" "ri").category) == "repo-or-package-name"


# ------------------------------------------------------------------------------------------ categories

@pytest.mark.parametrize("path, line, token, category", [
    ("src/Tiles/Cache.cs", "// mirrors the " "Es" "ri" " tile scheme", "Es" "ri", "comment"),
    ("src/server.py", "x = 1  # " "arc" "gis" " instance root", "arc" "gis", "comment"),
    ("src/Honua." "Es" "ri" "/Startup.cs", "namespace Honua." "Es" "ri" ".Compat;", "Es" "ri", "namespace-or-import"),
    ("tests/test_layers.py", "def test_" "es" "ri" "_layer_roundtrip():", "test_" "es" "ri" "_layer_roundtrip", "test-name"),
    ("web/map.ts", 'import { featureLayer } from "' 'es' 'ri' '-leaflet";', "es" "ri", "third-party-reference"),
    ("tools/portal.py", "from " "arc" "gis" ".gis import GIS", "arc" "gis", "third-party-reference"),
    ("src/routes.ts", 'const root = "https://example.test/' 'arc' 'gis' '/rest/services";', "arc" "gis", "url"),
    ("src/routes.ts", 'const lane = "' 'es' 'ri' '-dotnet";', "es" "ri", "string-literal"),
    ("data/clients.json", '{"client": "' 'Arc' 'GIS' ' Maps SDK for .NET"}', "Arc" "GIS" " Maps SDK for .NET",
     "product-name-label"),
    ("package.json", '  "name": "@honua/' 'es' 'ri' '-tools",', "es" "ri", "repo-or-package-name"),
])
def test_avoidable_categories(path, line, token, category):
    hit = only(classify(path, line), token)
    assert (hit.cls, hit.category) == ("avoidable", category)


def test_block_comments_span_lines():
    hits = classify("src/a.cs", "/*\n * " "Es" "ri" " parity notes\n */\nvar a = 1;\n")
    assert only(hits, "Es" "ri").category == "comment"


def test_marks_only_count_where_a_word_starts():
    assert classify("src/a.cs", "var Sourc" "eS" "ri" "d = 4326; // D" "es" "ri" "re") == []
    assert only(classify("src/a.cs", "bool is" "Es" "ri" "Layer = true;"), "is" "Es" "ri" "Layer").cls == "avoidable"


def test_product_names_without_either_substring_are_found():
    hit = only(classify("docs/a.md", "Open the map in " "Arc" "Map" ".\n"), "Arc" "Map")
    assert (hit.term, hit.cls) == ("Arc" "Map", "avoidable")


def test_path_components_count_once_per_prefix():
    files = [("tests/" "es" "ri" "-leaflet/a.spec.ts", b"ok\n"), ("tests/" "es" "ri" "-leaflet/b.spec.ts", b"ok\n")]
    hits, _, scanned = vt.scan(files, "honua-test", VOCABULARY, [])
    assert scanned == 2
    assert [(hit.path, hit.line, hit.category) for hit in hits] == [("tests/" "es" "ri" "-leaflet", 0, "path")]


def test_every_mark_in_one_path_component_is_a_hit():
    files = [("es" "ri" "-" "arc" "gis" "-tools/a.txt", b"ok\n"), ("es" "ri" "-" "arc" "gis" "-tools/b.txt", b"ok\n")]
    hits, _, _ = vt.scan(files, "honua-test", VOCABULARY, [])
    assert [(hit.path, hit.token) for hit in hits] == [("es" "ri" "-" "arc" "gis" "-tools", "es" "ri"),
                                                      ("es" "ri" "-" "arc" "gis" "-tools", "arc" "gis")]


def test_large_text_files_are_scanned():
    data = b"x = 1\n" * (4 * 1024 * 1024) + b"var layer = new " b"Es" b"ri" b"Layer();\n"
    hits, skipped, scanned = vt.scan([("src/big.cs", data)], "honua-test", VOCABULARY, [])
    assert (scanned, dict(skipped)) == (1, {})
    assert [(hit.line, hit.token) for hit in hits] == [(4 * 1024 * 1024 + 1, "Es" "ri" "Layer")]


def test_symlink_names_are_classified_without_following_the_target(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "notes.txt").write_text("ok\n")
    (repo / "Es" "ri" "Docs").symlink_to("notes.txt")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    for files in (vt.working_tree_files(repo), vt.git_ref_files(repo, "HEAD")):
        hits, skipped, scanned = vt.scan(files, "honua-test", VOCABULARY, [])
        assert [(hit.path, hit.category) for hit in hits] == [("Es" "ri" "Docs", "path")]
        assert (dict(skipped), scanned) == ({"symlink": 1}, 1)


def test_generated_binary_and_bundled_files_are_skipped_but_counted():
    bundle = b"import{a as " b"es" b"ri" b"GeometryToGeoJSON}from'./x.js';" + b"var a=1;" * 2000
    files = [("package-lock.json", b'"' b'es' b'ri' b'-leaflet": {}'), ("logo.png", b"\x89PNG\0" b"es" b"ri"),
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
    files = [("src/legacy/Shim.cs", b"class " b"Es" b"ri" b"Shim {}\n"), ("src/New.cs", b"class " b"Es" b"ri" b"New {}\n")]
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
    avoidable = [{"path": path, "line": n + 1, "token": "Es" "ri", "category": "identifier"}
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
    assert "src/new.cs:1  identifier  " "Es" "ri" in problems[0]


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
    problems = vt.baseline_growth(_baseline({"src/Layer.cs": 2}), _baseline({"src/" "Es" "ri" "Layer.cs": 3}))
    assert [p.split(";")[0] for p in problems] == ["baseline.honua-test: src/Layer.cs is a new entry (2)"]
    problems = vt.baseline_growth(_baseline({"tests/leaflet/a.ts": 1}),
                                  _baseline({"tests/" "es" "ri" "-leaflet/a.ts": 1, "tests/" "es" "ri" "-leaflet": 1}))
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
    (repo / "src" / "Query.cs").write_text('var t = "' 'es' 'ri' 'GeometryPoint"; // ' 'Es' 'ri' ' parity\n')
    (repo / ".gitignore").write_text("out/\n")
    (repo / "out").mkdir()
    (repo / "out" / "Es" "ri" "Build.cs").write_text("class " "Es" "ri" "Build {}\n")
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

    (repo / "src" / "Layer.cs").write_text("public class " "Es" "ri" "Layer {}\n")
    capsys.readouterr()
    assert vt.main(["lint", *common, "--baseline", str(baseline)]) == 1
    assert "src/Layer.cs:1  identifier  " "Es" "ri" "Layer" in capsys.readouterr().out


def test_scan_cli_reads_a_commit_without_checking_it_out(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "Query.cs").write_text("// uncommitted " "Es" "ri" "Edit\n")
    report_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"
    assert vt.main(["scan", "--root", str(repo), "--repo", "honua-test", "--allowlist", "", "--git-ref",
                    "HEAD", "--json", str(report_path), "--markdown", str(markdown_path)]) == 0
    report = json.loads(report_path.read_text())
    assert report["counts"] == {"spec": 1, "spec-interop": 0, "nominative": 0, "avoidable": 1, "confidential": 0,
                                "avoidableExcepted": 0}
    assert report["avoidable"][0]["token"] == "Es" "ri"
    assert "src/Query.cs:1  comment  " "Es" "ri" in markdown_path.read_text()


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
    assert VOCABULARY["es" "ri" "FieldTypeOID"]["source"] == "gsr-1.0"
    assert (VOCABULARY["es" "ri" "FieldTypeOID"]["section"], VOCABULARY["es" "ri" "FieldTypeOID"]["sectionTitle"]) == (
        "4.2.3", "Layer/Table Example")
    assert VOCABULARY["es" "ri" "SpatialRelIntersects"]["section"] == "4.2.4.2"
    assert set(document["sources"]) == {"gsr-1.0"}
    assert all(entry["source"] == "gsr-1.0" for entry in document["identifiers"])
    assert "es" "ri" "FieldTypeGUID" not in VOCABULARY  # later protocol value, not GSR 1.0 (R36)
    assert "arc" "gis" not in {entry["identifier"].lower() for entry in document["identifiers"]}


def test_policy_requires_neutral_compatibility_wording():
    policy = (vt.ROOT / "docs" / "THIRD-PARTY-TRADEMARKS.md").read_text()
    assert "Compatibility statements use protocol names, neutral client categories and tested versions." in policy
    assert "Do not add third-party product names or boilerplate attribution that repeats them." in policy
    assert not vt.is_attributed(policy)


def test_committed_allowlist_loads():
    entries = vt.load_allowlist(vt.ALLOWLIST)
    assert entries and all(entry.reason and entry.owner for entry in entries)


# ------------------------------------------------------------------------------------------ confidential (R30)

@pytest.mark.parametrize("path, line, token, category", [
    ("tools/replay.py", f"import {SCRIPTING}", SCRIPTING, "desktop-scripting"),
    ("tools/replay.py", f"{SCRIPTING}.mp.{MARK_B}Project(path)", SCRIPTING, "desktop-scripting"),
    ("src/Scan.py", f"class {SCRIPTING.title()}Scanner:", f"{SCRIPTING.title()}Scanner", "desktop-scripting"),
    ("docs/howto.md", f"Run the {SCRIPTING} replay.\n\n{ATTRIBUTION}", SCRIPTING, "desktop-scripting"),
    ("src/Load.cs", f'var project = "maps/site{PROJECT}";', PROJECT, "desktop-file"),
    ("src/Load.cs", f'var tools = "gp/Tools{TOOLBOX}";', TOOLBOX, "desktop-file"),
    ("README.md", f"Open Replay{PYTHON_TOOLBOX} first.", PYTHON_TOOLBOX, "desktop-file"),
    ("src/Runner.cs", f'var py = @"{INSTALL_PATH}";', "Arc" "GIS", "runner-detail"),
    ("env/runner.env", f"{LICENCE_VARIABLE}={LICENCE_SERVER}", LICENCE_VARIABLE, "runner-detail"),
    ("env/runner.env", f"LM_HOST={LICENCE_SERVER}", LICENCE_SERVER, "runner-detail"),
    ("src/Lic.cs", f"var m = new {LICENCE_MANAGER}();", LICENCE_MANAGER, "runner-detail"),
    (".github/workflows/x.yml", "    runs-on: [self-hosted, windows, " "es" "ri" "-desktop]", "es" "ri", "runner-detail"),
    ("src/Lanes.cs", f"public sealed class {CLIENT_CLASS} {{}}", CLIENT_CLASS, "desktop-client"),
    ("src/Lanes.cs", f'const string Client = "{PRO}";', PRO, "desktop-client"),
    ("tests/test_lanes.py", f'LANE = "{PRO.lower().replace(" ", "-")}"', "arc" "gis", "desktop-client"),
])
def test_every_confidential_pattern_is_detected(path, line, token, category):
    hit = only(classify(path, line), token)
    assert (hit.cls, hit.category) == ("confidential", category)


@pytest.mark.parametrize("line", [
    "import pytest  # .pytest_cache stays ignored",
    "Honua " "Arc" "GIS" " Professional Services",
    "var Sourc" "eS" "ri" "d = 4326;",
    "public record " "Es" "ri" "SampleLicenseMetadata(string Attribution);",
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
    files = [("tools/replay.py", f"import {SCRIPTING}\nclass {MARK_A}Shim: pass\n".encode())]
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
             (f"scripts/{PRO.lower().replace(' ', '-')}/{MARK_A}Run.ps1", b"$x = '" b"Es" b"ri" b"Lane'\n"),
             ("src/a.cs", b"class " b"Es" b"ri" b"A {}\n")]
    hits, skipped, scanned = vt.scan(files, "honua-test", VOCABULARY, [])
    markdown = vt.render_markdown(vt.build_report("honua-test", "0" * 40, hits, skipped, scanned))
    assert not vt.is_confidential_text(markdown)
    assert "| confidential (R30) | 2 |" in markdown and "src/a.cs:1  identifier  " "Es" "ri" "A" in markdown
    assert "2 avoidable hits in 1 file whose path is itself confidential" in markdown


def test_this_tooling_carries_no_confidential_term():
    for path in ("tools/check_vendor_terms.py", "tools/test_check_vendor_terms.py", "docs/THIRD-PARTY-TRADEMARKS.md",
                 ".github/workflows/gate-vendor-terms.yml"):
        text = (vt.ROOT / path).read_text()
        hits = vt.Classifier("honua-release", VOCABULARY, []).classify_text(path, text)
        assert [hit.as_dict() for hit in hits if hit.cls == "confidential"] == [], path


# ------------------------------------------------------------------------------------------ interop vocabulary (R36)

def test_interop_identifiers_are_spec_interop_and_reported_separately():
    hits = classify("src/Fields.cs", 'var a = "' 'es' 'ri' 'FieldTypeGUID"; var b = "' 'es' 'ri' 'GeometryPoint";')
    assert (only(hits, "es" "ri" "FieldTypeGUID").cls, only(hits, "es" "ri" "FieldTypeGUID").category) == (
        "spec-interop", "interop-identifier")
    assert only(hits, "es" "ri" "GeometryPoint").cls == "spec"
    report = vt.build_report("honua-test", "0" * 40, hits, vt.Counter(), 1)
    assert (report["counts"]["spec"], report["counts"]["spec-interop"]) == (1, 1)
    assert report["topInteropIdentifiers"] == {"es" "ri" "FieldTypeGUID": 1}


@pytest.mark.parametrize("identifier, category", [
    ("es" "ri" "GeometryNull", "non-wire-identifier"),
    ("es" "ri" "SpatialRelWithinDistance", "non-wire-identifier"),
    ("es" "ri" "SpatialRelBeyondDistance", "non-wire-identifier"),
    ("es" "ri" "MosaicByAttribute", "wrong-wire-value"),
])
def test_identifiers_that_are_not_wire_values_are_flagged_for_renaming(identifier, category):
    hit = only(classify("src/Wire.cs", f'var v = "{identifier}";'), identifier)
    assert (hit.cls, hit.category) == ("avoidable", category)
    assert INTEROP.refused["es" "ri" "MosaicByAttribute"]["expected"] == "es" "ri" "MosaicAttribute"
    assert "es" "ri" "MosaicAttribute" in VOCABULARY


def test_the_committed_interop_vocabulary_cites_every_entry():
    document = json.loads(vt.INTEROP.read_text())
    assert len(document["identifiers"]) == 76
    for entry in document["identifiers"]:
        assert entry["reference"].startswith("https://developers." "arc" "gis" ".com/"), entry
        assert entry["family"] and entry["field"] and isinstance(entry["listedVerbatim"], bool), entry
    assert not set(INTEROP.identifiers) & set(VOCABULARY), "an identifier is in GSR 1.0 and interop both"
    assert "not GeoServices REST Specification 1.0" in document["description"]


def _interop(tmp_path: Path, identifiers: list[dict], refused=()) -> Path:
    path = tmp_path / "interop.json"
    path.write_text(json.dumps({"schema": vt.INTEROP_SCHEMA, "identifiers": identifiers, "refused": list(refused)}))
    return path


def test_the_interop_vocabulary_grows_only_with_a_cited_entry(tmp_path):
    cited = {"identifier": "es" "ri" "NATravelDirectionFromFacility", "family": "travel direction",
             "field": "travelDirection",
             "reference": "https://developers." "arc" "gis" ".com/rest/services-reference/enterprise/service-area/"}
    assert "es" "ri" "NATravelDirectionFromFacility" in vt.load_interop(_interop(tmp_path, [cited])).identifiers
    for uncited in ({k: v for k, v in cited.items() if k != "reference"}, cited | {"reference": "see the docs"},
                    cited | {"reference": "https://example.test/" "es" "ri"}, cited | {"field": ""}):
        with pytest.raises(SystemExit, match="cited entry"):
            vt.load_interop(_interop(tmp_path, [uncited]))
    with pytest.raises(SystemExit, match="expected wire value"):
        vt.load_interop(_interop(tmp_path, [], [{"identifier": "es" "ri" "X", "category": "wrong-wire-value",
                                                 "reason": "r"}]))


# ------------------------------------------------------------------------------------------ certification labels (R37)

def test_stamped_certification_data_makes_product_labels_nominative():
    stamped = json.dumps({"schema": "honua.protocol-certification-requirements/v1", "trademarkNotice": ATTRIBUTION,
                          "requirements": [{"canonical_client": "Arc" "GIS" " Maps SDK for .NET"},
                                            {"canonical_client": PRO},
                                            {"canonical_client": f"{PRO}/{SCRIPTING}"}]}, indent=2)
    hits = classify("certification/requirements.json", stamped)
    assert (only(hits, "Arc" "GIS" " Maps SDK for .NET").cls, only(hits, "Arc" "GIS" " Maps SDK for .NET").category) == (
        "nominative", "certification-label")
    labels = [hit for hit in hits if hit.token == PRO]
    assert [(hit.cls, hit.category) for hit in labels] == [("nominative", "certification-label"),
                                                         ("confidential", "desktop-client")]
    assert only(hits, SCRIPTING).cls == "confidential"  # the label is allowed, the detail is not
    # the generator's schema marker counts outside certification/ too
    assert only(classify("docs/gis/data/requirements.json", stamped), "Arc" "GIS" " Maps SDK for .NET").category == \
        "certification-label"


def test_unstamped_certification_data_stays_avoidable():
    unstamped = json.dumps({"schema": "honua.protocol-certification-requirements/v1",
                            "requirements": [{"canonical_client": "Arc" "GIS" " Maps SDK for .NET"},
                                             {"canonical_client": PRO}]}, indent=2)
    hits = classify("certification/requirements.json", unstamped)
    assert (only(hits, "Arc" "GIS" " Maps SDK for .NET").cls, only(hits, "Arc" "GIS" " Maps SDK for .NET").category) == (
        "avoidable", "product-name-label")
    assert only(hits, PRO).cls == "confidential"
    # the notice outside the header (not a top-level trademarkNotice) is no stamp
    nested = json.dumps({"requirements": [{"note": ATTRIBUTION, "canonical_client": "Arc" "GIS" " Maps SDK for .NET"}]},
                        indent=2)
    assert only(classify("certification/requirements.json", nested), "Arc" "GIS" " Maps SDK for .NET").cls == "avoidable"


def test_a_stamped_yaml_certification_file_uses_its_header_comment():
    text = f"# {ATTRIBUTION}\nlanes:\n  - client: {MARK_B} Maps SDK for .NET 200.x\n"
    assert only(classify("certification/lanes.yaml", text), "Arc" "GIS" " Maps SDK for .NET").category == \
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
    assert alias.tokens == ("arc" "gis",) and len(alias.paths) == 2
    assert all(path.endswith(".cs") and "*" not in path for path in alias.paths)
    assert alias.covers("honua-server", alias.paths[0], "arc" "gis")
    assert not alias.covers("honua-server", "src/Honua.Server/Other.cs", "arc" "gis")
    assert not alias.covers("honua-server", alias.paths[0], "Es" "ri")


def test_the_recorded_leaflet_check_name_is_allowlisted_only_in_the_server_fixtures():
    """The c19f29d and 08ace249 fixtures quote a real Actions job name. The exception does not
    cover any other token or path."""
    paths = ("tools/fixtures/candidate-resolution-2026-10-06/honua-server-c19f29d.json",
             "tools/fixtures/candidate-resolution-2026-10-26/honua-server-08ace24.json")
    token = "Es" "ri" " Leaflet"
    for path in paths:
        entries = [entry for entry in vt.load_allowlist(vt.ALLOWLIST) if entry.covers("honua-release", path, token)]
        assert len(entries) == 1
        entry = entries[0]
        assert entry.tokens == (token,)
        assert entry.paths == paths
        assert entry.reason and entry.owner
        assert not entry.covers("honua-release", path, "Arc" "GIS")
        assert not entry.covers("honua-release", path, "es" "ri")
    other = "tools/fixtures/candidate-resolution-2026-10-06/honua-sdk-js-984425f.json"
    assert not entry.covers("honua-release", other, token)


def test_the_gate_pins_its_tooling_and_fails_on_known_confidential_uses():
    workflow = (vt.ROOT / ".github" / "workflows" / "gate-vendor-terms.yml").read_text()
    assert "--fail-on-known-confidential" in workflow
    assert "^[0-9a-f]{40}$" in workflow and "required: true" in workflow.split("tools_ref:")[2].split("outputs:")[0]
    assert "trunk" not in workflow




def test_identifier_exception_cannot_cover_confidential_hits_or_nonliteral_patterns(tmp_path):
    entry = {"repo": "honua-test", "paths": ["pins.yaml"], "reason": "exact identity", "owner": "@owner"}
    allowlist = vt.load_allowlist(_allowlist(tmp_path, [{**entry, "identifiers": [SCRIPTING]}]))
    hits = vt.Classifier("honua-test", VOCABULARY, allowlist).classify_text("pins.yaml", SCRIPTING)
    assert hits and all(hit.cls == "confidential" and hit.exception is None for hit in hits)
    for identifiers in [["*" "es" "ri" "*"], [""], "es" "ri", [12]]:
        with pytest.raises(SystemExit, match="exact nonempty lexical identifiers"):
            vt.load_allowlist(_allowlist(tmp_path, [{**entry, "identifiers": identifiers}]))


def test_immutable_gp_guide_exception_is_exact_and_remains_visible():
    import hashlib

    path = "certification/executable-docs/fixtures/site-geoprocessing-99dee72.md"
    raw = (vt.ROOT / path).read_bytes()
    assert len(raw) == 4504
    assert hashlib.sha256(raw).hexdigest() == "b0be5ceeb94f8fc8433426dc17645dfe4d33d27acaec1c7ac63e6f3b854f87d2"
    entries = vt.load_allowlist(vt.ALLOWLIST)
    selected = [entry for entry in entries if entry.covers("honua-release", path, "Es" "ri")]
    assert len(selected) == 1
    entry = selected[0]
    assert entry.paths == (path,) and entry.tokens == ("Es" "ri",)
    assert entry.owner == "@mikemcdougall"
    source_entry = json.loads(vt.ALLOWLIST.read_text())["entries"][entry.index]
    assert source_entry["issue"] == "honua-io/honua-release#721"
    assert "b0be5cee" in entry.reason
    for repo, candidate, content, excepted in [
        ("honua-release", path, raw, True),
        ("honua-release", path.replace("99dee72", "sibling"), raw, False),
        ("honua-site", path, raw, False),
        ("honua-release", path, b"Arc" b"GIS" b" Online\n", False),
    ]:
        hits, skipped, scanned = vt.scan([(candidate, content)], repo, VOCABULARY, entries)
        report = vt.build_report(repo, "0" * 40, hits, skipped, scanned)
        assert report["counts"]["avoidable"] == 1
        assert report["counts"]["avoidableExcepted"] == int(excepted)
        baseline = {"schema": vt.BASELINE_SCHEMA, "repo": repo, "files": {}, "total": 0}
        assert bool(vt.lint(report, baseline)) is not excepted
        assert ("exception" in report["avoidable"][0]) is excepted


@pytest.mark.parametrize("encoded", [
    "&#" + "69;sri",
    "%" + "45sri",
    chr(92) + "u0045sri",
    chr(92) + "x45sri",
    chr(92) + "N{LATIN CAPITAL LETTER E}sri",
    "Ｅｓｒｉ",
    "E\u200bsri",
    "%" + "252545sri",
])
def test_encoded_vendor_terms_are_detected_on_the_original_physical_line(encoded):
    hits = classify("src/client.ts", "first\nsecond\nconst name = '" + encoded + "';\nfourth\n")
    assert len(hits) == 1
    assert (hits[0].line, hits[0].token, hits[0].cls) == (3, "Es" "ri", "avoidable")


def test_encoded_wire_identifier_retains_spec_classification():
    encoded = "&#" + "101;sriGeometryPoint"
    hits = classify("data.json", '{"type":"' + encoded + '"}')
    assert [(hit.token, hit.cls) for hit in hits] == [("es" "ri" "GeometryPoint", "spec")]


def test_decoded_line_break_does_not_shift_physical_diagnostics():
    encoded = "&#" + "10;" + "&#" + "69;sri"
    hits = classify("site.html", "first\n" + encoded + "\nthird\n" "Es" "ri")
    assert [hit.line for hit in hits] == [2, 4]


def test_encoded_names_in_paths_retain_original_path():
    classifier = vt.Classifier("honua-test", VOCABULARY, [])
    path = "src/%" + "45sri/client.ts"
    hit = classifier.classify_path(path)[0]
    assert (hit.path, hit.line, hit.token) == ("src/%" + "45sri", 0, "Es" "ri")


def test_paired_source_backslashes_are_not_interpreted_as_one_escape():
    escaped = chr(92) * 2 + "u0045sri"
    assert classify("src/client.ts", escaped) == []


def test_too_many_encoding_layers_fail_closed_without_echoing_content():
    value = "&" + "amp;" * 10 + "#69;sri"
    with pytest.raises(ValueError, match="too many nested encodings") as error:
        classify("site.html", value)
    assert value not in str(error.value)


def test_plain_and_encoded_occurrences_are_both_reported():
    encoded = "&#" + "69;sri"
    hits = classify("site.html", "Es" "ri" " " + encoded)
    assert [hit.token for hit in hits] == ["Es" "ri", "Es" "ri"]


def test_large_encoded_line_fails_closed_without_echoing_content():
    value = "x" * 1_000_001 + "%"
    with pytest.raises(ValueError, match="input exceeds the inspection limit") as error:
        classify("source.txt", value)
    assert value not in str(error.value)


@pytest.mark.parametrize("separator", ["\r", "\u2028"])
def test_delta_line_mapping_preserves_earlier_logical_separators(separator):
    import vendor_terms_delta as delta

    text = "before" + separator + "still first physical line\n" + "&#" + "69;sri\n"
    hits = classify("file.txt", text)
    mapping = delta.logical_to_git_lines(text)
    assert len(hits) == 1
    assert mapping[hits[0].line - 1] == 2


def test_scan_cli_fails_closed_without_echoing_oversized_line(tmp_path):
    sensitive = "unique-sensitive-payload-" + "x" * 1_000_001 + "%"
    (tmp_path / "source.txt").write_text(sensitive)
    result = subprocess.run(
        [sys.executable, str(vt.ROOT / "tools/check_vendor_terms.py"), "scan", "--root", str(tmp_path),
         "--repo", "honua-test"], capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "input exceeds the inspection limit" in result.stderr
    assert sensitive not in result.stdout + result.stderr
    assert "unique-sensitive-payload-" not in result.stdout + result.stderr


def test_encoded_hit_is_not_hidden_by_different_scoped_exception_on_same_line():
    exception = vt.Exception_(0, "honua-test", ("source.py",), (), "reason", "owner", ("allowedModelBuilder",))
    text = "model%42" + "uilder + allowedModel" + "Builder"
    hits = classify("source.py", text, [exception])
    assert any(hit.exception is None and hit.cls == "avoidable" for hit in hits)
    assert any(hit.exception == 0 for hit in hits)


def test_raw_path_hit_survives_percent_decoding():
    classifier = vt.Classifier("honua-test", VOCABULARY, [])
    path = "src/%e" + "Es" "ri" ".py"
    assert any(hit.path == path and hit.cls == "avoidable" for hit in classifier.classify_path(path))


def test_excessive_terms_fail_closed():
    text = ("Es" + "ri ") * 513
    with pytest.raises(ValueError, match="too many terms"):
        classify("source.txt", text)


def test_pathological_token_fails_closed():
    text = "x" * 4097 + "Es" + "ri"
    with pytest.raises(ValueError, match="token exceeds"):
        classify("source.txt", text)


def test_long_changed_line_is_scanned_conservatively():
    text = "plain " * 1000 + "Es" + "ri " + "&#" + "69;sri"
    hits = classify("source.txt", text)
    assert len(hits) >= 2
    assert all(hit.cls == "avoidable" for hit in hits)


def test_encoded_suffix_does_not_inherit_an_identifier_exception():
    exception = vt.Exception_(0, "honua-test", ("source.py",), (), "reason", "owner", ("allowedModelBuilder",))
    value = "allowedModel" + "Builder%45vil"
    hits = classify("source.py", value, [exception])
    assert any(hit.cls == "avoidable" and hit.exception is None for hit in hits)
