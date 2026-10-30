"""Mixed-language routing keeps real setup state bounded to its declared context."""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from blocks import extract  # noqa: E402
from execution_contexts import ContextError, compile_plan  # noqa: E402
from inventory import InventoryError, document_record  # noqa: E402
from inputs import load_vars, needs, render  # noqa: E402
from run import Outcome, RunError, Session, run_document  # noqa: E402


def declaration(text, groups):
    blocks = extract(text, "markdown")
    return {"repo": "test/docs", "path": "guide.md", "revision": {"checkout": True}, "runtime": "node",
            "executionContexts": [{"name": name, "runtime": runtime, "documentedAt": "guide.md: language example",
                "blocks": [{"index": i, "sha256": blocks[i].sha256} for i in indices]}
                for name, runtime, indices in groups]}


def session(tmp_path):
    return Session("contexts", tmp_path, {r: r + "@sha256:controlled" for r in ("node", "python", "dotnet")},
                   "http://guard", tmp_path / "tools", False, "host", "test", "5.9.3", prerequisites={"jq"})


def execute(doc, text, parent):
    return run_document(doc, text, parent, {"_pins": {}}, {"env": {}, "substitute": {}}, "sha256:test", [], set())[0]


def gp_source():
    sources = json.loads((HERE / "sources.json").read_text())
    doc = next(d for d in sources["documents"] if d["repo"] == "honua-io/honua-site"
               and d["path"] == "docs/geoprocessing/index.md")
    return doc, (HERE / "fixtures/site-geoprocessing-b56533b9fd2c.md").read_text()


def test_actual_guide_binds_all_six_frozen_blocks_and_only_documented_key():
    doc, text = gp_source()
    # Published geoprocessing page bytes are the b56533b9fd2c fixture. The inventory
    # revision is the last commit on the default branch that changed the file.
    # The two shell install blocks are @honua/sdk-js@0.2.10 and honua-sdk==0.1.19.
    assert hashlib.sha256(text.encode()).hexdigest() == "6a10734164314cb3c85ee9e82756488f842ff86f8bf50c8639e9197cb14f2f20"
    plan = compile_plan(doc, extract(text, "markdown"))
    frozen = json.loads((HERE / "inventory.json").read_text())
    row = next(d for d in frozen["documents"] if d["id"] == "honua-site-docs-geoprocessing-index")
    assert row["revision"] == "477350e54f7a29ee63cbea112e317de3ba6a2b15"
    assert [b.sha256 for b in extract(text, "markdown")] == [b["sha256"] for b in row["blocks"]]
    assert [plan[i]["runtime"] for i in range(6)] == ["node", "node", "python", "python", "dotnet", "dotnet"]
    variables = load_vars(HERE / "vars", row["id"])
    assert set(variables["substitute"]) == {"<your-api-key>"}
    assert render(variables["substitute"]["<your-api-key>"]["value"], {"candidate.apiKey": "owned"}) == "owned"
    assert {p for b in extract(text, "markdown") for p in needs(b, set())["placeholders"]} == {"<your-api-key>"}
    assert len(frozen["documents"]) == 37


@pytest.mark.parametrize("mutation", ["absent-block", "unknown-key", "runtime", "wrong-language", "hash", "bool-index",
    "range", "duplicate-block", "duplicate-name", "traversal", "citation", "empty", "reorder", "binding-key",
    "binding-type", "declaration-type", "session", "checkout", "sourceBuild", "docker"])
def test_invalid_contexts_refuse_before_runtime_and_inventory(mutation):
    doc, text = gp_source(); doc = copy.deepcopy(doc)
    entries = doc["executionContexts"]; first = entries[0]
    if mutation == "absent-block": first["blocks"].pop()
    if mutation == "unknown-key": first["optional"] = True
    if mutation == "runtime": first["runtime"] = "sh"
    if mutation == "wrong-language": first["runtime"] = "python"
    if mutation == "hash": first["blocks"][0]["sha256"] = "f" * 64
    if mutation == "bool-index": first["blocks"][0]["index"] = False
    if mutation == "range": first["blocks"][0]["index"] = 100
    if mutation == "duplicate-block": first["blocks"].append(first["blocks"][0])
    if mutation == "duplicate-name": entries[1]["name"] = first["name"]
    if mutation == "traversal": first["name"] = "../escape"
    if mutation == "citation": first["documentedAt"] = " "
    if mutation == "empty": doc["executionContexts"] = []
    if mutation == "reorder": first["blocks"].reverse()
    if mutation == "binding-key": first["blocks"][0]["extra"] = True
    if mutation == "binding-type": first["blocks"][0] = []
    if mutation == "declaration-type": entries[0] = []
    if mutation in {"session", "checkout", "sourceBuild", "docker"}: doc[mutation] = True
    with pytest.raises(ContextError): compile_plan(doc, extract(text, "markdown"))
    with pytest.raises(InventoryError): document_record(doc, "a" * 40, text)


