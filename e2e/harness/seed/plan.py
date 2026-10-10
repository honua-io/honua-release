#!/usr/bin/env python3
"""Request construction for the candidate seed.

Publication order is the layer-id contract. The first nine services stay in the
order this harness has always published them, so their ids stay 1 through 9
when the server assigns ids in publication order. ``maui-buildings`` is
appended after that. Docs that still name the public demo's layer 13 bind the
id this publish call actually returns.

``maui-inspections`` is the only editable publication. The candidate admin
contract accepts Create/Update/Delete only with ``storageMode`` ``managed``.
Every other service stays a source-backed read.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

# Tokens the candidate (server 87966c3) accepts on a feature publication.
# Edit tokens require storageMode managed; a source-backed request is HTTP 400.
EDIT_CAPABILITIES = ("Query", "Create", "Update", "Delete")
EDIT_SERVICE = "maui-inspections"

# Fields the pinned honua-sdk-python 0.1.13 docs name, plus ``status`` because
# the same README queries ``status = 'active'``.
BUILDING_FIELDS = (
    "id",
    "name",
    "subtype",
    "class",
    "height",
    "num_floors",
    "render_height",
    "height_source",
    "status",
)

# honua-site assets/demo/layers.json. A drift here is a warning, not a renumber.
PINNED_LAYER_IDS = {
    "maui-parcels": 1,
    "maui-zoning": 2,
    "maui-place-names": 6,
}

STABLE_SERVICES = (
    "maui-parcels",
    "maui-zoning",
    "maui-roads",
    "maui-flood-hazard",
    "maui-sea-level-rise",
    "maui-place-names",
    "maui-inspections",
    "honua-workbench",
    "e2e",
)


class SeedStepError(RuntimeError):
    """A publish or permission call did not succeed. The manifest must not claim it did."""

    def __init__(self, step: str, status: int, body: str) -> None:
        super().__init__(f"{step} failed (HTTP {status}): {body}")
        self.step = step
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Publication:
    table: str
    layer_name: str
    service_name: str
    geometry_type: str
    primary_key: str
    editable: bool = False
    anonymous_write: bool = False


def publications() -> list[Publication]:
    """Demo services first, in the historical order, then the synthetic buildings layer."""
    rows = [
        Publication("maui_parcels", "maui-parcels", "maui-parcels", "Polygon", "gid"),
        Publication("maui_zoning", "maui-zoning", "maui-zoning", "Polygon", "gid"),
        Publication("maui_roads", "maui-roads", "maui-roads", "LineString", "gid"),
        Publication("maui_flood_hazard", "maui-flood-hazard", "maui-flood-hazard", "Polygon", "gid"),
        Publication("maui_sea_level_rise", "maui-sea-level-rise", "maui-sea-level-rise", "Polygon", "gid"),
        Publication("maui_place_names", "maui-place-names", "maui-place-names", "Point", "gid"),
        Publication(
            "maui_inspections",
            "maui-inspections",
            "maui-inspections",
            "Point",
            "id",
            editable=True,
            anonymous_write=True,
        ),
        Publication("workbench_assets", "workbench-assets", "honua-workbench", "Point", "OBJECTID"),
        Publication("e2e_src_fs", "e2e_src_fs", "e2e", "Point", "gid"),
        Publication("maui_buildings", "maui-buildings", "maui-buildings", "Polygon", "id"),
    ]
    services = tuple(row.service_name for row in rows)
    if services[: len(STABLE_SERVICES)] != STABLE_SERVICES:
        raise RuntimeError(f"publication order drifted from the stable nine: {services}")
    editable = [row.service_name for row in rows if row.editable or row.anonymous_write]
    if editable != [EDIT_SERVICE]:
        raise RuntimeError(f"edit capability must stay on {EDIT_SERVICE} only, got {editable}")
    return rows


def publish_body(publication: Publication) -> dict[str, Any]:
    """Admin ``POST /api/v1/admin/connections/{id}/layers`` body."""
    if publication.editable and publication.service_name != EDIT_SERVICE:
        raise ValueError(
            f"refusing edit capabilities on {publication.service_name}; "
            f"only {EDIT_SERVICE} is the scratch service"
        )
    body: dict[str, Any] = {
        "schema": "honua_data",
        "table": publication.table,
        "layerName": publication.layer_name,
        "serviceName": publication.service_name,
        "geometryColumn": "geom",
        "geometryType": publication.geometry_type,
        "primaryKey": publication.primary_key,
        "srid": 4326,
        "enabled": True,
    }
    if publication.editable:
        body["storageMode"] = "managed"
        body["capabilities"] = list(EDIT_CAPABILITIES)
    return body


def publish_bodies() -> list[dict[str, Any]]:
    return [publish_body(publication) for publication in publications()]


def access_plan() -> list[dict[str, Any]]:
    """Anonymous policy per service. Write stays on the inspections scratch service only."""
    return [
        {
            "serviceName": publication.service_name,
            "allowAnonymous": True,
            "allowAnonymousWrite": publication.anonymous_write,
        }
        for publication in publications()
    ]


def require_http(step: str, status: int, body: str) -> None:
    """Fail the seed when a publish or permission update does not return 2xx."""
    code = int(status)
    if code < 200 or code > 299:
        raise SeedStepError(step, code, body)


def _layer_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def render_manifest(connection_id: str, ids: Mapping[str, int]) -> dict[str, Any]:
    """Same manifest shape the drivers already read, plus the appended buildings layer."""
    missing = [service for service in (*(row.service_name for row in publications()),) if service not in ids]
    if missing:
        raise SeedStepError("render-manifest", 0, f"missing layer ids for {missing}")
    for service, layer_id in ids.items():
        if _layer_id(layer_id) is None:
            raise SeedStepError("render-manifest", 0, f"{service} layer id {layer_id!r} is not a positive integer")

    def demo(service: str) -> dict[str, Any]:
        return {"service": service, "layerId": ids[service]}

    return {
        "connectionId": connection_id,
        "service": "maui-zoning",
        "layers": {"e2e_src_fs": ids["e2e"], "maui_zoning": ids["maui-zoning"]},
        "slice1": {"e2e_src_fs": {"service": "e2e", "layerId": ids["e2e"]}},
        "demo": {
            "maui-parcels": demo("maui-parcels"),
            "maui-zoning": demo("maui-zoning"),
            "maui-roads": demo("maui-roads"),
            "maui-flood-hazard": demo("maui-flood-hazard"),
            "maui-sea-level-rise": demo("maui-sea-level-rise"),
            "maui-place-names": demo("maui-place-names"),
            "maui-inspections": demo("maui-inspections"),
            "workbench-assets": {"service": "honua-workbench", "layerId": ids["honua-workbench"]},
            "maui-buildings": demo("maui-buildings"),
        },
    }


def bind_fixture_context(manifest: Mapping[str, Any]) -> dict[str, str]:
    """Substitution values taken from ids the seed recorded, not from the public demo's layer 13."""
    demo = manifest.get("demo") if isinstance(manifest, Mapping) else None
    if not isinstance(demo, Mapping):
        return {}
    bound: dict[str, str] = {}
    zoning = demo.get("maui-zoning")
    if isinstance(zoning, Mapping) and _layer_id(zoning.get("layerId")) is not None:
        bound["fixture.featureService"] = "maui-zoning"
        bound["fixture.featureLayerId"] = str(zoning["layerId"])
    buildings = demo.get("maui-buildings")
    if isinstance(buildings, Mapping) and _layer_id(buildings.get("layerId")) is not None:
        bound["fixture.mauiBuildingsLayerId"] = str(buildings["layerId"])
    inspections = demo.get("maui-inspections")
    if isinstance(inspections, Mapping) and _layer_id(inspections.get("layerId")) is not None:
        bound["fixture.editService"] = EDIT_SERVICE
        bound["fixture.mauiInspectionsLayerId"] = str(inspections["layerId"])
    return bound


