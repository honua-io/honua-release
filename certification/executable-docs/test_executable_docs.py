"""Offline self-tests for the executable-docs gate: extraction, classification, inputs, oracle, guard."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from blocks import extract, parse_html  # noqa: E402
from inputs import doc_id, load_vars, needs, render  # noqa: E402
from inventory import Resolver, build, drift  # noqa: E402
from registry_guard import (filter_npm_packument, honua_dependencies, filter_nuget_versions, filter_pypi_simple,  # noqa: E402
                            nuget_family_pins, pins_from_manifest)
from run import (assert_output, combine_csharp, combine_js, continuation_error, scrub,  # noqa: E402
                 split_csharp, substitute, SERVE)


def by_index(text: str, fmt: str = "markdown"):
    return {b.index: b for b in extract(text, fmt)}


def test_runnable_languages_are_run_and_others_illustrative():
    blocks = by_index("```bash\nnpm i x\n```\n\n```python\nprint(1)\n```\n\n```yaml\na: 1\n```\n"
                      "\n```csharp\nConsole.WriteLine(1);\n```\n\n```http\nGET /healthz\n```\n")
    assert [blocks[i].intent for i in range(5)] == ["run", "run", "illustrative", "run", "run"]
    assert blocks[2].reason == "yaml block is not executable by this gate"


def test_skip_marker_requires_a_reason():
    text = ('<!-- doc-run: skip reason="needs a browser" -->\n```js\nmap.on()\n```\n\n'
            "<!-- doc-run: skip -->\n```js\nmap.off()\n```\n")
    blocks = by_index(text)
    assert blocks[0].intent == "excluded" and blocks[0].reason == "needs a browser"
    assert blocks[1].intent == "run" and "without a reason" in blocks[1].marker_error


def test_existing_doc_test_fence_attributes_are_honoured():
    blocks = by_index('```ts doc-test=compile\nconst a: number = 1\n```\n\n'
                      '```ts doc-test=skip reason="partial excerpt"\nfoo()\n```\n\n'
                      '```ts doc-test=skip\nbar()\n```\n')
    assert blocks[0].intent == "compile"
    assert blocks[1].intent == "excluded" and blocks[1].reason == "partial excerpt"
    assert blocks[2].intent == "run" and blocks[2].marker_error


def test_file_cue_and_explicit_file_marker():
    blocks = by_index("Put this in it as `compose.yaml`:\n\n```yaml\nservices: {}\n```\n\n"
                      "Save the script as `quickstart.py`:\n\n```python\nprint('hi')\n```\n\n"
                      "<!-- doc-run: file=app/main.ts -->\n```ts\nexport {}\n```\n")
    assert (blocks[0].intent, blocks[0].file) == ("file", "compose.yaml")
    assert (blocks[1].intent, blocks[1].file) == ("file", "quickstart.py")
    assert (blocks[2].intent, blocks[2].file, blocks[2].intent_source) == ("file", "app/main.ts", "marker")


def test_expected_output_binds_to_the_preceding_run_block():
    blocks = by_index("```bash\ncurl x\n```\n\nIt prints:\n\n```text\nhello 42\n```\n")
    assert blocks[1].intent == "output" and blocks[1].output_of == 0
    assert blocks[0].expected_output == "hello 42"


def test_console_transcripts_split_commands_from_output():
    blocks = by_index("```console\n$ honua --version\nhonua 1.2.3\n```\n")
    assert blocks[0].code == "honua --version\n"
    assert blocks[0].expected_output == "honua 1.2.3"


def test_powershell_is_a_recorded_alternative():
    blocks = by_index("```powershell\nGet-Item .\n```\n")
    assert blocks[0].intent == "alternative"


def test_indented_fences_inside_list_items_are_dedented():
    blocks = by_index("1. Install:\n\n   ```bash\n   pip install honua-sdk\n   ```\n")
    assert blocks[0].code == "pip install honua-sdk\n"


def test_html_pre_blocks_strip_markup_and_detect_shell():
    page = ('<p>x</p><pre tabindex="0"><span class="comment"># needs docker</span>\n'
            "git clone https://github.com/honua-io/honua-server.git\ncurl http://localhost:8080/healthz/ready\n"
            "</pre>")
    blocks = parse_html(page)
    assert blocks[0].language == "shell"
    assert "git clone" in blocks[0].code and "<span" not in blocks[0].code


def test_needs_detects_unset_env_and_placeholders():
    block = extract('```bash\ncurl -H "X-API-Key: $HONUA_API_KEY" "$BASE/x" <your-token>\n```\n', "markdown")[0]
    need = needs(block, set())
    assert need["env"] == ["BASE", "HONUA_API_KEY"]
    assert need["placeholders"] == ["<your-token>"]
    assert needs(block, {"HONUA_API_KEY", "BASE"})["env"] == []


def test_needs_ignores_assigned_defaulted_and_quoted_heredoc_names():
    code = ("```bash\nexport A=1\nB=$(date)\necho \"$A $B ${C:-x}\"\ncat > f <<'EOF'\n$NOT_A_REF\nEOF\n"
            "for X in 1 2; do echo $X; done\n```\n")
    need = needs(extract(code, "markdown")[0], set())
    assert need["env"] == [] and need["optionalEnv"] == ["C"]


def test_needs_in_python_js_and_csharp():
    py = extract('```python\nimport os\nk = os.environ["HONUA_API_KEY"]\nu = os.getenv("U", "d")\n```\n', "markdown")[0]
    assert needs(py, set())["env"] == ["HONUA_API_KEY"] and needs(py, set())["optionalEnv"] == ["U"]
    js = extract("```js\nconst k = process.env.KEY; const u = process.env.URL ?? 'x'\n```\n", "markdown")[0]
    assert needs(js, set())["env"] == ["KEY"] and needs(js, set())["optionalEnv"] == ["URL"]
    cs = extract('```csharp\nvar k = Environment.GetEnvironmentVariable("KEY");\n```\n', "markdown")[0]
    assert needs(cs, set())["env"] == ["KEY"]


def test_vars_file_entries_must_cite_the_document(tmp_path):
    (tmp_path / "doc.json").write_text(json.dumps({"env": {"K": {"value": "{candidate.apiKey}"}}}))
    with pytest.raises(ValueError, match="documentedAt"):
        load_vars(tmp_path, "doc")
    (tmp_path / "doc.json").write_text(json.dumps(
        {"env": {"K": {"value": "{candidate.apiKey}", "documentedAt": "README.md:12 'export K=...'"}}}))
    assert load_vars(tmp_path, "doc")["env"]["K"]["value"] == "{candidate.apiKey}"
    assert load_vars(tmp_path, "missing") == {"env": {}, "substitute": {}}
    assert render("{candidate.apiKey}/{unknown.x}", {"candidate.apiKey": "k"}) == "k/{unknown.x}"


def test_doc_id_is_stable():
    assert doc_id("honua-io/honua-sdk-js", "docs/quickstart.md") == "honua-sdk-js-docs-quickstart"


def test_output_oracle_matches_in_order_with_digits_normalized():
    assert assert_output("Found 3 features\n...\ndone", "Found 12 features\nnoise\ndone\n")[0]
    ok, why = assert_output("done\nFound 3 features", "Found 12 features\ndone\n")
    assert not ok and "not found" in why
    assert assert_output('{"mode": "disabled"}', '{"mode": "disabled", "x": 1}\n')[0]
    assert not assert_output('{"mode": "disabled"}', '{"x": 1}\n')[0]


def test_csharp_continuation_hoists_usings_and_types():
    first = "using Honua.Sdk;\nvar client = new Client();\nrecord Point(double X, double Y);\n"
    second = "using System.Linq;\nConsole.WriteLine(client);\n"
    usings, statements, types = split_csharp(first)
    assert usings == ["using Honua.Sdk;"] and types == ["record Point(double X, double Y);"]
    combined = combine_csharp([first, second])
    assert combined.index("using System.Linq;") < combined.index("var client")
    assert combined.index("Console.WriteLine") < combined.index("record Point")
    assert continuation_error("csharp", "error CS0103: The name 'client' does not exist")
    assert continuation_error("javascript", "ReferenceError: client is not defined")


def test_js_continuation_hoists_imports():
    combined = combine_js(["import { A } from 'a';\nconst a = new A();\n", "import { A } from 'a';\na.run();\n"])
    assert combined.count("import { A } from 'a';") == 1 and combined.index("a.run") > combined.index("new A")


def test_serve_detection_and_substitution_and_scrub():
    assert SERVE.search("npm install\nnpm run dev\n")
    assert SERVE.search("docker compose up\n") and not SERVE.search("docker compose up -d\n")
    assert substitute("curl https://your-server.example.com/x", {"https://your-server.example.com": "http://h"}) == \
        "curl http://h/x"
    assert scrub("HONUA_ADMIN_PASSWORD=abc key=k123 sekrit", ["sekrit"]) == "HONUA_ADMIN_PASSWORD=*** key=k123 ***"


MANIFEST = {
    "platformRelease": "2026.1-rc.2",
    "components": {"honua-server": {"sha": "a" * 40}},
    "clientArtifacts": {
        "honua-sdk-js": {"package": "@honua/sdk-js", "version": "0.1.12", "integrity": "sha512-x", "sourceSha": "b" * 40},
        "honua-sdk-python-wheel": {"package": "honua-sdk", "version": "0.1.12",
                                   "filename": "honua_sdk-0.1.12-py3-none-any.whl", "sourceSha": "c" * 40},
        "honua-sdk-dotnet": {"package": "Honua.Sdk", "version": "1.10.1", "registry": "nuget.org", "sourceSha": "d" * 40},
    },
}


def test_guard_pins_and_filters():
    pins = pins_from_manifest(MANIFEST)
    assert pins == {"npm": {"@honua/sdk-js": "0.1.12"}, "pypi": {"honua-sdk": "honua_sdk-0.1.12-py3-none-any.whl"},
                    "nuget": {"honua.sdk": "1.10.1"}}
    packument = {"name": "@honua/sdk-js", "versions": {"0.1.11": {}, "0.1.12": {}, "0.2.0-beta.0": {}},
                 "dist-tags": {"latest": "0.2.0-beta.0", "next": "0.2.0-beta.0"}, "time": {"0.1.12": "t", "created": "c"}}
    filtered = filter_npm_packument("@honua/sdk-js", packument, pins["npm"])
    assert list(filtered["versions"]) == ["0.1.12"] and filtered["dist-tags"] == {"latest": "0.1.12"}
    assert filter_npm_packument("@honua-io/sdk-js", packument, pins["npm"]) is None   # old scope
    assert filter_npm_packument("create-honua-app", packument, pins["npm"]) is None    # not pinned
    assert filter_npm_packument("maplibre-gl", packument, pins["npm"]) is packument   # third party
    simple = ('<a href="https://f/honua_sdk-0.1.11-py3-none-any.whl#sha256=1">honua_sdk-0.1.11-py3-none-any.whl</a><br/>'
              '<a href="https://f/honua_sdk-0.1.12-py3-none-any.whl#sha256=2">honua_sdk-0.1.12-py3-none-any.whl</a><br/>')
    kept = filter_pypi_simple("honua-sdk", simple, pins["pypi"])
    assert "0.1.12" in kept and "0.1.11" not in kept
    assert filter_pypi_simple("honua-gp", simple, pins["pypi"]) is None
    assert filter_pypi_simple("requests", simple, pins["pypi"]) == simple
    nuget = dict(pins["nuget"], **nuget_family_pins(pins))
    assert filter_nuget_versions("Honua.Sdk.GeoServices", {"versions": ["1.9.0", "1.10.1"]}, nuget) == {"versions": ["1.10.1"]}
    assert filter_nuget_versions("Honua.Sdk", {"versions": ["1.9.0"]}, nuget) is None
    assert filter_nuget_versions("Newtonsoft.Json", {"versions": ["13.0.3"]}, nuget) == {"versions": ["13.0.3"]}


def test_inventory_build_resolves_revision_rules_and_reports_drift():
    docs = {
        ("honua-io/honua-sdk-js", "b" * 40, "README.md"): "```bash\nnpm i @honua/sdk-js\n```\n",
        ("honua-io/honua-site", "e" * 40, "docs.html"): "<pre>docker compose up -d\n</pre>",
    }

    def fetch(url: str, accept: str | None = None, token: str | None = None) -> bytes:
        if url == "https://api.github.com/repos/honua-io/honua-site":
            return b'{"default_branch": "trunk"}'
        if url == "https://api.github.com/repos/honua-io/honua-site/commits/trunk":
            return ("e" * 40).encode()
        for (repo, rev, path), text in docs.items():
            if url == f"https://raw.githubusercontent.com/{repo}/{rev}/{path}":
                return text.encode()
        raise AssertionError(url)

    sources = {"documents": [
        {"repo": "honua-io/honua-sdk-js", "path": "README.md", "revision": {"clientArtifact": "honua-sdk-js"},
         "runtime": "node"},
        {"repo": "honua-io/honua-site", "path": "docs.html", "revision": {"defaultBranch": True}, "runtime": "python",
         "format": "html", "docker": True},
    ]}
    inventory = build(sources, Resolver(MANIFEST, fetch=fetch))
    assert [d["revision"] for d in inventory["documents"]] == ["b" * 40, "e" * 40]
    assert inventory["summary"]["byIntent"] == {"run": 2}
    assert drift(inventory, inventory) == []
    changed = json.loads(json.dumps(inventory))
    changed["documents"][1]["revision"] = "f" * 40
    assert drift(inventory, changed) == [f"honua-site-docs: revision {'e' * 12} -> {'f' * 12}"]


def test_committed_inventory_matches_sources_and_records_every_block():
    sources = json.loads((HERE / "sources.json").read_text())
    inventory = json.loads((HERE / "inventory.json").read_text())
    assert [d["id"] for d in inventory["documents"]] == [doc_id(d["repo"], d["path"]) for d in sources["documents"]]
    for document in inventory["documents"]:
        assert [b["index"] for b in document["blocks"]] == list(range(len(document["blocks"])))
        for block in document["blocks"]:
            assert block["intent"] in {"run", "compile", "file", "output", "alternative", "illustrative", "excluded"}
            if block["intent"] == "excluded":
                assert block["reason"].strip()
    for runtime, image in sources["runtimes"].items():
        assert "@sha256:" in image, runtime


def test_vars_files_name_declared_documents_and_cite_them():
    ids = {doc_id(d["repo"], d["path"]) for d in json.loads((HERE / "sources.json").read_text())["documents"]}
    for path in sorted((HERE / "vars").glob("*.json")):
        assert path.stem in ids, path.name
        load_vars(HERE / "vars", path.stem)


def test_guard_admits_the_dependency_closure_of_a_pin():
    entry = {"dependencies": {"@honua/honua-migrate": "0.1.3-beta.0", "@honua/sdk": "^0.1.2-beta.0", "zod": "3.0.0"}}
    assert honua_dependencies(entry) == {"@honua/honua-migrate": "0.1.3-beta.0", "@honua/sdk": "*"}
    packument = {"versions": {"0.1.2-beta.0": {}, "0.1.3": {}}, "dist-tags": {"latest": "0.1.3"}}
    assert filter_npm_packument("@honua/sdk", packument, {"@honua/sdk": "*"}) is packument


def test_url_placeholders_are_needs():
    block = extract('```python\nHonuaClient("https://your-honua-server.com")\nG("your-honua-server.com:8081")\n```\n',
                    "markdown")[0]
    assert needs(block, set())["placeholders"] == ["https://your-honua-server.com", "your-honua-server.com:8081"]


def test_release_train_and_nightly_consume_the_gate():
    import yaml
    root = HERE.parents[1]
    gate = yaml.safe_load((root / ".github/workflows/gate-executable-docs.yml").read_text())
    assert gate[True]["workflow_call"]["outputs"]["overall_status"]["value"] == "${{ jobs.run.outputs.overall_status }}"
    train = yaml.safe_load((root / ".github/workflows/release-train.yml").read_text())["jobs"]
    assert train["gate_executable_docs"]["uses"] == "./.github/workflows/gate-executable-docs.yml"
    assert "gate_executable_docs" in train["report"]["needs"]
    assemble = next(s for s in train["report"]["steps"] if s.get("id") == "assemble")
    assert "executable-docs|$S_EXEC_DOCS" in assemble["run"]
    sys.path.insert(0, str(root / "tools"))
    import check_promotion_readiness as readiness
    import mint_nightly_lock as mint
    assert readiness.EVIDENCE_CLASSES["executable-docs"] == "nightly"
    assert mint.CLASS_GATES["executable-docs"] == "executable-docs"
    assert "executable-docs" in mint.REQUIRED_NIGHTLY_GATES


def test_blockquoted_fences_and_teardown_blocks():
    text = ("> **Base URL.** Take it from `.env`:\n>\n> ```bash\n> export A=1\n> ```\n\n"
            "```bash\ndocker compose down              # stop, keep data\ndocker compose down --volumes\n```\n\n"
            "<!-- doc-run: teardown -->\n```bash\nrm -rf .venv\n```\n")
    blocks = by_index(text)
    assert (blocks[0].intent, blocks[0].code) == ("run", "export A=1\n")
    assert blocks[1].intent == "teardown" and blocks[2].intent == "teardown"
