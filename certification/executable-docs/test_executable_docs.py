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
from registry_guard import (filter_npm_packument, filter_nuget_registration, honua_dependencies, filter_nuget_versions, filter_pypi_simple,  # noqa: E402
                            digest_matches, nuget_family_pins, pins_from_manifest, Guard)
from run import (assert_output, combine_csharp, combine_js, continuation_error, scrub,  # noqa: E402
                 shell_runtime, split_csharp, substitute, SERVE)


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


def test_prose_examples_are_not_the_commands_output():
    text = ("```bash\nnpx honua-mcp\n```\n\nTools that need a Honua surface degrade gracefully on a plain\n"
            "endpoint: they return a structured result\n\n```json\n{\"available\": false}\n```\n\n"
            "instead of crashing.\n")
    blocks = by_index(text)
    assert blocks[1].intent == "illustrative" and blocks[0].expected_output is None


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


def test_output_oracle_preserves_digits_and_only_elides_explicit_ellipses():
    assert assert_output("Found 3 features\n...\ndone", "Found 12 features\nnoise\ndone\n")[0] is False
    assert assert_output("Found 3 features\n...\ndone", "Found 3 features\nnoise\ndone\n")[0] is True
    assert assert_output('PASS: "380 New York St" -> "..." (..., ...), score ...',
                         'PASS: "380 New York St" -> "380 New York St, Redlands" (34.05, -117.19), score 100\n')[0]
    ok, why = assert_output("done\nFound 3 features", "Found 12 features\ndone\n")
    assert ok is False and why == "output differs from documented full output"
    assert assert_output('{"mode": "disabled"}', '{"mode": "disabled", "x": 1}\n')[0] is False
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


def test_install_steps_run_in_the_runtime_of_the_language_they_install_for():
    page = {"runtime": "node"}
    assert shell_runtime("npm install @honua/sdk-js@0.1.12", page) == "node"
    assert shell_runtime("python3 -m pip install honua-sdk==0.1.12", page) == "python"
    assert shell_runtime("pip install honua-sdk==0.1.12", page) == "python"
    assert shell_runtime("dotnet new console -o app && cd app\ndotnet add package Honua.Sdk --version 1.10.1", page) == "dotnet"
    assert shell_runtime("export HONUA_BASE_URL=http://localhost:8080", page) == "node"
    assert shell_runtime("echo 'pip install x'", page) == "node"
    # A stack the document starts, or a repository it reads from, keeps one machine.
    assert shell_runtime("pip install honua-sdk==0.1.12", {"runtime": "node", "docker": True}) == "node"
    assert shell_runtime("dotnet build", {"runtime": "python", "checkout": {"cwd": ""}}) == "python"


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
                    "nuget": {"honua.sdk": "1.10.1"}, "digests": {"npm": {"@honua/sdk-js": "sha512-x"}, "pypi": {}, "nuget": {}}}
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
            assert block["intent"] in {"run", "compile", "file", "output", "alternative", "illustrative", "excluded",
                                       "teardown"}
            if block["intent"] == "excluded":
                assert block["reason"].strip()
    for runtime, image in sources["runtimes"].items():
        assert "@sha256:" in image, runtime


def test_vars_files_name_declared_documents_and_cite_them():
    ids = {doc_id(d["repo"], d["path"]) for d in json.loads((HERE / "sources.json").read_text())["documents"]}
    for path in sorted((HERE / "vars").glob("*.json")):
        assert path.stem in ids, path.name
        load_vars(HERE / "vars", path.stem)


