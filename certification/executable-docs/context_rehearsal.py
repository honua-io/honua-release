"""Real production-runner context control; local fixture packages are not SDK qualification."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

from blocks import extract
from registry_guard import Guard
from run import HERE, ROOT, Session, docker, prepare_tools, run_document


def _document(text: str, groups: list[tuple[str, str, list[int]]]) -> dict:
    blocks = extract(text, "markdown")
    return {"runtime": "node", "timeout": 120, "executionContexts": [
        {"name": name, "runtime": runtime, "documentedAt": "controlled context rehearsal; not a public SDK example",
         "blocks": [{"index": i, "sha256": blocks[i].sha256} for i in indices]}
        for name, runtime, indices in groups]}


def _wheel(path: Path) -> str:
    files = {"docs_context_python_probe.py": b"VALUE = 7\n",
             "docs_context_python_probe-0.0.0.dist-info/METADATA":
                 b"Metadata-Version: 2.1\nName: docs-context-python-probe\nVersion: 0.0.0\n",
             "docs_context_python_probe-0.0.0.dist-info/WHEEL":
                 b"Wheel-Version: 1.0\nGenerator: docs-context-control\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    records = io.StringIO(); writer = csv.writer(records, lineterminator="\n")
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, "sha256=" + digest, len(data)))
    record = "docs_context_python_probe-0.0.0.dist-info/RECORD"
    writer.writerow((record, "", "")); files[record] = records.getvalue().encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items(): archive.writestr(name, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rehearse(work: Path, output: Path) -> dict:
    sources = json.loads((HERE / "sources.json").read_text())
    images = sources["runtimes"]
    guard = Guard({"npm": {}, "pypi": {}, "nuget": {}}).start()
    parents: list[Session] = []
    record = {"schemaVersion": "executable-docs-context-rehearsal.v1", "qualification": False,
              "purpose": "Production runner state/routing controls with local non-Honua fixture packages and a BCL project",
              "sourceRevision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "sourceDirty": bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True).strip()),
              "runtimeImages": images, "controls": {}, "cleanup": [], "status": "fail"}
    work.mkdir(parents=True, exist_ok=True)
    try:
        tools = prepare_tools(images, work)
        def parent(name):
            value = Session(name, work / name, images, guard.base, tools, False, "host", work.name, "5.9.3")
            parents.append(value); return value
        def run(text, groups, owner):
            return run_document(_document(text, groups), text, owner, {"_pins": {}},
                {"env": {}, "substitute": {}}, "sha256:controlled-no-candidate", [], set())[0]
        positive = parent("positive")
        node = positive.execution_context("javascript", "node")
        fixture = Path(node.cwd) / "probe"; fixture.mkdir()
        (fixture / "package.json").write_text('{"name":"docs-context-node-probe","version":"0.0.0","type":"module","exports":"./index.mjs"}')
        (fixture / "index.mjs").write_text('export const value = 7;\n')
        python = positive.execution_context("python", "python")
        wheel = Path(python.cwd) / "docs_context_python_probe-0.0.0-py3-none-any.whl"
        record["pythonFixtureWheelSha256"] = _wheel(wheel)
        record["nodeFixtureSha256"] = hashlib.sha256((fixture / "package.json").read_bytes() + (fixture / "index.mjs").read_bytes()).hexdigest()
        text = f'''```bash
mkdir node-project && cd node-project
export CONTEXT_TOKEN=node NODE_ONLY=must-not-cross
npm install --offline --no-audit --no-fund --ignore-scripts ../probe
```
```js
import {{ value }} from 'docs-context-node-probe';
import {{ basename }} from 'node:path';
if (value !== 7 || process.env.CONTEXT_TOKEN !== 'node' || basename(process.cwd()) !== 'node-project'
    || (process.env.npm_config_registry ?? '') !== '{guard.base}/npm/') throw new Error('node context/state/registry mismatch');
console.log('node package readback: 7');
```
```bash
mkdir python-project && cd python-project
python3 -m venv .venv
source .venv/bin/activate
export CONTEXT_TOKEN=python
python3 -m pip install --no-index --no-deps ../{wheel.name}
```
```python
import os, sys
from pathlib import Path
from docs_context_python_probe import VALUE
assert VALUE == 7 and os.environ['CONTEXT_TOKEN'] == 'python'
assert 'NODE_ONLY' not in os.environ and Path.cwd().name == 'python-project'
assert sys.prefix.endswith('/python-project/.venv')
assert os.environ.get('PIP_INDEX_URL', '') == '{guard.base}/pypi/simple/'
print('python installed wheel readback: 7')
```
```bash
dotnet new console --no-restore -o geoprocessing && cd geoprocessing
export CONTEXT_TOKEN=dotnet
printf '%s' project-state > marker
```
```csharp
using System;
using System.IO;
if (Environment.GetEnvironmentVariable("CONTEXT_TOKEN") != "dotnet"
    || (Environment.GetEnvironmentVariable("NODE_ONLY") ?? "absent") != "absent"
    || new DirectoryInfo(Environment.CurrentDirectory).Name != "geoprocessing"
    || File.ReadAllText("marker") != "project-state"
    || !File.ReadAllText(Path.Combine(Environment.GetEnvironmentVariable("HOME") ?? "", ".nuget", "NuGet", "NuGet.Config"))
        .Contains("{guard.base}/nuget/v3/index.json")) throw new Exception("dotnet context/state/registry mismatch");
Console.WriteLine("dotnet project readback: project-state");
```
'''
        record["positiveSourceSha256"] = hashlib.sha256(text.encode()).hexdigest()
        result = run(text, [("javascript", "node", [0, 1]), ("python", "python", [2, 3]), ("dotnet", "dotnet", [4, 5])], positive)
        record["positive"] = result
        assert result["status"] == "pass" and len(result["blocks"]) == 6, result
        record["controls"]["actualPackageProjectReadback"] = "pass"
        negative = "```sh\nONLY_OTHER_CONTEXT=owned\n```\n```sh\necho $ONLY_OTHER_CONTEXT\n```\n"
        result = run(negative, [("node", "node", [0]), ("python", "python", [1])], parent("separate-env"))
        assert [b["status"] for b in result["blocks"]] == ["pass", "needs-input"], result
        record["controls"]["crossContextInputRefused"] = "pass"
        negative = "```bash\nfalse\n```\n```python\nraise RuntimeError('must never execute after failed setup')\n```\n"
        result = run(negative, [("python", "python", [0, 1])], parent("failed-setup"))
        assert [b["status"] for b in result["blocks"]] == ["fail", "fail"] and "earlier block" in result["blocks"][1]["detail"], result
        record["controls"]["failedSetupCannotPassLater"] = "pass"
        negative = "```csharp\nConsole.WriteLine(1);\n```\n"
        owner = parent("absent-project")
        result = run(negative, [("dotnet", "dotnet", [0])], owner)
        assert result["status"] == "fail" and "exactly one project" in result["blocks"][0]["detail"], result
        assert not list(owner.workdir.rglob("*.csproj"))
        record["controls"]["missingProjectCannotBeFabricated"] = "pass"
        assert not guard.refusals, guard.refusals
        record["status"] = "pass"
    finally:
        names = [name for owner in parents for child in owner.contexts.values() for name in child.containers.values()]
        for owner in parents: owner.close()
        guard.stop()
        for name in names:
            absent = docker("inspect", name).returncode != 0
            record["cleanup"].append({"container": name, "absent": absent})
            if not absent: record["status"] = "fail"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(record, indent=2) + "\n")
    assert record["status"] == "pass" and all(c["absent"] for c in record["cleanup"]), record
    return record