TEXT = "```bash\nNODE_ONLY=secret\n```\n```js\nconsole.log('node');\n```\n" \
       "```bash\npython3 -m pip install probe\n```\n```python\nimport probe\n```\n" \
       "```bash\ndotnet new console -o app && cd app\n```\n```csharp\nConsole.WriteLine(1);\n```\n"
GROUPS = [("javascript", "node", [0, 1]), ("python", "python", [2, 3]), ("dotnet", "dotnet", [4, 5])]


def test_setup_and_snippets_use_persistent_isolated_state(tmp_path, monkeypatch):
    parent = session(tmp_path); calls = []
    def shell(child, code, runtime, timeout, serve):
        assert set(child.runtimes) == {runtime} and child.prerequisites == {"jq"}
        assert child.home != parent.home and "NODE_ONLY" not in child.env
        child.env[runtime.upper() + "_ONLY"] = "owned"
        child.cwd = str(child.workdir / "app" / "project")
        Path(child.cwd).mkdir(); (Path(child.cwd) / "installed").write_text(runtime)
        calls.append((runtime, child))
        return Outcome("pass", "setup")
    def snippet(child, *args):
        runtime = next(iter(child.runtimes))
        assert child.env == {runtime.upper() + "_ONLY": "owned"}
        assert (Path(child.cwd) / "installed").read_text() == runtime
        calls.append((runtime, child)); return Outcome("pass", "readback")
    monkeypatch.setattr(Session, "run_shell", shell)
    for method in ("run_js", "run_python", "run_csharp"): monkeypatch.setattr(Session, method, snippet)
    result = execute(declaration(TEXT, GROUPS), TEXT, parent)
    assert result["status"] == "pass" and len(result["blocks"]) == 6
    assert all(calls[i][1] is calls[i + 1][1] for i in (0, 2, 4))
    assert len({id(c[1]) for c in calls}) == 3
    assert [r["executionContext"]["runtime"] for r in result["blocks"]] == [c[0] for c in calls]
    assert not parent.env and not parent.passed


@pytest.mark.parametrize("failed", ["node", "python", "dotnet"])
def test_failed_setup_cannot_be_replaced_by_later_snippet_success(tmp_path, monkeypatch, failed):
    def shell(child, code, runtime, timeout, serve):
        return Outcome("fail" if runtime == failed else "pass", "setup", command_failed=runtime == failed)
    ran = []
    def snippet(child, *args):
        runtime = next(iter(child.runtimes)); assert runtime != failed
        ran.append(runtime); return Outcome("pass", "snippet")
    monkeypatch.setattr(Session, "run_shell", shell)
    for method in ("run_js", "run_python", "run_csharp"): monkeypatch.setattr(Session, method, snippet)
    result = execute(declaration(TEXT, GROUPS), TEXT, session(tmp_path))
    assert result["status"] == "fail" and len(result["blocks"]) == 6
    assert len(ran) == 2
    assert [r["status"] for r in result["blocks"] if r["executionContext"]["runtime"] == failed] == ["fail", "fail"]


def test_export_in_another_context_does_not_satisfy_required_input(tmp_path, monkeypatch):
    text = "```sh\nONLY_NODE=owned\n```\n```sh\necho $ONLY_NODE\n```\n"
    doc = declaration(text, [("javascript", "node", [0]), ("python", "python", [1])])
    calls = []
    def shell(child, code, runtime, *args):
        calls.append(runtime); child.env["ONLY_NODE"] = "owned"; return Outcome("pass", "setup")
    monkeypatch.setattr(Session, "run_shell", shell)
    result = execute(doc, text, session(tmp_path))
    assert calls == ["node"] and result["blocks"][1]["status"] == "needs-input"
    inventory, _ = document_record(doc, "a" * 40, text)
    assert inventory["blocks"][1]["needs"]["env"] == ["ONLY_NODE"]