def test_guard_binds_pinned_artifacts_to_manifest_digests(monkeypatch):
    import base64
    import hashlib
    tgz, wheel, nupkg = b"npm-bytes", b"wheel-bytes", b"nupkg-bytes"
    sri = "sha512-" + base64.b64encode(hashlib.sha512(tgz).digest()).decode()
    wheel_hex, nupkg_hex = hashlib.sha256(wheel).hexdigest(), hashlib.sha256(nupkg).hexdigest()
    manifest = {"clientArtifacts": {
        "js": {"package": "@honua/sdk-js", "version": "0.1.12", "integrity": sri},
        "py": {"package": "honua-sdk", "version": "0.1.12", "filename": "honua_sdk-0.1.12-py3-none-any.whl",
               "digest": "sha256:" + wheel_hex},
        "net": {"package": "Honua.Sdk", "version": "1.10.1", "registry": "nuget.org", "digest": "sha256:" + nupkg_hex},
    }}
    pins = pins_from_manifest(manifest)
    assert pins["digests"] == {"npm": {"@honua/sdk-js": sri}, "pypi": {"honua-sdk": "sha256:" + wheel_hex},
                               "nuget": {"honua.sdk": "sha256:" + nupkg_hex}}
    assert digest_matches(tgz, sri) and not digest_matches(b"tampered", sri)
    assert digest_matches(nupkg, "sha256:" + nupkg_hex) and not digest_matches(b"tampered", "sha256:" + nupkg_hex)
    assert not digest_matches(tgz, "md5-" + base64.b64encode(hashlib.md5(tgz).digest()).decode())

    packument = lambda integrity: {"versions": {"0.1.12": {"dist": {"integrity": integrity}} if integrity else {}}}
    assert filter_npm_packument("@honua/sdk-js", packument(sri), pins["npm"], sri) is not None
    assert filter_npm_packument("@honua/sdk-js", packument("sha512-drift"), pins["npm"], sri) is None
    assert filter_npm_packument("@honua/sdk-js", packument(None), pins["npm"], sri) is None
    anchor = '<a href="https://f/honua_sdk-0.1.12-py3-none-any.whl{}">honua_sdk-0.1.12-py3-none-any.whl</a>'
    digest = pins["digests"]["pypi"]["honua-sdk"]
    assert filter_pypi_simple("honua-sdk", anchor.format("#sha256=" + wheel_hex), pins["pypi"], digest)
    assert filter_pypi_simple("honua-sdk", anchor.format("#sha256=" + "0" * 64), pins["pypi"], digest) is None
    assert filter_pypi_simple("honua-sdk", anchor.format(""), pins["pypi"], digest) is None

    class Handler:
        def __init__(self):
            self.code, self.body = None, b""
            self.wfile = self

        def send_response(self, code):
            self.code = code

        def send_header(self, *_):
            pass

        def end_headers(self):
            pass

        def write(self, data):
            self.body += data

    served = {"tgz": tgz, "nupkg": nupkg}
    monkeypatch.setattr(Guard, "_fetch", staticmethod(
        lambda url, accept=None: (200, served["tgz" if url.endswith(".tgz") else "nupkg"], "application/octet-stream")))
    guard = Guard(pins)
    try:
        def get(route, rest):
            handler = Handler()
            route(handler, rest, False)
            return handler.code
        assert get(guard._npm, "@honua/sdk-js/-/sdk-js-0.1.12.tgz") == 200
        assert get(guard._nuget_flat, "honua.sdk/1.10.1/honua.sdk.1.10.1.nupkg") == 200
        served.update(tgz=b"tampered", nupkg=b"tampered")
        assert get(guard._npm, "@honua/sdk-js/-/sdk-js-0.1.12.tgz") == 404
        assert get(guard._nuget_flat, "Honua.Sdk/1.10.1/honua.sdk.1.10.1.nupkg") == 404
        assert get(guard._nuget_flat, "honua.sdk.geoservices/1.10.1/honua.sdk.geoservices.1.10.1.nupkg") == 200
        assert {r["package"] for r in guard.refusals} == {
            "@honua/sdk-js (sdk-js-0.1.12.tgz) differs from the manifest integrity",
            "Honua.Sdk 1.10.1 differs from the manifest digest"}
    finally:
        guard.server.server_close()


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


