"""Unit coverage for generated format fixtures; no Docker daemon or server is required."""
from __future__ import annotations

import importlib.util
import os
import sys
from email.message import Message
from pathlib import Path


MODULE_PATH = Path(__file__).parent / "drivers" / "formats" / "formats.py"


def _load_formats_module():
    spec = importlib.util.spec_from_file_location("format_driver_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_docker_user_args_are_portable(monkeypatch):
    formats = _load_formats_module()
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.delattr(os, "getgid", raising=False)
    assert formats.docker_user_args() == []


def test_filegdb_fixture_is_a_gdb_zip(monkeypatch, tmp_path):
    formats = _load_formats_module()
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "source.geojson").write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    monkeypatch.setattr(formats, "FIX", fixtures)
    monkeypatch.setattr(formats, "WORK", tmp_path / "samples")
    formats.WORK.mkdir()

    class Result:
        returncode = 0
        stderr = b""

    def fake_run(command, **_kwargs):
        assert "OpenFileGDB" in command
        gdb = formats.WORK / "sample.gdb"
        gdb.mkdir()
        (gdb / "a00000001.gdbtable").write_bytes(b"fixture")
        return Result()

    monkeypatch.setattr(formats.subprocess, "run", fake_run)
    archive = formats.gdal_filegdb_zip()
    assert archive is not None

    import zipfile
    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["sample.gdb/a00000001.gdbtable"]


def test_binary_output_requires_expected_content_type(monkeypatch):
    formats = _load_formats_module()
    monkeypatch.setattr(formats, "EXPORT_LAYER", 9)

    class Response:
        headers = Message()
        headers["Content-Type"] = "application/vnd.apache.arrow.stream"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"arrow-payload"

    monkeypatch.setattr(formats.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    status, note = formats.query_binary_output("arrow", "application/vnd.apache.arrow.stream")
    assert status == "pass"
    assert "arrow" in note

    status, note = formats.query_binary_output("arrow", "application/geobuf")
    assert status == "fail"
    assert "content type" in note
