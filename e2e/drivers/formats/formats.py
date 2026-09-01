#!/usr/bin/env python3
"""S8 — per-format import/export/roundtrip coverage. Feeds the owner's format cut.

For EACH data format, independently:
  read      : POST a sample to /api/v1/admin/import/upload -> success AND featureCount > 0
  write     : export a seeded layer to the format (only where the server has a writer)
  roundtrip : re-import the exported file -> featureCount preserved

Emits one formatCoverage[] row per format: {format, read, write, roundtrip, notes}.
Honesty (AGENTS.md): a read-only format reports write="na" / roundtrip="na" — NOT fail. A format
whose sample cannot be generated (missing GDAL driver) reports "blocked" with the reason — NOT fail.
Raster/tile formats that are not a FeatureServer-import path report "na" with a note.

Statuses: pass | fail | blocked | na
"""
from __future__ import annotations
import json, os, subprocess, sys, urllib.request, urllib.error, zipfile
from pathlib import Path

BASE = os.environ.get("E2E_BASE", "http://localhost:8080").rstrip("/")
KEY = os.environ.get("E2E_API_KEY", "honua-console-dev-key")
OUT = Path(os.environ.get("E2E_OUT", "out"))
FIX = Path(__file__).resolve().parents[2] / "harness" / "seed" / "fixtures" / "formats"
# Alpine-small deliberately omits Arrow/Parquet. Pin a full multi-architecture image index so a
# mutable `*-latest` update cannot silently change the format-certification surface.
GDAL_IMAGE = os.environ.get(
    "E2E_GDAL_IMAGE",
    "ghcr.io/osgeo/gdal@sha256:0569eb8e7cfb757b1866b4d4a698087d237f57087af0434bfc0266d488e55271",
)
WORK = OUT / "format-samples"
WORK.mkdir(parents=True, exist_ok=True)

# The seeded export source (service/layer published by seed.sh) for write/roundtrip checks.
EXPORT_SVC = os.environ.get("E2E_EXPORT_SERVICE", "e2e")
try:
    _sm = json.loads((OUT / "seed-manifest.json").read_text())
    EXPORT_SVC = _sm.get("service", EXPORT_SVC)
    EXPORT_LAYER = _sm.get("layers", {}).get("maui_zoning")
except Exception:
    EXPORT_LAYER = None