def test_nuget_registration_keeps_only_the_pinned_leaf():
    leaf = lambda v: {"catalogEntry": {"id": "Honua.Sdk.Cli", "version": v}, "packageContent": f"x/{v}.nupkg"}
    index = {"count": 1, "items": [{"count": 3, "lower": "1.9.0", "upper": "1.10.1",
                                    "items": [leaf("1.9.0"), leaf("1.10.0"), leaf("1.10.1")]}]}
    kept = filter_nuget_registration(index, "1.10.1")
    assert [l["catalogEntry"]["version"] for l in kept["items"][0]["items"]] == ["1.10.1"]
    assert kept["items"][0]["lower"] == kept["items"][0]["upper"] == "1.10.1"
    assert filter_nuget_registration(index, "2.0.0") is None


def test_expected_failures_are_declared_or_described():
    text = ("To see it catch a deliberately broken manifest:\n\n```bash\nnode validate.mjs bad\n```\n\n"
            "<!-- doc-run: expect-fail -->\n```bash\nfalse\n```\n\n```bash\ntrue\n```\n")
    blocks = by_index(text)
    assert blocks[0].expect_failure and blocks[1].expect_failure and not blocks[2].expect_failure
    assert blocks[1].intent == "run"


def test_file_header_comments_and_replace_cues_name_files():
    blocks = by_index("```csharp\n// Program.cs\nConsole.WriteLine(1);\n```\n\n"
                      "Replace `Program.cs` with:\n\n```csharp\nConsole.WriteLine(2);\n```\n")
    assert (blocks[0].intent, blocks[0].file) == ("file", "Program.cs")
    assert (blocks[1].intent, blocks[1].file) == ("file", "Program.cs")
    assert scrub('api_key=os.environ["HONUA_API_KEY"]', []) == 'api_key=os.environ["HONUA_API_KEY"]'


@pytest.mark.parametrize("expected,actual,ok", [
    ('{"mode":"disabled"}', '{"mode":"enabled"}', False),
    ('{"count":3}', '{"count":12}', False),
    ('{"value":true}', '{"value":1}', False),
    ('{"data":{"mode":"disabled","id":"..."}}',
     '{"data":{"mode":"disabled","id":42,"extra":1}}', False),
    ('{"data":{"mode":"disabled","id":"..."}}', '{"data":{"mode":"disabled","id":42}}', True),
    ('{"data":[{"mode":"disabled"}]}', '{"data":[{"mode":"enabled"}]}', False),
])
def test_json_output_compares_values(expected, actual, ok):
    assert assert_output(expected, actual)[0] is ok


def test_unevaluated_candidate_check_fails_document():
    from run import summarize
    result = {"blocks": [{"status": "pass"}],
              "checks": [{"check": "boots-candidate-image", "status": "not-evaluated"}]}
    summarize(result)
    assert result["status"] == "fail"


