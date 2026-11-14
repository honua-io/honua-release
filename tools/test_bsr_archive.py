"""The BSR module archive is pinned by its file content and bound to the Geospatial.Grpc nupkg."""
from __future__ import annotations

import io
import zipfile

import pytest

import bsr_archive
import first_publication_preflight as preflight
from test_first_publication_preflight import BSR, GRPC, PROTOS, World, _bsr_entries, _channel, _manifest, _receipt, _sha, _zip


def test_reencoded_bsr_archive_with_the_same_module_files_is_published():
    # buf.build builds the archive on request; the pin is the module content, not the encoding.
    reencoded = _zip(_bsr_entries(), compression=zipfile.ZIP_STORED, date_time=(2026, 11, 14, 0, 0, 0))
    assert _sha(reencoded) != _sha(BSR)
    world = World(bsr_body=reencoded)
    receipt = preflight.build_receipt(
        world,
        _manifest(),
        observed_at="2026-09-26T00:00:00Z",
        retained={"grpc_sha256": _sha(GRPC), "bsr_content_sha256": preflight.archive_content_sha256(BSR)},
    )
    bsr = _channel(receipt, "bsr:buf.build/honua-io/geospatial-grpc")
    assert bsr["disposition"] == "published"
    assert bsr["files"] == [_channel(_receipt(), "bsr:buf.build/honua-io/geospatial-grpc")["files"][0]]
    assert bsr["files"][0]["content_sha256"] == preflight.archive_content_sha256(BSR)
    assert bsr["files"][0]["file_count"] == len(_bsr_entries())
    assert "sha256" not in bsr["files"][0]


def test_archive_content_digest_is_the_sorted_sha256sum_listing():
    listing = "".join(
        f"{_sha(data)}  {name}\n" for name, data in sorted(_bsr_entries().items())
    )
    assert preflight.archive_content_sha256(BSR) == _sha(listing.encode())


def test_bsr_archive_with_changed_module_file_fails_closed():
    changed = _zip(_bsr_entries({**PROTOS, "geospatial/v1/common.proto": b"tampered"}))
    world = World(bsr_body=changed)
    with pytest.raises(preflight.PreflightError, match="content sha256 .* does not match retained"):
        preflight.build_receipt(
            world,
            _manifest(),
            observed_at="2026-09-26T00:00:00Z",
            retained={"grpc_sha256": _sha(GRPC), "bsr_content_sha256": preflight.archive_content_sha256(BSR)},
        )


def test_bsr_protos_must_equal_the_pinned_grpc_nupkg_protos():
    # Even a retained content digest re-recorded from a drifted archive cannot pass: the module
    # protos are bound to the byte-pinned Geospatial.Grpc 1.0.0 nupkg.
    for protos in (
        {**PROTOS, "geospatial/v1/common.proto": b"tampered"},
        {**PROTOS, "geospatial/v1/extra.proto": b"extra"},
        {"geospatial/v1/common.proto": PROTOS["geospatial/v1/common.proto"]},
    ):
        drifted = _zip(_bsr_entries(protos))
        world = World(bsr_body=drifted)
        with pytest.raises(preflight.PreflightError, match="Geospatial.Grpc 1.0.0 nupkg"):
            preflight.build_receipt(
                world,
                _manifest(),
                observed_at="2026-09-26T00:00:00Z",
                retained={"grpc_sha256": _sha(GRPC), "bsr_content_sha256": preflight.archive_content_sha256(drifted)},
            )


@pytest.mark.parametrize("body", [
    b"not-a-zip",
    _zip({"../escape.proto": b"x", **_bsr_entries()}),
    _zip({"/abs.proto": b"x", **_bsr_entries()}),
])
def test_bsr_archive_that_is_not_a_safe_zip_fails_closed(body):
    with pytest.raises(preflight.PreflightError, match="BSR archive"):
        preflight.archive_content_sha256(body)


