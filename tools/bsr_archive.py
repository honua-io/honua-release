"""Content pin for the buf.build (BSR) module archive.

buf.build builds the archive on request, so its zip bytes are not a stable pin. The pin is the
archive's file content: the sha256 of the `sha256sum`-style listing ("<hex>  <name>\\n") of its
regular files sorted by name. Every .proto must also equal the copy in the byte-pinned
Geospatial.Grpc nupkg, so the module cannot drift from the published package.
"""
from __future__ import annotations

import io
import lzma
import zipfile
import zlib

from digests import sha256_hex

# Declared uncompressed bytes read from an archive before it is rejected (the module is ~200 KB).
ZIP_MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
_FIRST_PRINTABLE = 0x20
_DELETE = 0x7F
_READ_ERRORS = (
    zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, lzma.LZMAError,
    OSError, EOFError, RuntimeError, ValueError, NotImplementedError,
)


class ArchiveError(ValueError):
    """The archive is not a plain, safe zip, or its content is not the pinned content."""


def _unsafe_name(name: str) -> bool:
    parts = name.split("/")
    if name.startswith("/") or "\\" in name or ".." in parts or "" in parts:
        return True
    # A control character (newline above all) would make the listing ambiguous.
    return any(ord(character) < _FIRST_PRINTABLE or ord(character) == _DELETE for character in name)


def _checked_entries(archive: zipfile.ZipFile, label: str) -> list[zipfile.ZipInfo]:
    if any(info.is_dir() and info.file_size for info in archive.infolist()):
        raise ArchiveError(f"{label} has a directory entry that carries data")
    infos = [info for info in archive.infolist() if not info.is_dir()]
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ArchiveError(f"{label} has duplicate entries")
    for name in names:
        if _unsafe_name(name):
            raise ArchiveError(f"{label} has an unsafe entry name {name!r}")
    if sum(info.file_size for info in infos) > ZIP_MAX_UNCOMPRESSED_BYTES:
        raise ArchiveError(f"{label} declares more than {ZIP_MAX_UNCOMPRESSED_BYTES} uncompressed bytes")
    return infos


def zip_files(body: bytes, label: str) -> dict[str, bytes]:
    """Regular-file entries of a zip by name; fails closed on anything but a plain, safe zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            return {info.filename: archive.read(info) for info in _checked_entries(archive, label)}
    except _READ_ERRORS as exc:
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError(f"{label} is not a readable zip: {exc}") from exc


def listing_sha256(files: dict[str, bytes]) -> str:
    listing = "".join(f"{sha256_hex(files[name])}  {name}\n" for name in sorted(files))
    return sha256_hex(listing.encode("utf-8"))


def archive_content_sha256(body: bytes) -> str:
    return listing_sha256(zip_files(body, "BSR archive"))


def nupkg_protos(body: bytes) -> dict[str, bytes]:
    """The .proto files a nupkg ships under proto/, keyed by their module path."""
    nupkg = zip_files(body, "Geospatial.Grpc 1.0.0 nupkg")
    return {
        name.removeprefix("proto/"): data
        for name, data in nupkg.items()
        if name.startswith("proto/") and name.endswith(".proto")
    }


def verify(body: bytes, expected_content_sha256: str, pinned_protos: dict[str, bytes]) -> dict:
    """Return {content_sha256, file_count} when the archive carries exactly the pinned module."""
    files = zip_files(body, "BSR archive")
    content = listing_sha256(files)
    if content != expected_content_sha256:
        raise ArchiveError(f"content sha256 {content} does not match retained {expected_content_sha256}")
    archive_protos = {name: data for name, data in files.items() if name.endswith(".proto")}
    if not archive_protos or archive_protos != pinned_protos:
        raise ArchiveError("protos do not equal the pinned Geospatial.Grpc 1.0.0 nupkg protos")
    return {"content_sha256": content, "file_count": len(files)}
