"""Small mathematical oracles.

PNG bytes go through the shared release decoder. That module uses the standard
library only. This module does not import a server, SDK, GIS stack, or renderer.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import math
import struct
from pathlib import Path

def _load_render_png():
    """Load the shared decoder by path so this package never shadows `run` or `pins`."""
    path = Path(__file__).resolve().parents[1] / "installed-clients" / "render_png.py"
    spec = importlib.util.spec_from_file_location("honua_installed_clients_render_png", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"shared PNG decoder is not loadable: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_render_png = _load_render_png()
PngError = _render_png.PngError
decode_png = _render_png.decode_png


class ProofError(ValueError):
    pass


def content_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def point_wkb(x, y):
    return base64.b64encode(struct.pack("<BIdd", 1, 1, x, y)).decode()


def centroid(ring):
    """Shoelace centroid, independently of the production geometry library."""
    if len(ring) < 4 or ring[0] != ring[-1]:
        raise ProofError("polygon ring is not closed")
    area = sx = sy = 0.0
    for (x, y), (u, v) in zip(ring, ring[1:]):
        cross = x * v - u * y
        area += cross
        sx += (x + u) * cross
        sy += (y + v) * cross
    if not math.isfinite(area) or abs(area) < 1e-12:
        raise ProofError("polygon has no finite area")
    return sx / (3 * area), sy / (3 * area)


def prove_buffer(feature, *, x, y, distance, segments=32):
    """An NTS point buffer is a regular 32-gon; check every ordinate and centroid.

    Ring start and winding are deliberately irrelevant. Expected coordinates are
    generated with sin/cos, never by calling Buffer, PostGIS, Shapely or an SDK.
    """
    try:
        geometry = feature["geometry"]
        ring, = geometry["coordinates"]
        if geometry["type"] != "Polygon" or len(ring) != segments + 1:
            raise ProofError("buffer polygon vertex count differs")
        expected = [(x + distance * math.cos(2 * math.pi * i / segments),
                     y + distance * math.sin(2 * math.pi * i / segments)) for i in range(segments)]
        unmatched = expected.copy()
        for vertex in ring[:-1]:
            if len(vertex) != 2 or any(not math.isfinite(v) for v in vertex):
                raise ProofError("buffer contains invalid ordinates")
            match = next((p for p in unmatched if math.dist(vertex, p) <= 1e-8), None)
            if match is None:
                raise ProofError("buffer ordinate differs from independent trigonometric oracle")
            unmatched.remove(match)
        if math.dist(centroid(ring), (x, y)) > 1e-8:
            raise ProofError("buffer centroid differs from input point")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ProofError):
            raise
        raise ProofError("invalid buffer GeoJSON") from exc
    return {"centroid": [x, y], "vertexCount": len(expected), "ordinatesVerified": True}


def _journey_attribute(properties):
    """Read the fixture key from a published feature.

    A file-upload import stores source attributes in one JSONB `properties` column
    (honua.create_import_table: id, geometry, properties), and publishing projects source
    columns verbatim, so the fixture key may be nested one level. Exactly one location
    must hold it; the layout is reported, never guessed.
    """
    nested = properties.get("properties")
    top = "journey_id" in properties
    inner = isinstance(nested, dict) and "journey_id" in nested
    if top == inner:
        raise ProofError("published feature does not carry exactly one journey_id")
    return (properties["journey_id"], "columns") if top else (nested["journey_id"], "import-jsonb-properties")


def prove_features(document, expected):
    """Compare an unordered GeoJSON feature collection to authored fixture rows."""
    try:
        features = document["features"]
        if document["type"] != "FeatureCollection" or len(features) != len(expected):
            raise ProofError("imported feature count differs from fixture")
        rows = [(_journey_attribute(f["properties"]), f["geometry"]["type"], f["geometry"]["coordinates"])
                for f in features]
        layouts = {layout for (_, layout), _, _ in rows}
        if len(layouts) != 1:
            raise ProofError("published features mix attribute layouts")
        observed = sorted((value, kind, coordinates) for (value, _), kind, coordinates in rows)
        wanted = sorted((f["properties"]["journey_id"], f["geometry"]["type"],
                         f["geometry"]["coordinates"]) for f in expected)
        if observed != wanted:
            raise ProofError("imported feature IDs or ordinates differ from fixture")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ProofError("invalid imported feature collection") from exc
    return {"featureCount": len(expected), "contentDigest": content_digest(wanted), "attributeLayout": layouts.pop()}


def png_pixel(png, size, position):
    """Read one pixel with the shared PNG decoder, then check the authored size."""
    if not isinstance(png, (bytes, bytearray)):
        raise ProofError("PNG decoder refused: size")
    try:
        width, height, pixel = decode_png(bytes(png))
    except PngError as exc:
        raise ProofError(f"PNG decoder refused: {exc.code}") from exc
    if (width, height) != tuple(size):
        raise ProofError("PNG dimensions differ from the authored render size")
    try:
        return pixel(position[0], position[1])
    except PngError as exc:
        raise ProofError(f"PNG decoder refused: {exc.code}") from exc


def prove_pixel(png, *, bbox, point, size, rgba, media_type):
    """Compute the pixel position from the authored bbox, independently of rendering."""
    if media_type != "image/png":
        raise ProofError("render media type is not image/png")
    width, height = size
    px = math.floor((point[0] - bbox[0]) / (bbox[2] - bbox[0]) * width)
    py = math.floor((bbox[3] - point[1]) / (bbox[3] - bbox[1]) * height)
    if not (0 <= px < width and 0 <= py < height):
        raise ProofError("fixture point lies outside the image")
    observed = png_pixel(png, size, (px, py))
    if tuple(observed) != tuple(rgba):
        raise ProofError(f"render pixel at ({px}, {py}) is rgba{tuple(observed)}, not the authored "
                         f"style colour rgba{tuple(rgba)}")
    return {"x": px, "y": py, "rgba": list(rgba), "sha256": hashlib.sha256(png).hexdigest()}


def prove_map(document, expected_body, *, item_id, version_id, content_hash):
    """URL/replica content must match the authored map, not just a server hash."""
    resource = document.get("data", document)
    try:
        if (resource["itemId"] != item_id or resource["versionId"] != version_id
                or resource["contentHash"] != content_hash
                or resource["envelope"]["family"].lower() != "map"
                or resource["envelope"]["body"] != expected_body):
            raise ProofError("map identity or actual map body differs")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ProofError("map response omits its content or canonical identity") from exc
    return {"contentDigest": content_digest(expected_body), "itemId": item_id, "versionId": version_id}
