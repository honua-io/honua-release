import importlib.util
import stat
import zipfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("extract_capacity_evidence.py")
SPEC = importlib.util.spec_from_file_location("extract_capacity_evidence", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def bundle(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return path


def test_extracts_receipt_and_raw_artifacts(tmp_path):
    value = bundle(
        tmp_path / "evidence.zip",
        {gate.RECEIPT_NAME: b"{}", "requests.json": b"requests"},
    )
    extracted = gate.extract(value, tmp_path / "out")
    assert {item.name for item in extracted} == {gate.RECEIPT_NAME, "requests.json"}


@pytest.mark.parametrize("name", ["../escape", "/absolute", "nested/file", "..\\escape", "C:escape", "NUL.json", "CON", "trailing."])
def test_rejects_path_traversal_and_nested_members(tmp_path, name):
    value = bundle(
        tmp_path / "evidence.zip",
        {gate.RECEIPT_NAME: b"{}", name: b"unsafe"},
    )
    with pytest.raises(ValueError, match="unsafe"):
        gate.extract(value, tmp_path / "out")


def test_rejects_symlink_member(tmp_path):
    path = tmp_path / "evidence.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(gate.RECEIPT_NAME, b"{}")
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b"target")
    with pytest.raises(ValueError, match="unsafe"):
        gate.extract(path, tmp_path / "out")


def test_receipt_is_mandatory(tmp_path):
    value = bundle(tmp_path / "evidence.zip", {"metrics.json": b"{}"})
    with pytest.raises(ValueError, match=gate.RECEIPT_NAME):
        gate.extract(value, tmp_path / "out")


def test_rejects_case_collisions_on_windows(tmp_path):
    value = bundle(tmp_path / 'evidence.zip', {gate.RECEIPT_NAME: b'{}', 'DATA.json': b'1', 'data.json': b'2'})
    with pytest.raises(ValueError, match='unsafe'):
        gate.extract(value, tmp_path / 'out')