@pytest.mark.parametrize("projects", [0, 2])
def test_explicit_dotnet_context_never_scaffolds_a_replacement_project(tmp_path, monkeypatch, projects):
    child = session(tmp_path).execution_context("dotnet", "dotnet")
    for i in range(projects): (Path(child.cwd) / f"{i}.csproj").write_text("<Project/>")
    monkeypatch.setattr(child, "container", lambda runtime: "owned")
    monkeypatch.setattr(child, "_exec", lambda *args: pytest.fail("must not silently scaffold/run"))
    result = child.run_csharp("Console.WriteLine(1);", [], 10)
    assert result.status == "fail" and "exactly one project" in result.detail
    assert len(list(Path(child.cwd).glob("*.csproj"))) == projects


@pytest.mark.parametrize("name,runtime", [("../escape", "node"), ("safe", "shell"), (None, "python")])
def test_direct_context_creation_refuses_unknown_runtime_and_unsafe_path(tmp_path, name, runtime):
    parent = session(tmp_path)
    with pytest.raises(RunError): parent.execution_context(name, runtime)
    assert not parent.contexts


def test_context_cannot_change_runtime_and_parent_closes_every_owned_container(tmp_path, monkeypatch):
    import run
    parent = session(tmp_path); removed = []
    for runtime in ("node", "python", "dotnet"):
        child = parent.execution_context(runtime, runtime); child.containers[runtime] = "owned-" + runtime
    with pytest.raises(RunError): parent.execution_context("node", "python")
    monkeypatch.setattr(run, "docker", lambda *args: removed.append(args))
    parent.close()
    assert set(removed) == {("rm", "-f", "owned-" + r) for r in ("node", "python", "dotnet")}
    assert not parent.contexts


def test_deferred_typecheck_returns_to_its_own_setup_context(tmp_path, monkeypatch):
    text = "```sh\nTOKEN=node\n```\n```ts doc-test=compile\nconst value: number = 7;\n```\n```sh\nTOKEN=python\n```\n"
    doc = declaration(text, [("node", "node", [0, 1]), ("python", "python", [2])])
    def shell(child, code, runtime, *args):
        child.env["TOKEN"] = runtime; return Outcome("pass", "setup")
    def compile_(child, *args):
        assert set(child.runtimes) == {"node"} and child.env["TOKEN"] == "node"
        return Outcome("pass", "typechecked")
    monkeypatch.setattr(Session, "run_shell", shell); monkeypatch.setattr(Session, "run_compile", compile_)
    result = execute(doc, text, session(tmp_path))
    assert result["status"] == "pass" and result["blocks"][1]["executionContext"]["name"] == "node"


def test_other_context_filename_cannot_suppress_file_execution(tmp_path, monkeypatch):
    text = "<!-- doc-run: file=probe.py -->\n```python\nprint(7)\n```\n```bash\nprintf '%s' probe.py\n```\n"
    doc = declaration(text, [("python", "python", [0]), ("node", "node", [1])]); ran = []
    monkeypatch.setattr(Session, "put", lambda child, runtime, path, code: path.write_text(code))
    def python(child, *args):
        ran.append(child); return Outcome("pass", "file actually ran")
    monkeypatch.setattr(Session, "run_python", python)
    monkeypatch.setattr(Session, "run_shell", lambda *args: Outcome("pass", "shell"))
    parent = session(tmp_path); result = execute(doc, text, parent)
    assert result["status"] == "pass" and ran == [parent.contexts["python"]]


def test_long_parent_and_same_runtime_contexts_have_distinct_actual_container_names(tmp_path, monkeypatch):
    import subprocess
    import run
    parent = session(tmp_path); parent.name = "shared-parent-prefix-" * 6
    first = parent.execution_context("shared-prefix-first", "python")
    second = parent.execution_context("shared-prefix-second", "python")
    calls = []
    def docker(*args, **kwargs):
        calls.append(args); return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(run, "docker", docker)
    a, b = first.container("python"), second.container("python")
    assert a != b and first.container("python") == a
    starts = [args[args.index("--name") + 1] for args in calls if args[0] == "run"]
    assert starts == [a, b]
    # Startup of the second context must never delete the first's container.
    assert [args for args in calls if args[0] == "rm"] == [("rm", "-f", a), ("rm", "-f", b)]
