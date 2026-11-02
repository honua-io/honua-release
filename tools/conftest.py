"""Registry evidence used by architecture and rollback acceptance tests."""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def registry_images():
    images = {}
    for path in (Path(__file__).parent / "fixtures/image-platforms").glob("*/index.json"):
        raw = path.read_bytes()
        index = json.loads(raw)
        platforms = {child["platform"]["architecture"]: child["digest"]
                     for child in index["manifests"] if child["platform"]["os"] == "linux"}
        images[path.parent.name] = {
            "kind": "image", "coordinate": f"ghcr.io/honua-io/{path.parent.name}",
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "architectures": list(platforms), "platformDigests": platforms,
        }
    return images


@pytest.fixture
def registry_docker(tmp_path, monkeypatch, registry_images):
    """Replay raw registry bytes at the subprocess boundary; all verification runs unchanged."""
    fixture_root = Path(__file__).parent / "fixtures/image-platforms"
    responses = {f"{image['coordinate']}@{image['digest']}":
                 str(fixture_root / name / "index.json") for name, image in registry_images.items()}
    binary = tmp_path / "bin/docker"
    binary.parent.mkdir()
    binary.write_text(f"#!{sys.executable}\n" +
                      "import pathlib, sys\n" +
                      f"responses = {responses!r}\n" +
                      "assert sys.argv[1:4] == ['buildx', 'imagetools', 'inspect']\n" +
                      "assert sys.argv[5:] == ['--raw']\n" +
                      "sys.stdout.buffer.write(pathlib.Path(responses[sys.argv[4]]).read_bytes())\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    return registry_images


@pytest.fixture(autouse=True)
def _never_hash_the_real_sealed_held_out_set(monkeypatch, tmp_path_factory):
    """Tests never read the committed sealed held-out files (not even to hash them): the runner's SEALED paths
    point at nonexistent files unless a test seals its own (they monkeypatch SEALED themselves)."""
    module = sys.modules.get('ai_arc_runner')
    if module is None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import ai_arc_runner as module  # noqa: F401
        except Exception:  # noqa: BLE001
            return
    missing = tmp_path_factory.getbasetemp() / 'no-sealed-set'
    monkeypatch.setattr(module, 'SEALED', {'primary': missing / 'primary.sealed', 'reserve': missing / 'reserve.sealed'})
