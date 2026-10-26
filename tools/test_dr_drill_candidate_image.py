"""The DR drills must run the candidate's server image under both manifest image forms.

Checked-in trunk pins name the server by tag (`ghcr.io/...:nightly-<sha7>`); the nightly's resolved
candidate names it by digest (`ghcr.io/...@sha256:<hex>`). Both drills used to build the run
reference by string surgery that only worked for the tag form, so the first nightly-dispatched
drill would have asked docker for `...@sha256@sha256:<hex>` (full_platform.py) or
`...@sha256:<hex>@sha256:<hex>` (run.sh).
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "d" * 64
REPOSITORY = "ghcr.io/honua-io/honua-server"
FORMS = {
    "tag": f"{REPOSITORY}:nightly-8cb074f",
    "digest": f"{REPOSITORY}@{DIGEST}",
    "tag-and-digest": f"{REPOSITORY}:nightly-8cb074f@{DIGEST}",
    "registry-port": f"localhost:5000/honua-server:nightly-8cb074f",
}
EXPECTED = {
    "tag": f"{REPOSITORY}@{DIGEST}",
    "digest": f"{REPOSITORY}@{DIGEST}",
    "tag-and-digest": f"{REPOSITORY}@{DIGEST}",
    "registry-port": f"localhost:5000/honua-server@{DIGEST}",
}


def _full_platform():
    sys.path.insert(0, str(ROOT / "e2e"))
    sys.path.insert(0, str(ROOT / "e2e/dr-drill"))
    spec = importlib.util.spec_from_file_location("dr_full_platform", ROOT / "e2e/dr-drill/full_platform.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("form", sorted(FORMS))
def test_full_platform_pins_the_candidate_digest_for_every_image_form(form):
    assert _full_platform().pinned_image(FORMS[form], DIGEST) == EXPECTED[form]


@pytest.mark.parametrize("form", sorted(FORMS))
def test_scoped_restore_drill_pins_the_candidate_digest_for_every_image_form(form, tmp_path):
    script = (ROOT / "e2e/dr-drill/run.sh").read_text(encoding="utf-8")
    match = re.search(r"readarray -t LOCK < <\(python3 - \"\$ROOT/platform-manifest.yaml\" <<'PY'\n(.*?)\nPY\n",
                      script, re.S)
    assert match, "run.sh no longer reads its pins through the inline manifest reader"
    manifest = tmp_path / "platform-manifest.yaml"
    manifest.write_text(
        "platformRelease: 2026.1-rc.3\ncomponents:\n  honua-server:\n"
        f"    sha: {'a' * 40}\n    image: {FORMS[form]}\n    digest: {DIGEST}\n    dbSchema: 129\n",
        encoding="utf-8")
    lines = subprocess.run([sys.executable, "-", str(manifest)], input=match.group(1), text=True,
                           capture_output=True, check=True).stdout.splitlines()
    assert lines[2] == EXPECTED[form]
    assert lines[3] == DIGEST
