"""Strict PNG controls for the shared decoder and the terminal-journey pixel oracle.

These checks run in the tools self-test. They do not execute a candidate.
"""
from __future__ import annotations

import importlib.util
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_render_png():
    path = ROOT / "certification" / "installed-clients" / "render_png.py"
    spec = importlib.util.spec_from_file_location("honua_installed_clients_render_png_test", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"shared PNG decoder is not loadable: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_png = _load_render_png()


def _oracles():
    spec = importlib.util.spec_from_file_location(
        "terminal_journey_oracles_for_png",
        ROOT / "certification" / "terminal-journey" / "oracles.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def _image(raw, *, width=1, height=1, color=6, before=b"", idat_padding=b""):
    compressed = zlib.compress(raw) + idat_padding
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + before
            + _chunk(b"IDAT", compressed)
            + _chunk(b"IEND", b""))


def test_decoder_refusals_are_typed_and_final_idat_padding_is_kept():
    valid = _image(b"\x00\0\0\xff\xff")
    assert render_png.decode_png(valid)[2](0, 0) == (0, 0, 255, 255)
    padded = _image(b"\x00\0\0\xff\xff", idat_padding=b"\x00\x01")
    assert render_png.decode_png(padded)[2](0, 0) == (0, 0, 255, 255)
    misordered = b"\x89PNG\r\n\x1a\n" + _chunk(b"IDAT", zlib.compress(b"\x00\0\0\xff\xff")) + valid[8:]
    duplicate_iend = valid + _chunk(b"IEND", b"")
    unsupported = _image(b"\x00\x00", color=1)
    critical = valid[:33] + _chunk(b"ABCD", b"x") + valid[33:]
    overlarge = b"\x89PNG\r\n\x1a\n" + b"\0" * render_png.MAX_PNG_BYTES
    cases = {
        "signature": b"not a png",
        "chunk-order": misordered,
        "trailing-data": duplicate_iend,
        "format": unsupported,
        "critical-chunk": critical,
        "size": overlarge,
    }
    for code, payload in cases.items():
        try:
            render_png.decode_png(payload)
        except render_png.PngError as exc:
            assert exc.code == code, code
        else:
            raise AssertionError(code)


def test_journey_pixel_oracle_uses_the_shared_decoder_and_media_type():
    oracles = _oracles()
    rows = [bytearray(20 * 4) for _ in range(10)]
    rows[2][40:44] = bytes((239, 32, 32, 255))
    png = _image(b"".join(b"\0" + bytes(row) for row in rows), width=20, height=10, idat_padding=b"\x00")
    kwargs = dict(bbox=[0, 0, 4, 4], point=[2, 3], size=[20, 10], rgba=[239, 32, 32, 255])
    assert oracles.prove_pixel(png, media_type="image/png", **kwargs)["rgba"] == [239, 32, 32, 255]
    try:
        oracles.prove_pixel(png, media_type="image/jpeg", **kwargs)
    except oracles.ProofError as exc:
        assert "media type" in str(exc)
    else:
        raise AssertionError("media type")
    broken = png[:33] + _chunk(b"ABCD", b"x") + png[33:]
    try:
        oracles.prove_pixel(broken, media_type="image/png", **kwargs)
    except oracles.ProofError as exc:
        assert "critical-chunk" in str(exc)
    else:
        raise AssertionError("critical-chunk")


def test_executor_import_does_not_shadow_the_journey_driver():
    script = """
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / "certification" / "terminal-journey"))
import executor
import run
journey = (root / "certification" / "terminal-journey").resolve()
assert Path(executor.__file__).resolve().parent == journey
assert Path(run.__file__).resolve().parent == journey
assert Path(run.pins.__file__).resolve().parent == journey
assert callable(run.build_receipt)
"""
    subprocess.run([sys.executable, "-c", script, str(ROOT)], check=True)
