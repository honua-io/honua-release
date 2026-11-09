import json
from pathlib import Path
import shutil

import pytest

import deployment_configuration as configuration


@pytest.fixture
def retained(tmp_path):
    shutil.copytree(configuration.ROOT / "deployment-configurations",
                    tmp_path / "deployment-configurations")
    return tmp_path


def image(root):
    return next(iter(json.loads((root / configuration.CATALOG).read_text())["images"]))


def rewrite(root, mutate):
    path = root / configuration.CATALOG
    catalog = json.loads(path.read_text())
    entry = next(iter(catalog["images"].values()))
    mutate(entry)
    path.write_text(json.dumps(catalog))


def test_retained_rc1_is_manual_and_checksums_match(retained):
    selected = configuration.select(retained, image(retained), "compose")
    assert selected["release"] == "2026.1-rc.1"
    assert selected["rollbackMode"] == "manual"
    compose = Path(selected["files"][0]).read_text()
    assert "${HONUA_IMAGE:?Set the server image}" in compose
    assert "/app/honua-entrypoint" not in compose
    assert selected["source"]["revision"] == "a4bde4b8c5ee8cc3dec05ad8b3d4f84470c1ea6e"


@pytest.mark.parametrize("requested", ["ghcr.io/honua-io/honua-server:latest",
                                      "ghcr.io/honua-io/honua-server@sha256:" + "0" * 64])
def test_unknown_images_are_refused(retained, requested):
    with pytest.raises(ValueError, match="not retained"):
        configuration.select(retained, requested, "compose")


def test_unretained_helm_configuration_is_refused(retained):
    with pytest.raises(ValueError, match="no retained helm"):
        configuration.select(retained, image(retained), "helm")


def test_changed_configuration_is_refused(retained):
    path = retained / "deployment-configurations/2026.1-rc.1/compose.yaml"
    path.write_text(path.read_text().replace("read_only: true", "read_only: false"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        configuration.select(retained, image(retained), "compose")


@pytest.mark.parametrize("path", ["../outside.yaml", "/etc/passwd", "README.md"])
def test_paths_outside_catalog_are_refused(retained, path):
    (retained / "README.md").write_text("outside configuration catalog")
    rewrite(retained, lambda entry: entry["configurations"]["compose"][0].update(path=path))
    with pytest.raises(ValueError):
        configuration.select(retained, image(retained), "compose")


def test_symlink_escape_is_refused(retained):
    outside = retained / "outside.yaml"
    outside.write_text("outside configuration catalog")
    (retained / "deployment-configurations/link.yaml").symlink_to(outside)
    rewrite(retained, lambda entry: entry["configurations"]["compose"][0].update(
        path="deployment-configurations/link.yaml"))
    with pytest.raises(ValueError, match="escapes"):
        configuration.select(retained, image(retained), "compose")


def test_cli_refuses_corrupt_catalog_without_selection(retained, capsys):
    rewrite(retained, lambda entry: entry.update(rollbackMode="automatic-rc1"))
    assert configuration.main(["--root", str(retained), "--image", image(retained)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "DEPLOYMENT_CONFIGURATION_REFUSED" in output.err


def test_committed_catalog_is_valid():
    assert configuration.main(["--check"]) == 0


RC1_INDEX = "ghcr.io/honua-io/honua-server@sha256:c8d7aef98761618145725530b6a4e4b25ea4a9ae121da168362f9be4cf5f7d37"


def test_published_release_identity_selects_configuration(retained):
    assert configuration.select(retained, RC1_INDEX, "compose")["release"] == "2026.1-rc.1"


@pytest.mark.parametrize("mutate", [
    lambda entry: entry.pop("platformDigests"),
    lambda entry: entry.update(platformDigests={}),
    lambda entry: entry["platformDigests"].update({"linux/amd64": "sha256:bad"}),
    lambda entry: entry["platformDigests"].update({"windows/amd64": "sha256:" + "0" * 64}),
    lambda entry: entry.pop("release"),
    lambda entry: entry.pop("source"),
    lambda entry: entry["source"].update(revision="mutable-branch"),
    lambda entry: entry["source"].update(repository=""),
    lambda entry: entry["source"].update(path=""),
    lambda entry: entry.update(configurations={}),
    lambda entry: entry["configurations"].update(kustomize=[]),
    lambda entry: entry["configurations"]["compose"][0].update(sha256="bad"),
    lambda entry: entry.update(unexpected="value"),
])
def test_check_refuses_invalid_entry(retained, mutate, capsys):
    rewrite(retained, mutate)
    assert configuration.main(["--root", str(retained), "--check"]) == 1
    assert not capsys.readouterr().out


@pytest.mark.parametrize("mutate", [
    lambda catalog: catalog.update(schema="unsupported"),
    lambda catalog: catalog.update(images={}),
    lambda catalog: catalog.update(images={"ghcr.io/honua-io/honua-server:latest": next(iter(catalog["images"].values()))}),
])
def test_check_refuses_invalid_catalog(retained, mutate, capsys):
    path = retained / configuration.CATALOG
    catalog = json.loads(path.read_text())
    mutate(catalog)
    path.write_text(json.dumps(catalog))
    assert configuration.main(["--root", str(retained), "--check"]) == 1
    assert not capsys.readouterr().out


def test_check_refuses_duplicate_keys(retained, capsys):
    path = retained / configuration.CATALOG
    text = path.read_text()
    path.write_text(text.replace('"release":', '"release": "ambiguous", "release":', 1))
    assert configuration.main(["--root", str(retained), "--check"]) == 1
    assert not capsys.readouterr().out
