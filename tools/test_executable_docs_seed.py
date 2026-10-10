"""Behavior of the candidate seed helper and the seed.sh request path.

These tests execute plan.py and seed.sh. They do not treat a source-string
match as proof that a publication, permission update, or SQL row happened.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "e2e/harness/seed/seed.sh"
PLAN_PATH = ROOT / "e2e/harness/seed/plan.py"

INSPECTION_NAMES = (
    "Kahului Harbor pier 2",
    "Kanaha Beach Park restroom",
    "Waihee Ridge trailhead",
    "Iao Valley lookout",
    "Maalaea small boat harbor",
    "Kihei baseyard",
    "Paia community center",
    "Hookipa overlook",
)


def load_plan():
    spec = importlib.util.spec_from_file_location("honua_release_seed_plan", PLAN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plan = load_plan()


def run_seed(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    out = tmp_path / "out"
    out.mkdir()
    env = os.environ.copy()
    env.update({
        "SEED_DRY_RUN": "1",
        "E2E_OUT": str(out),
        "SEED_DRY_SQL": str(out / "applied.sql"),
        "SEED_DRY_REQUESTS": str(out / "requests.ndjson"),
    })
    env.update(extra)
    return subprocess.run(
        ["bash", str(SEED)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def request_bodies(tmp_path: Path) -> list[dict]:
    path = tmp_path / "out" / "requests.ndjson"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_seed_script_parses():
    proc = subprocess.run(["bash", "-n", str(SEED)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_publication_order_and_scratch_only_edit_capability():
    services = [row.service_name for row in plan.publications()]
    assert services[:9] == list(plan.STABLE_SERVICES)
    assert services[9:] == ["maui-buildings"]
    bodies = plan.publish_bodies()
    editable = [body for body in bodies if "storageMode" in body or "capabilities" in body]
    assert [body["serviceName"] for body in editable] == ["maui-inspections"]
    assert editable[0]["storageMode"] == "managed"
    assert editable[0]["capabilities"] == ["Query", "Create", "Update", "Delete"]
    assert "Extract" not in editable[0]["capabilities"]
    writes = [row for row in plan.access_plan() if row["allowAnonymousWrite"]]
    assert [row["serviceName"] for row in writes] == ["maui-inspections"]
    buildings = next(body for body in bodies if body["serviceName"] == "maui-buildings")
    assert buildings["geometryType"] == "Polygon"
    assert buildings["primaryKey"] == "id"
    assert "storageMode" not in buildings
    assert "capabilities" not in buildings


def test_edit_capability_is_refused_on_any_other_service():
    zoning = plan.Publication("maui_zoning", "maui-zoning", "maui-zoning", "Polygon", "gid", editable=True)
    with pytest.raises(ValueError, match="refusing edit"):
        plan.publish_body(zoning)


def test_buildings_rows_match_the_documented_filters_and_extents():
    rows = plan.buildings_rows()
    assert len(rows) >= 3
    assert [row["id"] for row in rows] == [1, 2, 3, 4]
    for row in rows:
        assert set(plan.BUILDING_FIELDS) <= set(row)
        assert row["min_lon"] < row["max_lon"]
        assert row["min_lat"] < row["max_lat"]
    active = [row for row in rows if row["status"] == "active"]
    tall = [row for row in rows if row["height"] is not None and row["height"] > 10]
    assert active and len(active) < len(rows)
    assert tall and len(tall) < len(rows)
    by_id = {row["id"]: row for row in rows}
    assert by_id[1]["height"] == Decimal("18.0")
    assert by_id[1]["render_height"] == Decimal("18.0")
    assert by_id[3]["height"] is None
    assert by_id[3]["num_floors"] == 2
    assert by_id[3]["render_height"] == Decimal("6.0")
    assert by_id[4]["status"] == "inactive"
    extents = {(row["min_lon"], row["min_lat"], row["max_lon"], row["max_lat"]) for row in rows}
    assert len(extents) == len(rows)
    assert by_id[1]["min_lon"] == Decimal("-156.470")
    assert by_id[2]["min_lon"] != by_id[1]["min_lon"]


def test_buildings_sql_refuses_a_row_missing_a_documented_field():
    rows = plan.buildings_rows()
    del rows[0]["status"]
    with pytest.raises(ValueError, match="missing"):
        plan.buildings_sql(rows)


def test_require_http_and_manifest_fail_closed():
    plan.require_http("publish maui-parcels", 201, "{}")
    with pytest.raises(plan.SeedStepError, match=r"HTTP 500"):
        plan.require_http("publish maui-parcels", 500, '{"error":"publish refused"}')
    ids = {row.service_name: index + 1 for index, row in enumerate(plan.publications())}
    missing = dict(ids)
    del missing["maui-buildings"]
    with pytest.raises(plan.SeedStepError, match="missing layer ids"):
        plan.render_manifest("dry-run-connection", missing)
    rejected = dict(ids)
    rejected["maui-buildings"] = True
    with pytest.raises(plan.SeedStepError, match="not a positive integer"):
        plan.render_manifest("dry-run-connection", rejected)
    proc = subprocess.run(
        [os.environ.get("PYTHON", "python3"), str(PLAN_PATH), "require-http",
         "--step", "access-policy maui-inspections", "--status", "403"],
        input='{"error":"permission refused"}',
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "HTTP 403" in proc.stderr
    assert "permission refused" in proc.stderr


def test_dry_run_publishes_stable_ids_and_the_synthetic_rows(tmp_path: Path):
    proc = run_seed(tmp_path)
    assert proc.returncode == 0, proc.stderr
    bodies = request_bodies(tmp_path)
    published = plan.publish_bodies()
    access = plan.access_plan()
    assert bodies[0]["name"] == "e2e-pg"
    assert bodies[0]["databaseName"] == "honua"
    assert bodies[1:1 + len(published)] == published
    tail = bodies[1 + len(published):]
    assert len(tail) == len(access) * 2
    for index, row in enumerate(access):
        policy = {"allowAnonymous": True, "allowAnonymousWrite": row["allowAnonymousWrite"]}
        assert tail[index * 2] == policy
        assert tail[index * 2 + 1] == {"accessPolicy": policy}
    assert [row["serviceName"] for row in access if row["allowAnonymousWrite"]] == ["maui-inspections"]

    ids = {row.service_name: index + 1 for index, row in enumerate(plan.publications())}
    manifest = json.loads((tmp_path / "out" / "seed-manifest.json").read_text(encoding="utf-8"))
    assert manifest == plan.render_manifest("dry-run-connection", ids)
    assert [manifest["demo"][name]["layerId"] for name in (
        "maui-parcels", "maui-zoning", "maui-place-names", "maui-inspections")] == [1, 2, 6, 7]
    assert manifest["demo"]["workbench-assets"] == {"service": "honua-workbench", "layerId": 8}
    assert manifest["slice1"]["e2e_src_fs"] == {"service": "e2e", "layerId": 9}
    assert manifest["demo"]["maui-buildings"] == {"service": "maui-buildings", "layerId": 10}
    assert manifest["demo"]["maui-buildings"]["layerId"] != 13
    assert not (tmp_path / "out" / "seed-layer-ids.txt").exists()

    applied = (tmp_path / "out" / "applied.sql").read_text(encoding="utf-8")
    assert plan.buildings_sql().strip() in applied
    assert "generate_series(0, 13) AS i, generate_series(0, 22) AS j;" in applied
    assert "(1 + ((i * 3 + j) % 6))::text" in applied
    assert "tmk_txt" not in applied
    for name in INSPECTION_NAMES:
        assert name in applied
    zones = [str(1 + ((i * 3 + j) % 6)) for i in range(14) for j in range(23)]
    assert len(zones) == 14 * 23
    assert "1" in zones and len(set(zones)) == 6
    for lon in ("-156.47", "-156.466", "-156.462", "-156.458"):
        assert lon in applied


@pytest.mark.parametrize("mode,needle", [("publish", "publish maui-parcels failed (HTTP 500)"),
                                         ("access", "access-policy maui-parcels failed (HTTP 403)")])
def test_failed_publish_or_permission_update_writes_no_manifest(tmp_path: Path, mode: str, needle: str):
    proc = run_seed(tmp_path, SEED_DRY_FAIL=mode)
    assert proc.returncode != 0
    assert needle in proc.stderr
    assert not (tmp_path / "out" / "seed-manifest.json").exists()
    if mode == "publish":
        assert request_bodies(tmp_path)[1]["serviceName"] == "maui-parcels"
        assert "maui-buildings" not in (tmp_path / "out" / "requests.ndjson").read_text(encoding="utf-8")
