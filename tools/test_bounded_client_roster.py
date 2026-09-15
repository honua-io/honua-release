"""The bounded 2026.1 external-client roster joins one receipt result per row (release#346).

honua-evidence's ``client-interop-cert-v1`` normalizer narrows a receipt to
``(client_lane, client_version, surface)`` and rejects the whole receipt unless each
result's ``test_case_id`` resolves to exactly one requirement there. These tests hold
the generated denominator to that rule and prove the generator and validator fail
closed when a bounded row cannot join.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CERTIFICATION = ROOT / "certification"
CATALOG = "protocol-certification-requirements.v1.json"
ROSTER = "sources/bounded-client-roster.v1.json"
QGIS_WFS = ("QGIS", "wfs", "serve.wfs")
QGIS_WFS_TEST_ID = "client-cert/qgis/wfs/serve.wfs"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _resolve(requirements: list[dict], row: dict, test_id: str) -> list[dict]:
    return [
        candidate for candidate in requirements
        if candidate["client_lane"] == row["client_lane"]
        and candidate["client_version"] == row["client_version"]
        and candidate["surface"] == row["surface"]
        and test_id in candidate.get("test_ids", [])
    ]


def _row(catalog: dict, key: tuple[str, str, str]) -> dict:
    return next(
        row for row in catalog["requirements"]
        if (row["canonical_client"], row["surface"], row["operation"]) == key
    )


def test_every_bounded_row_resolves_exactly_one_receipt_result():
    catalog = _load(CERTIFICATION / CATALOG)
    roster = _load(CERTIFICATION / ROSTER)
    bounded = [row for row in catalog["requirements"] if row["canonical_client"] in roster["clients"]]

    assert len(bounded) == len(roster["cells"]) == 59
    for row in bounded:
        assert len(row["test_ids"]) == 1, row
        assert _resolve(catalog["requirements"], row, row["test_ids"][0]) == [row]


def test_denominator_reflects_the_qgis_and_lane_identity_rulings():
    catalog = _load(CERTIFICATION / CATALOG)
    roster = _load(CERTIFICATION / ROSTER)
    lanes = {row["client_lane"] for row in catalog["requirements"]}

    for name, client in roster["clients"].items():
        rows = [row for row in catalog["requirements"] if row["canonical_client"] == name]
        allowed = {client["client_lane"], *client.get("retained_producer_lanes", {})}
        assert {row["client_lane"] for row in rows} <= allowed, name
        assert not lanes & set(client["replaced_lanes"]), name
        if client["client_version"] is not None:
            assert {row["client_version"] for row in rows} == {client["client_version"]}, name
    assert roster["clients"]["QGIS"]["client_version"] == "3.44.13-Solothurn"


@pytest.fixture
def certification_copy(tmp_path: Path) -> Path:
    target = tmp_path / "certification"
    shutil.copytree(
        CERTIFICATION, target,
        ignore=shutil.ignore_patterns(
            "__pycache__", "installed-clients", "terminal-journey", "first-publication", "release-controls",
        ),
    )
    return target


def _run(certification: Path, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(certification / script)],
        cwd=certification, capture_output=True, text=True, timeout=600,
    )


def _drop_test_ids(catalog: dict) -> None:
    del _row(catalog, QGIS_WFS)["test_ids"]


def _add_second_test_id(catalog: dict) -> None:
    _row(catalog, QGIS_WFS)["test_ids"].append("CERT-CONN-01")


def _restore_per_surface_lane(catalog: dict) -> None:
    _row(catalog, ("OWSLib", "wcs", "serve.wcs"))["client_lane"] = "owslib-wcs"


def _share_a_bounded_test_id(catalog: dict) -> None:
    qgis = _row(catalog, QGIS_WFS)
    rival = dict(_row(catalog, ("OGC CITE", "wfs", "serve.wfs")))
    rival.update(client_lane=qgis["client_lane"], client_version=qgis["client_version"], test_ids=[QGIS_WFS_TEST_ID])
    catalog["requirements"].append(rival)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_drop_test_ids, "must carry lane 'desktop-qgis' and exactly test_ids"),
        (_add_second_test_id, "must carry lane 'desktop-qgis' and exactly test_ids"),
        (_restore_per_surface_lane, "must carry lane 'py-owslib'"),
        (_share_a_bounded_test_id, "resolve to more than one requirement"),
    ],
)
def test_validator_rejects_a_bounded_row_that_cannot_join(
    certification_copy: Path, mutate: Callable[[dict], None], message: str,
):
    catalog = _load(certification_copy / CATALOG)
    mutate(catalog)
    _write(certification_copy / CATALOG, catalog)

    result = _run(certification_copy, "validate-protocol-requirements.py")

    assert result.returncode != 0
    assert message in result.stderr


def test_generator_refuses_a_bounded_row_without_a_governed_test_id(certification_copy: Path):
    roster = _load(certification_copy / ROSTER)
    roster["cells"] = [
        cell for cell in roster["cells"]
        if (cell["canonical_client"], cell["surface"], cell["operation"]) != QGIS_WFS
    ]
    _write(certification_copy / ROSTER, roster)

    result = _run(certification_copy, "generate-protocol-requirements.py")

    assert result.returncode != 0
    assert "bounded-roster requirement has no governed test ID: ('QGIS', 'wfs', 'serve.wfs')" in result.stderr


def test_validator_rejects_a_roster_cell_off_the_test_id_rule(certification_copy: Path):
    roster = _load(certification_copy / ROSTER)
    next(
        cell for cell in roster["cells"]
        if (cell["canonical_client"], cell["surface"], cell["operation"]) == QGIS_WFS
    )["test_id"] = "CERT-CONN-01"
    _write(certification_copy / ROSTER, roster)

    result = _run(certification_copy, "validate-protocol-requirements.py")

    assert result.returncode != 0
    assert f"must use test ID {QGIS_WFS_TEST_ID!r}" in result.stderr