def _corrupt_deflate() -> bytes:
    body = bytearray(_zip({"geospatial/v1/common.proto": b"x" * 4096}))
    offset = 30 + len("geospatial/v1/common.proto")
    body[offset:offset + 8] = b"\xff" * 8
    return bytes(body)


def _dir_with_data() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(zipfile.ZipInfo("geospatial/"), b"hidden")
        archive.writestr("geospatial/v1/common.proto", b"a")
    return output.getvalue()


@pytest.mark.parametrize("body, reason", [
    (_zip({"x\n" + _sha(b"y") + "  y": b"x", **_bsr_entries()}), "unsafe entry name"),
    (_zip({"geospatial/v1/\rcommon.proto": b"x"}), "unsafe entry name"),
    (_corrupt_deflate(), "not a readable zip"),
    (_dir_with_data(), "directory entry that carries data"),
])
def test_bsr_archive_entries_that_would_hide_content_fail_closed(body, reason):
    with pytest.raises(preflight.PreflightError, match=reason):
        preflight.archive_content_sha256(body)


def test_bsr_archive_declaring_too_many_bytes_fails_before_reading(monkeypatch):
    monkeypatch.setattr(bsr_archive, "ZIP_MAX_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(preflight.PreflightError, match="uncompressed bytes"):
        preflight.archive_content_sha256(_zip({"geospatial/v1/common.proto": b"x" * 11}))


def test_grpc_nupkg_without_protos_cannot_vouch_for_the_bsr_archive():
    grpc = _zip({"Geospatial.Grpc.nuspec": b"<package/>"})
    world = World(grpc_body=grpc)
    with pytest.raises(preflight.PreflightError, match="protos do not equal"):
        preflight.build_receipt(
            world,
            _manifest(),
            observed_at="2026-09-26T00:00:00Z",
            retained={"grpc_sha256": _sha(grpc), "bsr_content_sha256": preflight.archive_content_sha256(BSR)},
        )


def test_bsr_archive_with_duplicate_entries_fails_closed():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("geospatial/v1/common.proto", b"a")
        with pytest.warns(UserWarning):
            archive.writestr("geospatial/v1/common.proto", b"b")
    with pytest.raises(preflight.PreflightError, match="BSR archive"):
        preflight.archive_content_sha256(output.getvalue())


@pytest.mark.parametrize("record", [
    {"content_sha256": "zz" * 32, "file_count": 3},
    {"content_sha256": "ab" * 32},
    {"content_sha256": "ab" * 32, "file_count": 0},
    {"content_sha256": "ab" * 32, "file_count": True},
])
def test_bsr_content_record_without_a_hex_digest_and_file_count_fails_audit(record):
    receipt = _receipt()
    bsr = _channel(receipt, "bsr:buf.build/honua-io/geospatial-grpc")
    bsr["files"] = [{**record, "filename": "bsr.zip", "url": preflight.BSR_ZIP_URL}]
    with pytest.raises(preflight.PreflightError, match="not 64 hex|no file_count"):
        preflight.audit(receipt)


def test_only_the_bsr_channel_may_carry_a_content_digest_instead_of_bytes():
    receipt = _receipt()
    grpc = _channel(receipt, "nuget:Geospatial.Grpc")
    grpc["files"] = [{"content_sha256": "ab" * 32, "file_count": 3, "filename": "g.nupkg", "url": preflight.GRPC_NUPKG_URL}]
    with pytest.raises(preflight.PreflightError, match="digest that is not 64 hex"):
        preflight.audit(receipt)


def test_committed_receipt_records_the_retained_bsr_content_pin():
    committed = preflight.load_receipt(preflight.RECEIPT_PATH)
    bsr = _channel(committed, "bsr:buf.build/honua-io/geospatial-grpc")
    assert bsr["files"] == [{
        "content_sha256": preflight.BSR_CONTENT_SHA256,
        "file_count": preflight.BSR_FILE_COUNT,
        "filename": "geospatial-grpc-bsr-f52df33b3b5d4723881ad0bacaf8a754.zip",
        "url": preflight.BSR_ZIP_URL,
    }]
