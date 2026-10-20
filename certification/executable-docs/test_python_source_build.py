from copy import deepcopy

import pytest

from blocks import extract
import python_source_build as source

REVISION = "a" * 40
TEXT = '''# Source install
```bash
git clone https://github.com/honua-io/honua-sdk-python.git
cd honua-sdk-python
pip install ./packages/honua-sdk
pip install "./packages/honua-sdk[grpc,geopandas,raster]"
pip install ./packages/honua-sdk ./packages/honua-admin
```
# Development
```bash
pip install -e "packages/honua-sdk[grpc,geopandas]"
pip install -e "packages/honua-admin"
pip install pytest pytest-cov ruff mypy
ruff check .
```
'''


def document():
    return {"repo": source.REPOSITORY, "path": "README.md", "pythonSourceBuild": {
        "blocks": [{"index": block.index, "sha256": block.sha256}
                   for block in extract(TEXT, "markdown")]}}


def request():
    return source.derive(document(), REVISION, TEXT)


def receipts(value):
    return [{"sourceSha": value["sourceSha"], "blockIndex": block["index"],
             "blockSha256": block["sha256"], **invocation}
            for block in value["blocks"] for invocation in block["invocations"]]


def test_core_admin_and_editable_steps_remain_distinct_ordered_recipes():
    value = request()
    actual = [row for block in value["blocks"] for row in block["invocations"]]
    assert [row["requested"] for row in actual] == [
        ["./packages/honua-sdk"], ["./packages/honua-sdk[grpc,geopandas,raster]"],
        ["./packages/honua-sdk", "./packages/honua-admin"],
        ["packages/honua-sdk[grpc,geopandas]"], ["packages/honua-admin"],
        ["pytest", "pytest-cov", "ruff", "mypy"]]
    assert [row["editable"] for row in actual] == [[False], [False], [False, False], [True], [True], [False] * 4]
    source.require_exact_invocation_coverage(value, receipts(value))


@pytest.mark.parametrize("mutation", ["omit", "duplicate", "reorder", "superset", "noneditable", "wrong-source", "wrong-block"])
def test_profile_coverage_refuses_missing_or_substituted_install_steps(mutation):
    value = request()
    rows = deepcopy(receipts(value))
    if mutation == "omit": rows.pop()
    if mutation == "duplicate": rows.append(deepcopy(rows[-1]))
    if mutation == "reorder": rows[0], rows[1] = rows[1], rows[0]
    if mutation == "superset": rows[2]["requested"][0] = "./packages/honua-sdk[grpc,geopandas,raster]"
    if mutation == "noneditable": rows[3]["editable"] = [False]
    if mutation == "wrong-source": rows[0]["sourceSha"] = "b" * 40
    if mutation == "wrong-block": rows[0]["blockSha256"] = "0" * 64
    with pytest.raises(ValueError, match="exact ordered"):
        source.require_exact_invocation_coverage(value, rows)


@pytest.mark.parametrize("mutation", ["missing-dev-block", "changed-command", "mutable-source", "wrong-repo"])
def test_document_boundary_refuses_source_or_command_drift(mutation):
    doc, revision, text = document(), REVISION, TEXT
    if mutation == "missing-dev-block": doc["pythonSourceBuild"]["blocks"].pop()
    if mutation == "changed-command": text = text.replace("pip install pytest", "pip install pytest==99")
    if mutation == "mutable-source": revision = "trunk"
    if mutation == "wrong-repo": doc["repo"] = "other/repository"
    with pytest.raises(ValueError):
        source.derive(doc, revision, text)


@pytest.mark.parametrize("command", ["pip install -e ../outside", "pip install ./packages/../honua-sdk", "pip install $SOURCE", "pip install -e", "pip install https://other.example/package.whl", "pip install pytest&"])
def test_install_planning_refuses_unbound_paths_and_expansion(command):
    with pytest.raises(ValueError):
        source.pip_invocation(command)
