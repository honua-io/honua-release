"""Small mathematical oracles. No server, SDK, GIS or renderer imports."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
import zlib


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
    """Decode bounded, non-interlaced 8-bit RGB/RGBA PNGs using the PNG specification."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 4 * 1024 * 1024:
        raise ProofError("render is not a bounded PNG")
    width, height = size
    if not 1 <= width <= 4096 or not 1 <= height <= 4096:
        raise ProofError("render dimensions exceed the bound")
    offset, compressed, channels, ended = 8, bytearray(), None, False
    while offset < len(png):
        if offset + 12 > len(png):
            raise ProofError("PNG chunk is truncated")
        length, = struct.unpack_from(">I", png, offset)
        tag = png[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(png):
            raise ProofError("PNG chunk is truncated")
        body = png[offset + 8:offset + 8 + length]
        checksum, = struct.unpack_from(">I", png, offset + 8 + length)
        if zlib.crc32(tag + body) != checksum:
            raise ProofError("PNG chunk CRC differs")
        if tag == b"IHDR":
            if offset != 8 or length != 13 or channels is not None:
                raise ProofError("PNG header is invalid")
            w, h, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            if (w, h) != (width, height) or depth != 8 or colour not in {2, 6} or any((compression, filtering, interlace)):
                raise ProofError("PNG dimensions or encoding differ from supported render output")
            channels = 4 if colour == 6 else 3
        elif tag == b"IDAT":
            if channels is None:
                raise ProofError("PNG image precedes its header")
            compressed.extend(body)
        elif tag == b"IEND":
            if body or end != len(png):
                raise ProofError("PNG end or trailing bytes are invalid")
            ended = True
            break
        offset = end
    if channels is None or not compressed or not ended:
        raise ProofError("PNG is incomplete")
    stride = width * channels
    bound = (stride + 1) * height
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(bytes(compressed), bound + 1)
    except zlib.error as exc:
        raise ProofError("PNG compressed stream is invalid") from exc
    if len(raw) != bound or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ProofError("PNG decoded byte count differs")
    previous = bytearray(stride)
    pixel = None
    for y in range(height):
        start = y * (stride + 1)
        kind, row = raw[start], bytearray(raw[start + 1:start + stride + 1])
        if kind > 4:
            raise ProofError("PNG row filter is invalid")
        for i in range(stride):
            left = row[i - channels] if i >= channels else 0
            above = previous[i]
            upper_left = previous[i - channels] if i >= channels else 0
            predicted = left + above - upper_left
            distances = [abs(predicted - v) for v in (left, above, upper_left)]
            paeth = (left, above, upper_left)[distances.index(min(distances))]
            addition = (0, left, above, (left + above) // 2, paeth)[kind]
            row[i] = (row[i] + addition) & 255
        if y == position[1]:
            pixel = tuple(row[position[0] * channels:(position[0] + 1) * channels])
        previous = row
    if pixel is None or len(pixel) != channels:
        raise ProofError("PNG pixel lies outside its bounds")
    return pixel if channels == 4 else pixel + (255,)


def prove_pixel(png, *, bbox, point, size, rgba):
    """Compute the pixel position from the authored bbox, independently of rendering."""
    width, height = size
    px = math.floor((point[0] - bbox[0]) / (bbox[2] - bbox[0]) * width)
    py = math.floor((bbox[3] - point[1]) / (bbox[3] - bbox[1]) * height)
    if not (0 <= px < width and 0 <= py < height):
        raise ProofError("fixture point lies outside the image")
    observed = png_pixel(png, size, (px, py))
    if tuple(observed) != tuple(rgba):
        raise ProofError("render pixel differs from authored style colour")
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