def render_height(height: Decimal | None, num_floors: int | None) -> Decimal:
    """The quickstart's documented ``COALESCE(height, num_floors * 3.0, 4.0)``."""
    if height is not None:
        return height
    if num_floors is not None:
        return Decimal(num_floors) * Decimal("3.0")
    return Decimal("4.0")


def buildings_rows() -> list[dict[str, Any]]:
    """Four synthetic footprints. Not a county extract and not the public demo's layer 13."""
    specs = (
        (1, "synthetic-footprint-1", "commercial", "retail", Decimal("18.0"), 4, "active"),
        (2, "synthetic-footprint-2", "residential", "dwelling", Decimal("12.5"), 3, "active"),
        (3, "synthetic-footprint-3", "residential", "dwelling", None, 2, "active"),
        (4, "synthetic-footprint-4", "utility", "shed", Decimal("4.0"), 1, "inactive"),
    )
    rows = []
    for index, (feature_id, name, subtype, kind, height, floors, status) in enumerate(specs):
        lon = Decimal("-156.470") + (Decimal(index) * Decimal("0.004"))
        lat = Decimal("20.889")
        rows.append(
            {
                "id": feature_id,
                "name": name,
                "subtype": subtype,
                "class": kind,
                "height": height,
                "num_floors": floors,
                "render_height": render_height(height, floors),
                "height_source": "synthetic",
                "status": status,
                "min_lon": lon,
                "min_lat": lat,
                "max_lon": lon + Decimal("0.003"),
                "max_lat": lat + Decimal("0.003"),
            }
        )
    validate_buildings(rows)
    return rows


