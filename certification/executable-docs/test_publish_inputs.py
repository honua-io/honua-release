"""Publication inputs come from the real compose database, never the published endpoint."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from inputs import load_vars, render
from publish_inputs import DOCUMENT, SQL, publication_inputs

HERE = Path(__file__).resolve().parent


def setup_probe(monkeypatch, observation=None, config=None):
    if observation is None:
        observation = {"count": 7, "validGeometryCount": 7, "integerPrimaryKey": True}
    if config is None:
        config = {"services": {"db": {"environment": {"POSTGRES_DB": "honua", "POSTGRES_USER": "honua",
                                                    "POSTGRES_PASSWORD": "owned-password"}},
                               "server": {"environment": {"ConnectionStrings__DefaultConnection":
                                "Host=db;Port=5432;Database=honua;Username=honua;Password=owned-password"}}}}
    calls = []
    def execute(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(config if "config" in args else observation))
    monkeypatch.setattr("publish_inputs.subprocess.run", execute)
    return calls


def test_measured_source_inputs_render_every_current_placeholder(monkeypatch, tmp_path):
    calls = setup_probe(monkeypatch)
    context, evidence = publication_inputs(tmp_path)
    assert context["publish.expectedSourceCount"] == "7"
    assert evidence["observation"]["count"] == 7
    assert "owned-password" not in json.dumps(evidence)
    assert calls[1][0][-1] == SQL and "exec" in calls[1][0] and "psql" in calls[1][0]
    assert all(call[1]["timeout"] == 30 for call in calls)
    variables = load_vars(HERE / "vars", DOCUMENT)
    document = next(d for d in json.loads((HERE / "inventory.json").read_text())["documents"] if d["id"] == DOCUMENT)
    placeholders = {p for b in document["blocks"] for p in b.get("needs", {}).get("placeholders", [])}
    assert len(placeholders) == 16
    for placeholder in placeholders:
        value = render(variables["substitute"][placeholder]["value"], context)
        assert "{" not in value and "<" not in value
    second, _ = publication_inputs(tmp_path)
    assert second["publish.connectionName"] != context["publish.connectionName"]


@pytest.mark.parametrize("observation", [
    {"count": 0, "validGeometryCount": 0, "integerPrimaryKey": True},
    {"count": 1001, "validGeometryCount": 1001, "integerPrimaryKey": True},
    {"count": True, "validGeometryCount": 1, "integerPrimaryKey": True},
    {"count": "3", "validGeometryCount": 3, "integerPrimaryKey": True},
    {"count": 3, "validGeometryCount": 2, "integerPrimaryKey": True},
    {"count": 3, "validGeometryCount": 3, "integerPrimaryKey": False},
    {"count": 1, "validGeometryCount": True, "integerPrimaryKey": True},
    {}, [],
])
def test_missing_or_incompatible_source_refuses(monkeypatch, tmp_path, observation):
    setup_probe(monkeypatch, observation=observation)
    with pytest.raises(ValueError, match="requires 1"):
        publication_inputs(tmp_path)


def test_mismatched_candidate_database_refuses_before_query(monkeypatch, tmp_path):
    calls = setup_probe(monkeypatch, config={"services": {"db": {"environment": {
        "POSTGRES_DB": "honua", "POSTGRES_USER": "honua", "POSTGRES_PASSWORD": "different"}},
        "server": {"environment": {"ConnectionStrings__DefaultConnection": "Host=external"}}}})
    with pytest.raises(ValueError, match="does not match"):
        publication_inputs(tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize("result", [(1, "private diagnostic"), (0, "not JSON")])
def test_probe_failure_refuses_without_exposing_output(monkeypatch, tmp_path, result):
    monkeypatch.setattr("publish_inputs.subprocess.run", lambda *a, **kw:
                        SimpleNamespace(returncode=result[0], stdout=result[1]))
    with pytest.raises(ValueError) as error:
        publication_inputs(tmp_path)
    assert result[1] not in str(error.value)


def test_image_placeholders_bind_only_to_supplied_candidate():
    for document in ("honua-site-docs", "honua-site-docs-playbooks-install-with-docker-index"):
        entry = load_vars(HERE / "vars", document)["substitute"]["<CANDIDATE_SERVER_IMAGE>"]
        assert entry["value"] == "{candidate.image}"
        assert render(entry["value"], {"candidate.image": "test@sha256:abc"}) == "test@sha256:abc"
        assert render(entry["value"], {}) == "{candidate.image}"


PREVIOUS_SAMPLES_IMAGE = (
    "ghcr.io/honua-io/honua-server:nightly-8cb074f"
    "@sha256:2d7877c4d1cb3b30647fb225893d41b13728f0448997997cdf4bf40fe99be07d"
)


def test_samples_image_binds_to_the_candidate():
    from run import substitute
    code = f"export HONUA_SERVER_IMAGE={PREVIOUS_SAMPLES_IMAGE}\n"
    for document in ("honua-samples-readme", "honua-samples-samples-wms-getmap-check-readme"):
        variables = load_vars(HERE / "vars", document)
        entry = variables["substitute"][PREVIOUS_SAMPLES_IMAGE]
        assert entry["value"] == "{candidate.image}"
        assert "nightly-8cb074f" in entry["documentedAt"]
        table = {PREVIOUS_SAMPLES_IMAGE: render(entry["value"], {"candidate.image": "test@sha256:abc"})}
        assert substitute(code, table) == 'export HONUA_SERVER_IMAGE=test@sha256:abc\n'


def test_create_honua_app_sdk_flag_binds_to_the_manifest_pin():
    from run import client_package_version, substitute
    import yaml
    variables = load_vars(HERE / "vars", "honua-sdk-js-packages-create-honua-app-readme")
    entry = variables["substitute"]["--sdk-version 0.2.6"]
    assert entry["value"] == "--sdk-version {client.sdkJs}"
    assert "0.2.6" in entry["documentedAt"]
    manifest = yaml.safe_load((HERE.parents[1] / "platform-manifest.yaml").read_text(encoding="utf-8"))
    version = client_package_version(manifest, "@honua/sdk-js")
    assert version
    code = "npm create honua-app@latest my-map -- --sdk-version 0.2.6\n"
    table = {"--sdk-version 0.2.6": render(entry["value"], {"client.sdkJs": version})}
    assert substitute(code, table) == f"npm create honua-app@latest my-map -- --sdk-version {version}\n"


def test_docs_html_channel_tag_binds_to_the_candidate():
    from run import substitute
    code = 'export HONUA_SERVER_IMAGE="ghcr.io/honua-io/honua-server:2026.1-rc"\n'
    for document in ("honua-site-docs", "honua-site-docs-playbooks-install-with-docker-index"):
        variables = load_vars(HERE / "vars", document)
        entry = variables["substitute"]["ghcr.io/honua-io/honua-server:2026.1-rc"]
        assert entry["value"] == "{candidate.image}"
        assert "2026.1-rc" in entry["documentedAt"]
        table = {key: render(value["value"], {"candidate.image": "test@sha256:abc"})
                 for key, value in variables["substitute"].items()}
        assert substitute(code, table) == 'export HONUA_SERVER_IMAGE="test@sha256:abc"\n'
