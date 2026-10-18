from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("python_wheel_generator", Path(__file__).parents[1] / "certification/generate-protocol-requirements.py")
GEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GEN)
SHA = "c" * 40
WHEEL = {"filename": "honua_sdk-0.1.16-py3-none-any.whl", "sha256": "d" * 64, "source": "pypi"}


def manifest():
    return {"components": {"honua-sdk-python": {"sha": SHA, "version": "0.1.16"}},
            "clientArtifacts": {"honua-sdk-python-wheel": {"sourceSha": SHA, "version": "0.1.16",
                "repository": "honua-io/honua-sdk-python", "ecosystem": "pypi", "package": "honua-sdk",
                "publicationState": "published", "filename": WHEEL["filename"], "digest": "sha256:" + WHEEL["sha256"]}}}


def test_independent_selected_manifest_owns_exact_public_wheel():
    assert GEN.python_wheel_identity(manifest(), "0.1.16", SHA) == WHEEL
    for group, field, value in (("component", "sha", "e" * 40), ("component", "version", "0.1.15"),
            ("artifact", "sourceSha", "e" * 40), ("artifact", "version", "0.1.15"),
            ("artifact", "repository", "attacker/sdk"), ("artifact", "publicationState", "candidate"),
            ("artifact", "package", "other"), ("artifact", "ecosystem", "local"),
            ("artifact", "filename", "honua_sdk-0.1.15-py3-none-any.whl"),
            ("artifact", "digest", "sha256:" + "D" * 64), ("artifact", "digest", "")):
        bad = deepcopy(manifest())
        target = bad["components"]["honua-sdk-python"] if group == "component" else bad["clientArtifacts"]["honua-sdk-python-wheel"]
        target[field] = value
        with pytest.raises(ValueError, match="coherent published"):
            GEN.python_wheel_identity(bad, "0.1.16", SHA)


def test_generated_python_identity_preserves_every_other_requirement(monkeypatch, tmp_path):
    monkeypatch.setattr(GEN, "OUTPUT", tmp_path / "requirements.json")
    monkeypatch.setattr(GEN, "selected_python_wheel", lambda _: None)
    GEN.main()
    before = json.loads(GEN.OUTPUT.read_text())
    monkeypatch.setattr(GEN, "selected_python_wheel", lambda _: dict(WHEEL))
    GEN.main()
    after = json.loads(GEN.OUTPUT.read_text())
    assert len(before["requirements"]) == len(after["requirements"])
    assert before["source_revisions"] == after["source_revisions"]
    changed = 0
    for old, new in zip(before["requirements"], after["requirements"]):
        if old["client_lane"] == "sdk-python-certification":
            assert new.pop("client_package") == WHEEL
            changed += 1
        assert old == new
    assert changed > 0