def validate_buildings(rows: list[dict[str, Any]]) -> None:
    if len(rows) < 3:
        raise ValueError("maui-buildings needs at least three features with geometry")
    seen: set[int] = set()
    for row in rows:
        missing = [field for field in (*BUILDING_FIELDS, "min_lon", "min_lat", "max_lon", "max_lat") if field not in row]
        if missing:
            raise ValueError(f"building row {row.get('id')!r} is missing {missing}")
        if row["status"] not in {"active", "inactive"}:
            raise ValueError(f"building row {row['id']} has status {row['status']!r}")
        if row["id"] in seen:
            raise ValueError(f"duplicate building id {row['id']}")
        seen.add(row["id"])
        expected = render_height(row["height"], row["num_floors"])
        if row["render_height"] != expected:
            raise ValueError(
                f"building row {row['id']} render_height {row['render_height']} != documented coalesce {expected}"
            )
        if not (row["min_lon"] < row["max_lon"] and row["min_lat"] < row["max_lat"]):
            raise ValueError(f"building row {row['id']} does not have a polygon extent")
    if not any(row["status"] == "active" for row in rows):
        raise ValueError("no building matches status = 'active'")
    if not any(row["height"] is not None and row["height"] > 10 for row in rows):
        raise ValueError("no building matches height > 10")
    if not any(row["status"] != "active" for row in rows):
        raise ValueError("status = 'active' would match every row")
    if not any(row["height"] is None or row["height"] <= 10 for row in rows):
        raise ValueError("height > 10 would match every row")


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_number(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def buildings_sql(rows: list[dict[str, Any]] | None = None) -> str:
    """SQL the seed applies for the synthetic buildings table. Rows are validated first."""
    materialized = buildings_rows() if rows is None else rows
    validate_buildings(materialized)
    inserts = []
    for row in materialized:
        height = "NULL" if row["height"] is None else _sql_number(row["height"])
        inserts.append(
            "  ("
            f"{int(row['id'])}, {_sql_text(row['name'])}, {_sql_text(row['subtype'])}, "
            f"{_sql_text(row['class'])}, {height}, {int(row['num_floors'])}, "
            f"{_sql_number(row['render_height'])}, {_sql_text(row['height_source'])}, "
            f"{_sql_text(row['status'])}, "
            "ST_SetSRID(ST_MakeEnvelope("
            f"{_sql_number(row['min_lon'])}, {_sql_number(row['min_lat'])}, "
            f"{_sql_number(row['max_lon'])}, {_sql_number(row['max_lat'])}"
            "), 4326))"
        )
    values = ",\n".join(inserts)
    return f"""-- Synthetic maui-buildings fixture for the pinned getting-started field contract.
-- Generated rows, not a county building extract and not the public demo dataset.
CREATE SCHEMA IF NOT EXISTS honua_data;
DROP TABLE IF EXISTS honua_data.maui_buildings;
CREATE TABLE honua_data.maui_buildings (
  id             integer PRIMARY KEY,
  name           text NOT NULL,
  subtype        text,
  class          text,
  height         double precision,
  num_floors     integer,
  render_height  double precision NOT NULL,
  height_source  text NOT NULL,
  status         text NOT NULL,
  geom           geometry(Polygon,4326) NOT NULL
);
INSERT INTO honua_data.maui_buildings
  (id, name, subtype, class, height, num_floors, render_height, height_source, status, geom)
VALUES
{values};
"""


def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Candidate seed request construction")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("publish-bodies")
    sub.add_parser("buildings-sql")
    sub.add_parser("access-plan")
    require = sub.add_parser("require-http")
    require.add_argument("--step", required=True)
    require.add_argument("--status", required=True, type=int)
    rendered = sub.add_parser("render-manifest")
    rendered.add_argument("--connection-id", required=True)
    rendered.add_argument("--ids", required=True, help="JSON object of service name to layer id")
    try:
        args = parser.parse_args(argv)
        if args.command == "publish-bodies":
            _emit(publish_bodies())
        elif args.command == "buildings-sql":
            sys.stdout.write(buildings_sql())
        elif args.command == "access-plan":
            _emit(access_plan())
        elif args.command == "require-http":
            require_http(args.step, args.status, sys.stdin.read())
        elif args.command == "render-manifest":
            ids = json.loads(args.ids)
            if not isinstance(ids, dict):
                raise SeedStepError("render-manifest", 0, "ids must be a JSON object")
            _emit(render_manifest(args.connection_id, {str(key): int(value) for key, value in ids.items()}))
        else:
            parser.error(args.command)
    except (SeedStepError, ValueError, json.JSONDecodeError) as exc:
        print(f"::error:: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
