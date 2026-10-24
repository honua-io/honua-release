"""Pure, bounded PNG and authored-coordinate render assertions shared by release clients.

The caller owns the real client request and candidate publication. This module judges only
the returned media and pixels against fixture coordinates; it cannot manufacture a pass.
"""
from __future__ import annotations

import base64
import binascii
import math
import struct
import zlib
from typing import Any, Callable

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 4 * 1024 * 1024
MAX_DIMENSION = 4096
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PngError(ValueError):
    """A typed, response-independent decoder refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _valid_chunk_type(kind: bytes) -> bool:
    return len(kind) == 4 and all(65 <= c <= 90 or 97 <= c <= 122 for c in kind)


def decode_png(data: bytes) -> tuple[int, int, Callable[[int, int], tuple[int, int, int, int]]]:
    """Decode bounded 8-bit, non-interlaced PNG pixels with full container validation."""
    if not isinstance(data, bytes) or len(data) > MAX_PNG_BYTES:
        raise PngError("size")
    if not data.startswith(PNG_SIGNATURE):
        raise PngError("signature")

    offset, header, palette, transparency = len(PNG_SIGNATURE), None, None, None
    idat: list[bytes] = []
    idat_size = 0
    idat_closed = False
    seen_iend = False
    while offset < len(data):
        if len(data) - offset < 12:
            raise PngError("truncated-chunk")
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            raise PngError("truncated-chunk")
        kind = data[offset + 4:offset + 8]
        chunk = data[offset + 8:end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if not _valid_chunk_type(kind) or kind[2] & 0x20:
            raise PngError("chunk-type")
        if zlib.crc32(kind + chunk) != crc:
            raise PngError("crc")
        offset = end

        if header is None and kind != b"IHDR":
            raise PngError("chunk-order")
        if kind == b"IHDR":
            if header is not None or length != 13:
                raise PngError("header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunk)
            if (not width or not height or width > MAX_DIMENSION or height > MAX_DIMENSION
                    or width * height > MAX_PIXELS):
                raise PngError("dimensions")
            if depth != 8 or color not in CHANNELS or compression or filtering or interlace:
                raise PngError("format")
            header = width, height, color
        elif kind == b"PLTE":
            if (palette is not None or transparency is not None or idat or header[2] in (0, 4)
                    or not length or length % 3 or length > 768):
                raise PngError("palette")
            palette = chunk
        elif kind == b"tRNS":
            color = header[2]
            if transparency is not None or idat or color in (4, 6):
                raise PngError("transparency")
            if (color == 3 and (palette is None or not 0 < length <= len(palette) // 3)
                    or color == 0 and length != 2 or color == 2 and length != 6):
                raise PngError("transparency")
            transparency = chunk
        elif kind == b"IDAT":
            if idat_closed or header[2] == 3 and palette is None:
                raise PngError("chunk-order")
            idat_size += length
            if idat_size > MAX_PNG_BYTES:
                raise PngError("size")
            idat.append(chunk)
        elif kind == b"IEND":
            if length or not idat:
                raise PngError("iend")
            if offset != len(data):
                raise PngError("trailing-data")
            seen_iend = True
            break
        elif kind[0] & 0x20 == 0:
            raise PngError("critical-chunk")
        if idat and kind != b"IDAT":
            idat_closed = True
    if not seen_iend:
        raise PngError("missing-iend")

    width, height, color = header
    channels = CHANNELS[color]
    stride = width * channels
    expected = height * (stride + 1)
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(b"".join(idat), expected + 1)
        if len(raw) > expected or inflater.unconsumed_tail:
            raise PngError("inflate-limit")
        raw += inflater.flush(expected + 1 - len(raw))
    except zlib.error as exc:
        raise PngError("zlib") from exc
    if len(raw) > expected:
        raise PngError("inflate-limit")
    # PNG Third Edition permits unused bytes inside the final IDAT chunk only.
    # The enclosing PNG framing is still checked through IEND and rejects bytes after it.
    if len(inflater.unused_data) > len(idat[-1]):
        raise PngError("idat-padding-order")
    if len(raw) != expected or not inflater.eof:
        raise PngError("inflate-length")

    rows: list[bytes] = []
    previous = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        filter_type = raw[start]
        if filter_type > 4:
            raise PngError("filter")
        line = bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                estimate = left + up - corner
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - corner))
                predictor = (left, up, corner)[distances.index(min(distances))]
            else:
                predictor = 0
            line[i] = (line[i] + predictor) & 0xFF
        if color == 3 and any(index >= len(palette) // 3 for index in line):
            raise PngError("palette-index")
        rows.append(bytes(line))
        previous = line

    def pixel(x: int, y: int) -> tuple[int, int, int, int]:
        if not (0 <= x < width and 0 <= y < height):
            raise PngError("pixel-coordinate")
        row, start = rows[y], x * channels
        if color == 6:
            return tuple(row[start:start + 4])  # type: ignore[return-value]
        if color == 4:
            gray, alpha = row[start:start + 2]
            return gray, gray, gray, alpha
        if color == 3:
            index = row[start]
            alpha = transparency[index] if transparency is not None and index < len(transparency) else 255
            return *palette[index * 3:index * 3 + 3], alpha
        if color == 2:
            rgb = tuple(row[start:start + 3])
            transparent = transparency is not None and rgb == tuple(
                sample & 0xFF for sample in struct.unpack(">HHH", transparency))
            return *rgb, 0 if transparent else 255
        gray = row[start]
        transparent = transparency is not None and gray == (struct.unpack(">H", transparency)[0] & 0xFF)
        return gray, gray, gray, 0 if transparent else 255

    return width, height, pixel


def render_expectations(fixture: dict[str, Any]) -> list[tuple[int, int, bool]]:
    """Expected alpha occupancy from authored geographic envelopes and pixel coordinates."""
    spec = fixture["mcp"]["render"]
    width, height = spec["width"], spec["height"]
    minx, miny, maxx, maxy = spec["bbox"]
    margin = spec["edgeMarginPixels"]
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
            or maxx <= minx or maxy <= miny or not all(math.isfinite(v) for v in (minx, miny, maxx, maxy))
            or margin < 0):
        raise ValueError("invalid render fixture")
    sx, sy = (maxx - minx) / width, (maxy - miny) / height
    boxes = [((e[0] - minx) / sx, (maxy - e[3]) / sy, (e[2] - minx) / sx, (maxy - e[1]) / sy)
             for e in (feature["envelope"] for feature in fixture["area"]["features"])]
    result = []
    for x, y in spec["samples"]:
        if type(x) is not int or type(y) is not int or not 0 <= x < width or not 0 <= y < height:
            raise ValueError("invalid render sample")
        cx, cy = x + 0.5, y + 0.5
        inside = any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in boxes)
        near = any(x0 - margin <= cx <= x1 + margin and y0 - margin <= cy <= y1 + margin
                   and not (x0 + margin <= cx <= x1 - margin and y0 + margin <= cy <= y1 - margin)
                   for x0, y0, x1, y1 in boxes)
        if not near:
            result.append((x, y, inside))
    return result


def oracle_map_render(observed: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, str]:
    """Require a real PNG and independent painted/transparent coordinate samples."""
    if observed.get("mimeType") != "image/png":
        return False, "invalid-media-type: expected image/png"
    encoded = observed.get("png")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_PNG_BYTES + 2) // 3):
        return False, "invalid-png:base64"
    try:
        width, height, pixel = decode_png(base64.b64decode(encoded, validate=True))
    except (binascii.Error, ValueError) as exc:
        code = exc.code if isinstance(exc, PngError) else "base64"
        return False, f"invalid-png:{code}"
    try:
        spec = fixture["mcp"]["render"]
        samples = render_expectations(fixture)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False, "invalid-render-fixture"
    if (width, height) != (spec["width"], spec["height"]):
        return False, f"wrong-dimensions: {width}x{height}, expected {spec['width']}x{spec['height']}"
    painted = sum(inside for _, _, inside in samples)
    if not samples or not painted or painted == len(samples):
        return False, "invalid-render-fixture: need painted and transparent interior samples"
    mismatches = [(x, y, pixel(x, y)[3], inside) for x, y, inside in samples
                  if (pixel(x, y)[3] > 0) != inside]
    if mismatches:
        return False, f"alpha-mismatch: {len(mismatches)} of {len(samples)} authored samples"
    return True, f"{painted} painted and {len(samples) - painted} transparent authored samples match"