def emit(fmt, read, write, roundtrip, notes=""):
    row = {"format": fmt, "read": read, "write": write, "roundtrip": roundtrip, "notes": notes}
    with (OUT / "formats.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"  [fmt] {fmt:<12} read={read:<7} write={write:<4} roundtrip={roundtrip:<4} {notes}", file=sys.stderr)


def emit_scenario(sid, status, why, evidence=None):
    with (OUT / "scenarios.jsonl").open("a") as f:
        f.write(json.dumps({"scenario": sid, "status": status, "why": why, "evidence": evidence}) + "\n")
    print(f"  [{status.upper():7}] {sid}: {why}", file=sys.stderr)


def _multipart(fields, files):
    boundary = "----honuae2e7f3b"
    body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for name, (fname, data, ctype) in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                 f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def import_file(path: Path, table: str, extra: dict | None = None, ctype="application/octet-stream"):
    """POST a file to the import endpoint. Returns (success, featureCount, detectedFormat, note)."""
    fields = {"TableName": table, "TargetSrid": "4326"}
    fields.update(extra or {})
    body, content_type = _multipart(fields, {"file": (path.name, path.read_bytes(), ctype)})
    req = urllib.request.Request(f"{BASE}/api/v1/admin/import/upload", data=body, method="POST",
                                 headers={"Content-Type": content_type, "X-API-Key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
        except Exception:
            return False, 0, None, f"HTTP {e.code}"
    except Exception as e:
        return False, 0, None, str(e)[:120]
    errs = d.get("validationErrors") or d.get("detail") or ""
    return bool(d.get("success")), d.get("featureCount") or 0, d.get("format"), (str(errs)[:160] if errs else "")


def export_layer(fmt: str) -> bytes | None:
    if EXPORT_LAYER is None:
        return None
    url = f"{BASE}/api/v1/admin/services/{EXPORT_SVC}/FeatureServer"  # not used; export is admin route
    url = f"{BASE}/api/v1/admin/services/{EXPORT_SVC}/layers/{EXPORT_LAYER}/export?format={fmt}"
    req = urllib.request.Request(url, headers={"X-API-Key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read() if r.status == 200 else None
    except Exception:
        return None


def query_binary_output(fmt: str, expected_content_type: str) -> tuple[str, str]:
    """Exercise a FeatureServer binary output format against the seeded layer."""
    if EXPORT_LAYER is None:
        return "blocked", "seed manifest does not identify the output layer"
    query = (f"{BASE}/rest/services/{EXPORT_SVC}/FeatureServer/{EXPORT_LAYER}/query"
             f"?where=1%3D1&outFields=*&returnGeometry=true&f={fmt}")
    req = urllib.request.Request(query, headers={"X-API-Key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = response.read()
            content_type = response.headers.get_content_type()
    except urllib.error.HTTPError as e:
        return "fail", f"FeatureServer f={fmt} returned HTTP {e.code}"
    except Exception as e:
        return "fail", f"FeatureServer f={fmt} request failed: {str(e)[:120]}"
    if not payload:
        return "fail", f"FeatureServer f={fmt} returned an empty payload ({content_type})"
    if content_type != expected_content_type:
        return "fail", (f"FeatureServer f={fmt} content type {content_type!r}, "
                        f"expected {expected_content_type!r}; {len(payload)}B")
    return "pass", f"FeatureServer f={fmt} returned {len(payload)}B ({content_type})"


def docker_user_args() -> list[str]:
    """Preserve host-owned generated files on POSIX without breaking Docker Desktop on Windows."""
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return ["--user", f"{os.getuid()}:{os.getgid()}"]
    return []


def gdal_convert(driver: str, out_name: str, extra_args=None) -> Path | None:
    """Convert source.geojson to `out_name` via GDAL-in-docker. Returns path or None if unavailable."""
    src = FIX / "source.geojson"
    (WORK / "source.geojson").write_bytes(src.read_bytes())
    args = ["ogr2ogr", "-f", driver, f"/d/{out_name}", "/d/source.geojson"] + (extra_args or [])
    try:
        p = subprocess.run(["docker", "run", "--rm", *docker_user_args(), "-v", f"{WORK}:/d", "-w", "/d", GDAL_IMAGE] + args,
                           capture_output=True, timeout=180)
    except Exception as e:
        print(f"    gdal unavailable: {e}", file=sys.stderr)
        return None
    out = WORK / out_name
    if p.returncode != 0:
        detail = p.stderr.decode("utf-8", "replace").strip()[-200:]
        print(f"    {driver} sample generation failed: {detail}", file=sys.stderr)
        return None
    return out if out.exists() and out.stat().st_size > 0 else None


def gdal_filegdb_zip() -> Path | None:
    """Generate Honua's documented `.gdb.zip` upload artifact with OpenFileGDB."""
    src = FIX / "source.geojson"
    (WORK / "source.geojson").write_bytes(src.read_bytes())
    gdb = WORK / "sample.gdb"
    zip_path = WORK / "sample.gdb.zip"
    try:
        p = subprocess.run(
            ["docker", "run", "--rm", *docker_user_args(),
             "-v", f"{WORK}:/d", "-w", "/d", GDAL_IMAGE,
             "ogr2ogr", "-f", "OpenFileGDB", "/d/sample.gdb", "/d/source.geojson"],
            capture_output=True,
            timeout=180,
        )
    except Exception as e:
        print(f"    FileGDB sample generation unavailable: {e}", file=sys.stderr)
        return None
    if p.returncode != 0 or not gdb.is_dir():
        detail = p.stderr.decode("utf-8", "replace").strip()[-200:]
        print(f"    OpenFileGDB sample generation failed: {detail}", file=sys.stderr)
        return None
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for part in gdb.rglob("*"):
            if part.is_file():
                archive.write(part, part.relative_to(WORK))
    return zip_path if zip_path.stat().st_size > 0 else None


def gdal_shapefile_zip() -> Path | None:
    """Shapefile is multi-file; produce a zip the importer accepts."""
    src = FIX / "source.geojson"
    (WORK / "source.geojson").write_bytes(src.read_bytes())
    script = ("ogr2ogr -f 'ESRI Shapefile' /d/shp /d/source.geojson && "
              "cd /d/shp && (command -v zip >/dev/null && zip -q ../sample_shp.zip * || true)")
    try:
        subprocess.run(["docker", "run", "--rm", *docker_user_args(),
                        "-v", f"{WORK}:/d", "-w", "/d", GDAL_IMAGE, "sh", "-c", script],
                       capture_output=True, timeout=180)
    except Exception:
        return None
    z = WORK / "sample_shp.zip"
    if z.exists() and z.stat().st_size > 0:
        return z
    # zip missing in the small image — build the zip on the host from the produced .shp parts.
    shpdir = WORK / "shp"
    if shpdir.is_dir() and any(shpdir.iterdir()):
        import zipfile
        with zipfile.ZipFile(z, "w") as zf:
            for p in shpdir.iterdir():
                zf.write(p, p.name)
        return z
    return None


def do_read(fmt, path, extra=None, ctype="application/octet-stream", expect=2):
    if path is None:
        return "blocked", 0, "sample unavailable (GDAL driver missing)"
    ok, cnt, det, note = import_file(path, f"f_{fmt.lower()}", extra, ctype)
    if ok and cnt >= expect:
        return "pass", cnt, f"detected={det} featureCount={cnt}"
    if ok and cnt == 0:
        return "fail", 0, f"detected={det} but 0 features imported"
    return "fail", cnt, f"detected={det} {note}"


def do_writer(fmt_key, gdal_reimport_driver=None):
    """write + roundtrip for a server-supported export format."""
    data = export_layer(fmt_key)
    if data is None:
        return "fail", "na", "export returned non-200/empty"
    ext = {"csv": "csv", "shapefile": "zip", "gpkg": "gpkg"}[fmt_key]
    exp = WORK / f"export.{ext}"
    exp.write_bytes(data)
    ok, cnt, det, note = import_file(exp, f"rt_{fmt_key}",
                                     {"SourceSrid": "4326"} if fmt_key != "gpkg" else None,
                                     "application/octet-stream")
    if ok and cnt >= 3:
        return "pass", "pass", f"export {len(data)}B; roundtrip featureCount={cnt}"
    return "pass", "fail", f"export {len(data)}B ok; re-import yielded {cnt} ({note})"


def main():
    # server reachability
    try:
        with urllib.request.urlopen(f"{BASE}/healthz/ready", timeout=5) as r:
            ready = r.status == 200
    except Exception:
        ready = False
    if not ready:
        emit_scenario("S8-format-coverage", "blocked", f"server not ready at {BASE}")
        return

    passed = 0
    # --- feature formats with committed / generated samples --------------------------------------
    # GeoJSON (committed)
    r, _, n = do_read("GeoJSON", FIX / "source.geojson", ctype="application/geo+json")
    emit("GeoJSON", r, "na", "na", n); passed += r == "pass"

    # CSV (committed, WKT geometry column) — also a writer
    r, _, n = do_read("CSV", FIX / "sample.csv", ctype="text/csv")
    w, rt, wn = do_writer("csv") if r == "pass" else ("na", "na", "read failed")
    emit("CSV", r, w, rt, f"{n}; {wn}"); passed += r == "pass"

    # WKT (committed; requires SourceSrid)
    r, _, n = do_read("WKT", FIX / "sample.wkt", {"SourceSrid": "4326"}, "text/plain")
    emit("WKT", r, "na", "na", n + " (requires SourceSrid)"); passed += r == "pass"

    # WKB — server import does not auto-detect standalone WKB (binary or hex): surfaced gap.
    import struct
    wkb = WORK / "sample.wkb"
    def _pt(x, y):  # little-endian WKB point
        return struct.pack("<BIdd", 1, 1, x, y)
    wkb.write_bytes(_pt(-156.30, 20.80) + _pt(-156.40, 20.90))
    r, _, n = do_read("WKB", wkb, {"SourceSrid": "4326"}, "application/octet-stream", expect=1)
    emit("WKB", r, "na", "na", n + " (not auto-detected on import)")
    passed += r == "pass"

    # GeoPackage (GDAL) — also a writer
    r, _, n = do_read("GeoPackage", gdal_convert("GPKG", "sample.gpkg"), ctype="application/geopackage+sqlite3")
    w, rt, wn = do_writer("gpkg") if r == "pass" else ("na", "na", "read failed")
    emit("GeoPackage", r, w, rt, f"{n}; {wn}"); passed += r == "pass"

    # Shapefile (GDAL zip) — also a writer
    r, _, n = do_read("Shapefile", gdal_shapefile_zip(), ctype="application/zip")
    w, rt, wn = do_writer("shapefile") if r == "pass" else ("na", "na", "read failed")
    emit("Shapefile", r, w, rt, f"{n}; {wn}"); passed += r == "pass"

    # Esri JSON — committed FeatureSet; server import does not auto-detect it (parsed as GeoJSON): gap.
    r, _, n = do_read("EsriJSON", FIX / "sample.esrijson", ctype="application/json")
    emit("EsriJSON", r, "na", "na", n + " (not auto-detected; parsed as GeoJSON)")
    passed += r == "pass"

    # FlatGeobuf (GDAL)
    r, _, n = do_read("FlatGeobuf", gdal_convert("FlatGeobuf", "sample.fgb"))
    emit("FlatGeobuf", r, "na", "na", n); passed += r == "pass"

    # GPX (GDAL) — format detected but 0 features imported on this build: surfaced gap.
    r, _, n = do_read("GPX", gdal_convert("GPX", "sample.gpx", ["-dsco", "GPX_USE_EXTENSIONS=YES"]),
                      ctype="application/gpx+xml")
    emit("GPX", r, "na", "na", n); passed += r == "pass"

    # KML (GDAL)
    r, _, n = do_read("KML", gdal_convert("KML", "sample.kml"), ctype="application/vnd.google-earth.kml+xml")
    emit("KML", r, "na", "na", n); passed += r == "pass"

    # GeoParquet / GeoArrow / GeoBuf — sample generation needs GDAL drivers absent from the small image.
    r, _, n = do_read(
        "GeoParquet",
        gdal_convert("Parquet", "sample.geoparquet", ["-lco", "GEOMETRY_ENCODING=WKB"]),
    )
    emit("GeoParquet", r, "na", "na", n + " (WKB GeoParquet upload)")
    passed += r == "pass"

    # FileGDB — read-capable (OpenFileGDB) but sample generation is not automated here.
    r, _, n = do_read("FileGDB", gdal_filegdb_zip())
    emit("FileGDB", r, "na", "na", n + " (.gdb.zip upload)")
    passed += r == "pass"
    arrow, note = query_binary_output("arrow", "application/vnd.apache.arrow.stream")
    emit("GeoArrow", "na", arrow, "na", note)
    geobuf, note = query_binary_output("geobuf", "application/geobuf")
    emit("GeoBuf", "na", geobuf, "na", note)

    # Tile / raster formats are not a FeatureServer feature-import path.
    emit("MVT", "na", "na", "na", "vector tile OUTPUT format, not a feature-import path")
    emit("COG", "na", "na", "na", "raster/coverage ingest, not a FeatureServer import")
    emit("PNG/JPEG/TIFF", "na", "na", "na", "raster imagery, not a feature-import path")

    # The S8 scenario gates on a CORE regression only; exotic-format gaps are surfaced in
    # formatCoverage[] for the owner's cut without blocking every platform PR.
    core_rows = [json.loads(l) for l in (OUT / "formats.jsonl").read_text().splitlines()]
    core_fmts = {"GeoJSON", "CSV", "GeoPackage", "Shapefile"}
    core_fail = [r["format"] for r in core_rows if r["format"] in core_fmts and r["read"] != "pass"]
    if core_fail:
        emit_scenario("S8-format-coverage", "fail",
                      f"CORE format read regression: {core_fail}", {"readPass": passed})
    else:
        emit_scenario("S8-format-coverage", "pass",
                      f"{passed} feature formats read (core intact); per-format rows in formatCoverage[]",
                      {"readPass": passed})


if __name__ == "__main__":
    main()
