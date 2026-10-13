#!/usr/bin/env python3
"""Request construction for the candidate seed.

Publication order is the layer-id contract. The first nine services stay in the
order this harness has always published them, so their ids stay 1 through 9
when the server assigns ids in publication order.

``maui-inspections`` is the only editable publication. The candidate admin
contract accepts Create/Update/Delete only with ``storageMode`` ``managed``.
Every other service stays a source-backed read.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Mapping

# Tokens accepted on a feature publication.
# Edit tokens require storageMode managed; a source-backed request is HTTP 400.
EDIT_CAPABILITIES = ("Query", "Create", "Update", "Delete")
EDIT_SERVICE = "maui-inspections"

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
    """Publish the nine existing services in their historical order."""
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
        if publication.service_name != "e2e"
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
    """The existing manifest shape the drivers already read."""
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
        },
    }



def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Candidate seed request construction")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("publish-bodies")
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
        elif args.command == "access-plan":
            _emit(access_plan())
        elif args.command == "require-http":
            require_http(args.step, args.status, sys.stdin.read())
        elif args.command == "render-manifest":
            ids = json.loads(args.ids)
            if not isinstance(ids, dict):
                raise SeedStepError("render-manifest", 0, "ids must be a JSON object")
            _emit(render_manifest(args.connection_id, ids))
        else:
            parser.error(args.command)
    except (SeedStepError, ValueError, json.JSONDecodeError) as exc:
        print(f"::error:: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
