from pathlib import Path

import pytest

from digests import ascii_canonical_bytes, file_sha256_digest, file_sha256_hex, sha256_digest, sha256_hex

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_byte_digests_are_lowercase_hex_with_and_without_prefix():
    assert sha256_hex(b"") == EMPTY_SHA256
    assert sha256_digest(b"") == "sha256:" + EMPTY_SHA256


@pytest.mark.parametrize("data", [b"", b"receipt", bytes(range(256)) * 9000])
def test_file_digests_match_the_bytes_they_stream(tmp_path, data):
    path = tmp_path / "artifact.bin"
    path.write_bytes(data)
    assert file_sha256_hex(path) == sha256_hex(data)
    assert file_sha256_digest(path) == sha256_digest(data)


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        file_sha256_hex(Path("/nonexistent/receipt.json"))


def test_ascii_canonical_bytes_sort_keys_compact_and_escape_non_ascii():
    assert ascii_canonical_bytes({"b": [1, 2.5], "a": "café"}) == b'{"a":"caf\\u00e9","b":[1,2.5]}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_ascii_canonical_bytes_refuses_non_finite_numbers(value):
    with pytest.raises(ValueError):
        ascii_canonical_bytes({"value": value})
