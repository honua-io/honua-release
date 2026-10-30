"""Write a customer-shaped deployment directory for one trial."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from constants import LAYERS, POSTGIS_IMAGE, PRIOR_IMAGE, PROJECTED_CRS
from corpus_io import CORPUS

ENV_NAMES = (
    "HONUA_IMAGE",
    "HONUA_ADMIN_PASSWORD",
    "HONUA_MASTER_KEY",
    "POSTGRES_PASSWORD",
    "HONUA_PORT",
)
PROCESS_NAMES = ("db", "honua")


def fixture_dir(work: Path, state: dict) -> Path:
    return work / f"{state['town_slug']}-gis-{state['fixture_token']}"


def render_compose(state: dict) -> str:
    slug = state["town_slug"]
    return "\n".join([
        f"name: {slug}-gis",
        "services:",
        "  db:",
        f"    image: {POSTGIS_IMAGE}",
        "    environment:",
        "      POSTGRES_USER: gis",
        "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}",
        "      POSTGRES_DB: gis",
        "    labels:",
        f"      customer.site: {slug}",
        "  honua:",
        "    image: ${HONUA_IMAGE}",
        "    depends_on:",
        "      - db",
        "    environment:",
        "      ASPNETCORE_ENVIRONMENT: Development",
        "      ConnectionStrings__DefaultConnection: Host=db;Port=5432;Database=gis;Username=gis;Password=${POSTGRES_PASSWORD}",
        "      HONUA_ADMIN_PASSWORD: ${HONUA_ADMIN_PASSWORD}",
        "      Security__ConnectionEncryption__MasterKey: ${HONUA_MASTER_KEY}",
        "      Licensing__Mode: Disabled",
        '      HostValidation__AllowedHosts: "*"',
        '      AllowedHosts: "*"',
        "    ports:",
        '      - "${HONUA_PORT:-8080}:8080"',
        "    labels:",
        f"      customer.site: {slug}",
        "",
    ])


def render_env_example(state: dict) -> str:
    return "\n".join([
        f"HONUA_IMAGE={state['image_ref']}",
        "HONUA_ADMIN_PASSWORD=replace-from-vault",
        "HONUA_MASTER_KEY=replace-from-vault",
        "POSTGRES_PASSWORD=replace-from-vault",
        "HONUA_PORT=8080",
        "",
    ])


def render_handbook(state: dict) -> str:
    return "\n".join([
        f"# {state['town_name']} site handbook",
        "",
        "The database name is gis. Secrets come from the operator vault.",
        "The parcels access log is access.log.",
        "A request is slow when it took more than 1 second.",
        f"Station coordinates use {PROJECTED_CRS}.",
        "Legacy permit rows live in tables/legacy-permits.csv.",
        "",
    ])


def write_fixture(work: Path, state: dict) -> Path:
    root = fixture_dir(work, state)
    data = root / "data"
    backup = root / "backup"
    tables = root / "tables"
    projected = root / "projected"
    for path in (data, backup, tables, projected):
        path.mkdir(parents=True, exist_ok=True)
    (root / "deployment-notes.md").write_text(state["notes"], encoding="utf-8")
    (root / "site-handbook.md").write_text(render_handbook(state), encoding="utf-8")
    (root / ".env.example").write_text(render_env_example(state), encoding="utf-8")
    (root / "compose.yaml").write_text(render_compose(state), encoding="utf-8")
    (root / "access.log").write_text(state["access_log"], encoding="utf-8")
    names = {"parcels": "parcels.geojson", "flood_zones": "flood-zones.geojson", "shelters": "shelters.geojson"}
    for layer in LAYERS:
        source = CORPUS / f"town-{state['town_key']}" / f"{layer}.4326.geojson"
        (data / names[layer]).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (projected / "stations.geojson").write_text(
        json.dumps({"crs": PROJECTED_CRS, "features": state["stations"]["features"]}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    header = "permit_id,year,fee,note,active,owner"
    rows = [header]
    for row in state["messy_table"]:
        rows.append(",".join("" if row[key] is None else str(row[key]) for key in (
            "permit_id", "year", "fee", "note", "active", "owner",
        )))
    (tables / "legacy-permits.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (root / "saved-map.json").write_text(json.dumps(state["map"], sort_keys=True) + "\n", encoding="utf-8")
    (backup / "previous-release.txt").write_text(
        "\n".join([
            f"image: {state['prior_image']}",
            f"digest: {state['prior_digest']}",
            f"schema: {state['prior_schema']}",
            "",
        ]),
        encoding="utf-8",
    )
    (root / "release-images.json").write_text(
        json.dumps(
            {
                "current_image": state["image_ref"],
                "newer_image": state["target_image"],
                "prior_image_default": PRIOR_IMAGE,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def compose_config(root: Path) -> str:
    """Validate the compose file when the docker CLI is present. No image pull."""
    env = os.environ.copy()
    env.setdefault("HONUA_IMAGE", PRIOR_IMAGE)
    env.setdefault("HONUA_ADMIN_PASSWORD", "local-only")
    env.setdefault("HONUA_MASTER_KEY", "local-only")
    env.setdefault("POSTGRES_PASSWORD", "local-only")
    completed = subprocess.run(
        ["docker", "compose", "-f", str(root / "compose.yaml"), "config"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "docker compose config failed")
    return completed.stdout
