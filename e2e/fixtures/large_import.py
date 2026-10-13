#!/usr/bin/env python3
"""Deterministic GeoJSON fixtures for cloud scenario S10 large-import (honua-release#548).

The Redis-on fixture is larger than a release cell's background-import threshold, so it is
generated, never committed (GitHub refuses large blobs, and a regenerated file must match the pin
byte for byte). The Redis-off fixture is small and committed next to this module. Both are pinned
here by feature count, byte size and SHA-256; the harness refuses a file whose digest differs.

Every byte is a function of the feature index alone: integer arithmetic for the coordinates, fixed
separators and no floating-point formatting, so two runs on any platform write identical bytes.

    python e2e/fixtures/large_import.py --out /tmp/s10-large-import.geojson   # prints the pin
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class FixturePin:
    name: str
    features: int
    bytes: int
    sha256: str


# The positive (Redis-on) fixture: ~82 MiB, inside the release cells' background window
# (BackgroundJobThresholdBytes 64 MiB < bytes <= max(threshold, MaxMemoryBytes) = 100 MiB).
LARGE = FixturePin(name="s10-large-import.geojson", features=330_000, bytes=85_688_964,
                   sha256="b17954756c76353b1b19452773032d1bd3bdaaac30ab1ace13ff63c9d4c79ca9")
# The negative (Redis-off) fixture: committed, far below any staging cap, so the request reaches
# the background-queue decision instead of being refused for size first.
SMALL = FixturePin(name="s10-redis-off.geojson", features=8, bytes=2_109,
                   sha256="418a8c0e396d3df484f2ea8b62281852594551c93589ca33158e533feb75d3a3")
SMALL_PATH = HERE / SMALL.name

_HEADER = b'{"type":"FeatureCollection","name":"s10_large_import","features":[\n'
_TRAILER = b"\n]}\n"
_NOTE_WIDTH = 120
_CHUNK = 10_000


def _micro(value: int) -> str:
    """Integer micro-degrees as a fixed six-decimal string (no float formatting)."""
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 1_000_000}.{value % 1_000_000:06d}"


def feature(index: int) -> bytes:
    """Feature `index` (0-based): a point on a 600-column grid over Maui-ish longitudes/latitudes."""
    lon = -160_000_000 + (index % 600) * 6_000
    lat = 18_900_000 + (index // 600) * 5_000
    note = f"honua s10 large import fixture row {index:07d} ".ljust(_NOTE_WIDTH, ".")
    return (f'{{"type":"Feature","properties":{{"s10_id":{index + 1},"band":"b{index % 17:02d}",'
            f'"note":"{note}"}},"geometry":{{"type":"Point","coordinates":[{_micro(lon)},{_micro(lat)}]}}}}'
            ).encode("ascii")


def chunks(count: int):
    """The fixture's bytes, in bounded chunks."""
    yield _HEADER
    for start in range(0, count, _CHUNK):
        rows = [feature(index) for index in range(start, min(start + _CHUNK, count))]
        yield (b",\n" if start else b"") + b",\n".join(rows)
    yield _TRAILER


def generate(path: Path, count: int) -> dict:
    """Write `count` features to `path`; return what was written (bytes, sha256, features)."""
    digest, size = hashlib.sha256(), 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as out:
        for chunk in chunks(count):
            out.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return {"bytes": size, "sha256": digest.hexdigest(), "features": count}


def measure(path: Path) -> dict:
    digest, size = hashlib.sha256(), 0
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
    return {"bytes": size, "sha256": digest.hexdigest()}


class FixtureMismatch(ValueError):
    """A fixture whose bytes differ from the pin; never staged."""


def verify(path: Path, pin: FixturePin) -> dict:
    """The fixture's measured size and digest, or FixtureMismatch when either differs from `pin`."""
    seen = measure(path)
    if seen["bytes"] != pin.bytes or seen["sha256"] != pin.sha256:
        raise FixtureMismatch(f"fixture-sha-mismatch: {pin.name} is {seen['bytes']} bytes sha256:"
                              f"{seen['sha256']}, pinned {pin.bytes} bytes sha256:{pin.sha256}")
    return {**seen, "features": pin.features}


def materialize(pin: FixturePin, workdir: Path) -> Path:
    """The pinned fixture as a local file, verified: the large one generated, the small one copied."""
    target = Path(workdir) / pin.name
    if pin is SMALL:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(SMALL_PATH.read_bytes())
    else:
        generate(target, pin.features)
    verify(target, pin)
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--small", action="store_true", help="the Redis-off fixture instead of the large one")
    args = parser.parse_args(argv)
    pin = SMALL if args.small else LARGE
    written = generate(args.out, pin.features)
    print(json.dumps({"name": pin.name, **written}))
    return 0 if (written["bytes"], written["sha256"]) == (pin.bytes, pin.sha256) else 1


if __name__ == "__main__":
    sys.exit(main())
