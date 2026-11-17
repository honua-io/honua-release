"""The catalog regenerates and validates after the server renames a lane or demotes a capability."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MATRIX = "sources/server/capability-matrix.v1.json"
CATALOG = "protocol-certification-requirements.v1.json"
REALTIME = "streaming.feature-subscriptions"
RENAMED = "geoservices-rest-stub"


@pytest.fixture
def certification(tmp_path):
    target = tmp_path / "certification"
    shutil.copytree(ROOT / "certification", target, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "platform-manifest.yaml", tmp_path / "platform-manifest.yaml")
    return target


def regenerate_and_validate(certification):
    for script in ("generate-protocol-requirements.py", "validate-protocol-requirements.py"):
        result = subprocess.run([sys.executable, str(certification / script)],
                                capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stderr
    return json.loads((certification / CATALOG).read_text())["requirements"]


def test_experimental_realtime_streams_drop_their_licensed_js_rows(certification):
    matrix = json.loads((certification / MATRIX).read_text())
    realtime = next(row for row in matrix["capabilities"] if row["key"] == REALTIME)
    entries = sum(realtime["maturity"].values())
    realtime.update(status="experimental", maturity={"experimental": entries})
    (certification / MATRIX).write_text(json.dumps(matrix, indent=2) + "\n")

    rows = regenerate_and_validate(certification)

    assert not [row for row in rows if row["capability_key"] == REALTIME]
    assert not [row for row in rows if row["client_lane"] == "sdk-js-certification" and row["licensed"]]


@pytest.mark.parametrize("lane", [RENAMED, "arcgis-stub"])
def test_renamed_geoservices_rest_stub_lane_keeps_its_disposition(certification, lane):
    # The 532565bc snapshot already names the new lane; the older id keeps its disposition too.
    path = certification / MATRIX
    text = path.read_text()
    assert set(re.findall(r'"clientLane": "([^"]+-stub)"', text)) == {RENAMED}
    path.write_text(text.replace(f'"{RENAMED}"', f'"{lane}"'))

    rows = regenerate_and_validate(certification)

    lanes = {row["client_lane"] for row in rows if row["addressable_by_client"]}
    assert lane in lanes