@pytest.mark.parametrize("installed,pins,closure,status", [
    ({"@honua-io/old": "1.0.0"}, {"@honua/sdk": "2.0.0"}, {}, "fail"),
    ({"honua-extra": "1.0.0"}, {}, {}, "fail"),
    ({"@honua/core": "1.0.0"}, {}, {"@honua/core": "1.0.0"}, "pass"),
    ({"@honua/core": "0.9.0"}, {}, {"@honua/core": "1.0.0"}, "fail"),
    ({"@honua/sdk": "1.0.0"}, {"@honua/sdk": "2.0.0"}, {"@honua/sdk": "*"}, "fail"),
])
def test_document_rejects_url_packages_outside_admitted_set(tmp_path, installed, pins, closure, status):
    from types import SimpleNamespace
    from run import Outcome, run_document
    session = SimpleNamespace(workdir=tmp_path, env={}, passed={},
                              run_shell=lambda *args: Outcome("pass", "installed", exit_code=0),
                              installed_honua=lambda runtime: installed)
    result, _ = run_document({"runtime": "node"}, "```sh\nnpm install https://example.org/pkg.tgz\n```",
                             session, {"_pins": pins, "_closure": lambda: closure},
                             {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == status


def test_unsupported_explicit_run_fails_document(tmp_path):
    from types import SimpleNamespace
    from run import run_document
    text = "<!-- doc-run: run -->\n```ruby\nputs 'hello'\n```"
    assert extract(text, "markdown")[0].intent == "run"
    session = SimpleNamespace(workdir=tmp_path, env={})
    result, _ = run_document({"runtime": "node"}, text, session, {},
                             {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "fail"
    assert "cannot execute" in result["blocks"][0]["detail"]


@pytest.mark.parametrize("text", ["No commands here.", "```yaml\nservices: {}\n```"])
def test_zero_executed_blocks_is_a_failure(tmp_path, text):
    from types import SimpleNamespace
    from run import run_document
    result, _ = run_document({"runtime": "node"}, text, SimpleNamespace(workdir=tmp_path, env={}), {},
                             {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "fail"
    assert result["checks"] == [{"check": "nothing-executed", "status": "fail", "detail": "zero blocks executed"}]


def test_empty_docker_document_reports_unevaluated_candidate_as_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import run
    monkeypatch.setattr(run, "snapshot_containers", lambda: set())
    result, _ = run.run_document({"runtime": "node", "docker": True}, "No blocks.",
                                 SimpleNamespace(workdir=tmp_path, env={}), {},
                                 {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "fail"
    assert result["checks"][0]["status"] == "not-evaluated"
    assert result["counts"] == {"fail": 1, "not-evaluated": 1}


@pytest.mark.parametrize("error", [PermissionError("root-owned Program.cs"), RuntimeError("unexpected fault")])
def test_unexpected_block_exception_reports_failure_and_continues(tmp_path, error):
    from types import SimpleNamespace
    from run import Outcome, run_document
    def execute(code, *args):
        if "broken" in code:
            raise error
        return Outcome("pass", "exit code 0", exit_code=0)
    session = SimpleNamespace(workdir=tmp_path, env={}, passed={}, run_shell=execute,
                              installed_honua=lambda runtime: {})
    result, _ = run_document({"runtime": "node"}, "```sh\nbroken\n```\n```sh\ntrue\n```", session, {},
                             {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "fail"
    assert [b["status"] for b in result["blocks"]] == ["fail", "pass"]
    assert type(error).__name__ in result["blocks"][0]["stderrTail"]
    assert "Traceback" in result["blocks"][0]["stderrTail"]


@pytest.mark.parametrize("language,runtime,method,code", [
    ("python", "python", "run_python", "print(1)"),
    ("js", "node", "run_js", "console.log(1)"),
    ("ts", "node", "run_js", "console.log(1)"),
    ("csharp", "dotnet", "run_csharp", "Console.WriteLine(1);"),
    ("http", "node", "run_http", "GET /healthz"),
    ("sh", "node", "run_shell", "true"),
])
def test_every_executed_language_rejects_unpinned_installs(tmp_path, language, runtime, method, code):
    from types import SimpleNamespace
    from run import Outcome, run_document
    audited = []
    def installed(actual_runtime):
        audited.append(actual_runtime)
        return {"honua-extra": "9.9.9"}
    session = SimpleNamespace(workdir=tmp_path, env={}, passed={}, installed_honua=installed,
                              ensure_dotnet_project=lambda: None)
    setattr(session, method, lambda *args: Outcome("pass", "completed", exit_code=0))
    result, _ = run_document({"runtime": runtime}, f"```{language}\n{code}\n```", session,
                             {"candidate.baseUrl": "http://localhost:8080", "_pins": {}},
                             {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "fail"
    assert result["blocks"][0]["status"] == "fail"
    assert "honua-extra 9.9.9 (admitted version: none)" in result["blocks"][0]["detail"]
    assert audited == [runtime]


@pytest.mark.parametrize("expected,actual,ok", [
    ("Found 3", "Found 30", False), ("version 1.2.3", "version 1.2.4", False),
    ("done", "prefix done", False), ("done", "done trailing", False),
    ("hello world", " hello\n  world \n", True),
    ('{"a":{"b":3}}', '{"a":{"b":4}}', False),
    ('{"a":3}', 'noise\n{"a":3}', False),
    ('{"a":3}', '{\n "a": 3\n}', True),
    ('{"a":3}', '{"a":3,"extra":1}', False),
])
def test_full_output_literal_expectations(expected, actual, ok):
    assert assert_output(expected, actual)[0] is ok


def test_workflow_inventory_drift_is_fatal():
    import yaml
    gate = yaml.safe_load((HERE.parents[1] / ".github/workflows/gate-executable-docs.yml").read_text())
    step = next(s for s in gate["jobs"]["self-test"]["steps"] if "Inventory drift" in s.get("name", ""))
    assert step["run"] == "python certification/executable-docs/inventory.py --check"
    assert step.get("continue-on-error", False) is False


@pytest.mark.parametrize("readiness,expected_status", [(None, "needs-input"), ({"log": "ready"}, "pass"),
                                                      ({"log": "missing"}, "fail")])
def test_elapsed_serve_timer_never_proves_readiness(tmp_path, monkeypatch, readiness, expected_status):
    import subprocess
    import run
    session = run.Session("test", tmp_path, {}, "http://guard", tmp_path, False, "host", "test", "5.9.3")
    monkeypatch.setattr(session, "container", lambda runtime: "test-container")
    class Process:
        calls = 0
        returncode = None
        def poll(self):
            self.calls += 1
            if self.calls > 1:
                self.returncode = 124
            return self.returncode
        def wait(self):
            return self.returncode
    def popen(args, **kwargs):
        kwargs["stdout"].write("ready\n")
        kwargs["stdout"].flush()
        return Process()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(run.time, "sleep", lambda seconds: None)
    result = session.run_shell("npm run dev", "node", 600, True, readiness)
    assert result.status == expected_status
    assert result.exit_code == 124


def test_container_runs_as_host_user_with_writable_home(tmp_path, monkeypatch):
    import os
    import subprocess
    import run
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(run, "docker", docker)
    session = run.Session("test", tmp_path, {"node": "node@sha256:test"}, "http://guard", tmp_path,
                          False, "host", "test", "5.9.3")
    session.container("node")
    invocation = next(args for args in calls if args[0] == "run")
    assert invocation[invocation.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert f"HOME={session.home}" in invocation
    assert (session.home / ".nuget/NuGet/NuGet.Config").exists()


def test_inventory_check_exits_one_on_revision_drift(tmp_path, monkeypatch):
    import inventory
    committed = {"documents": [{"id": "doc", "revision": "a" * 40, "blocks": []}]}
    current = {"documents": [{"id": "doc", "revision": "b" * 40, "blocks": []}]}
    (tmp_path / "inventory.json").write_text(json.dumps(committed))
    (tmp_path / "sources.json").write_text('{"documents":[]}')
    (tmp_path / "manifest.yaml").write_text("platformRelease: 2026.1")
    monkeypatch.setattr(inventory, "build", lambda *args: current)
    monkeypatch.setattr(sys, "argv", ["inventory.py", "--check", "--sources", str(tmp_path / "sources.json"),
                                     "--manifest", str(tmp_path / "manifest.yaml"),
                                     "--inventory", str(tmp_path / "inventory.json")])
    assert inventory.main() == 1


def test_session_setup_exception_still_writes_complete_report(tmp_path, monkeypatch):
    import run
    documents = [{"repo": "honua-io/example", "path": path, "runtime": "node", "revision": {"checkout": True}}
                 for path in ("first.md", "second.md")]
    (tmp_path / "sources.json").write_text(json.dumps({"documents": documents, "runtimes": {"python": "pinned"}}))
    (tmp_path / "manifest.yaml").write_text("components:\n  honua-server:\n    image: candidate\n    digest: sha256:" + "a" * 64)
    class Resolver:
        def __init__(self, *args, **kwargs):
            pass
        def revision(self, doc):
            return "b" * 40
        def read(self, *args):
            return "```sh\ntrue\n```"
    class Guards:
        def __init__(self, *args):
            pass
        def base(self, network):
            raise PermissionError("container-created directory")
        def closure(self):
            return {}
        def refusals(self):
            return []
        def stop(self):
            pass
    monkeypatch.setattr(run, "Resolver", Resolver)
    monkeypatch.setattr(run, "Guards", Guards)
    monkeypatch.setattr(run, "prepare_tools", lambda *args: tmp_path)
    monkeypatch.delenv("HONUA_SERVER_IMAGE", raising=False)
    monkeypatch.setattr(sys, "argv", ["run.py", "--evidence-uri", "local", "--sources", str(tmp_path / "sources.json"),
                                     "--manifest", str(tmp_path / "manifest.yaml"), "--inventory", str(tmp_path / "missing"),
                                     "--workdir", str(tmp_path / "work"), "--output", str(tmp_path / "report.json"),
                                     "--summary", str(tmp_path / "summary.md")])
    assert run.main() == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "fail"
    assert [d["path"] for d in report["documents"]] == ["first.md", "second.md"]
    assert [d["blocks"][0]["status"] for d in report["documents"]] == ["fail", "fail"]
    assert all("PermissionError" in d["blocks"][0]["stderrTail"] for d in report["documents"])
    assert (tmp_path / "summary.md").exists()


def test_typecheck_audits_package_installs(tmp_path):
    from types import SimpleNamespace
    from run import Outcome, run_document
    session = SimpleNamespace(workdir=tmp_path, env={},
                              run_compile=lambda *args: Outcome("pass", "typechecked", exit_code=0),
                              installed_honua=lambda runtime: {"@honua/old": "0.0.1"})
    result, _ = run_document({"runtime": "node"}, "```ts doc-test=compile\nconst value = 1;\n```", session, {},
                             {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "fail"
    assert result["blocks"][0]["status"] == "fail"
    assert "@honua/old 0.0.1 (admitted version: none)" in result["blocks"][0]["detail"]


@pytest.mark.parametrize("http_exit,status", [(0, "pass"), (22, "fail"), (7, "fail")])
def test_readiness_url_requires_successful_http_response(tmp_path, monkeypatch, http_exit, status):
    import subprocess
    import run
    session = run.Session("url", tmp_path, {}, "http://guard", tmp_path, False, "host", "url", "5.9.3")
    monkeypatch.setattr(session, "container", lambda runtime: "url-container")
    probes = []
    def docker(*args, **kwargs):
        probes.append(args)
        return subprocess.CompletedProcess(args, http_exit, "", "")
    class Process:
        calls = 0
        returncode = None
        def poll(self):
            self.calls += 1
            if self.calls > 1:
                self.returncode = 124
            return self.returncode
        def wait(self):
            return self.returncode
    monkeypatch.setattr(run, "docker", docker)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(run.time, "sleep", lambda seconds: None)
    outcome = session.run_shell("npm run dev", "node", 600, True, {"url": "http://localhost:3000/ready"})
    assert outcome.status == status
    assert probes == [("exec", "url-container", "curl", "-fsS", "--max-time", "2", "http://localhost:3000/ready")]


@pytest.mark.parametrize("candidate,status", [(True, "pass"), (False, "fail")])
def test_docker_continuation_checks_the_existing_session_stack(tmp_path, monkeypatch, candidate, status):
    from types import SimpleNamespace
    import run
    server = {"container": "server", "service": "honua", "image": "candidate" if candidate else "old-image",
              "isCandidate": candidate}
    session = SimpleNamespace(workdir=tmp_path, env={}, passed={}, servers_seen={"server": server},
                              run_shell=lambda *args: run.Outcome("pass", "exit code 0", exit_code=0),
                              installed_honua=lambda runtime: {})
    monkeypatch.setattr(run, "snapshot_containers", lambda: {"server"})
    result, _ = run.run_document({"runtime": "node", "docker": True}, "```sh\ntrue\n```", session, {},
                                 {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == status
    assert result["checks"][0]["status"] == status
    assert result["checks"][0]["check"] == "boots-candidate-image"


def test_stopped_session_stack_cannot_satisfy_candidate_check(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import run
    session = SimpleNamespace(workdir=tmp_path, env={}, passed={},
                              servers_seen={"stopped": {"isCandidate": True}},
                              run_shell=lambda *args: run.Outcome("pass", "exit code 0", exit_code=0),
                              installed_honua=lambda runtime: {})
    monkeypatch.setattr(run, "snapshot_containers", lambda: set())
    result, _ = run.run_document({"runtime": "node", "docker": True}, "```sh\ntrue\n```", session, {},
                                 {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "fail"
    assert result["checks"][0]["status"] == "not-evaluated"


@pytest.mark.parametrize("version,status", [("1.10.1", "pass"), ("1.9.0", "fail")])
def test_dotnet_family_audit_matches_registry_guard_exact_version(tmp_path, version, status):
    from types import SimpleNamespace
    from run import Outcome, run_document
    session = SimpleNamespace(workdir=tmp_path, env={}, passed={}, ensure_dotnet_project=lambda: None,
                              run_csharp=lambda *args: Outcome("pass", "completed", exit_code=0),
                              installed_honua=lambda runtime: {"honua.sdk.cli": version})
    result, _ = run_document({"runtime": "dotnet"}, "```csharp\nConsole.WriteLine(1);\n```", session,
                             {"_pins": {"Honua.Sdk": "1.10.1"}}, {"env": {}, "substitute": {}},
                             "sha256:test", [], set())
    assert result["status"] == status


def test_python_audit_uses_metadata_without_requiring_pip(tmp_path, monkeypatch):
    import subprocess
    import run
    calls = []
    def docker(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '[{"name":"Honua_Sdk","version":"0.1.11"}]', "")
    monkeypatch.setattr(run, "docker", docker)
    session = run.Session("metadata", tmp_path, {"python": "python-pinned"}, "http://guard", tmp_path,
                          False, "host", "metadata", "5.9.3")
    session.containers["python"] = "python-container"
    assert session.installed_honua("python") == {"honua-sdk": "0.1.11"}
    assert "importlib.metadata" in calls[0][0][-1]
    assert calls[0][1]["check"] is True


def test_candidate_database_probe_requires_tcp():
    import yaml
    overlay = yaml.safe_load((HERE / "compose.readiness.yml").read_text())
    assert overlay == {"services": {"db": {"healthcheck": {
        "test": ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U honua -d honua"]}}}}


@pytest.mark.parametrize("failure_step,expected_calls", [(0, 1), (1, 2), (2, 3), (None, 3)])
def test_boot_keeps_health_licensing_and_seed_failures_fatal(monkeypatch, failure_step, expected_calls):
    import subprocess
    import run
    calls = []
    def execute(command, **kwargs):
        index = len(calls)
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if failure_step == index else 0)
    monkeypatch.setattr(subprocess, "run", execute)
    if failure_step is None:
        assert run.boot_candidate() is None
    else:
        with pytest.raises(run.RunError):
            run.boot_candidate()
    assert len(calls) == expected_calls
    assert calls[0] == ["docker", "compose", "-f", str(run.ROOT / "e2e/harness/compose.candidate.yml"),
                        "-f", str(HERE / "compose.readiness.yml"), "up", "-d"]
    if len(calls) > 1:
        assert calls[1] == ["bash", str(run.ROOT / "e2e/harness/boot.sh"), "wait"]
    if len(calls) > 2:
        assert calls[2] == ["bash", str(run.ROOT / "e2e/harness/seed/seed.sh")]
