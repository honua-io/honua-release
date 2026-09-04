import json
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "collect_candidate_evidence", Path(__file__).with_name("collect-candidate-evidence.py")
)
_module = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_module)
collect = _module.collect


SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _write(root: Path, name: str, *, digest: str = DIGEST, source: str = SHA, generated: str = "2026-09-04T11:00:00Z") -> None:
    folder = root / name
    folder.mkdir()
    receipt = folder / "receipt.json"
    receipt.write_text(json.dumps({
        "candidate": {"image_digest": digest, "source_sha": source},
        "generatedAt": generated,
    }) + "\n")
    (folder / "metadata.json").write_text(json.dumps({
        "id": name, "runId": "1", "runAttempt": "1", "artifactId": "2", "receipt": str(receipt),
    }) + "\n")


def test_all_required_receipts_bound_to_candidate_pass(tmp_path):
    for name in ("realtime", "sdk-python", "protocol-grpc", "protocol-mcp"):
        _write(tmp_path, name)
    result = collect(tmp_path, digest=DIGEST, source_sha=SHA, now=NOW)
    assert result["status"] == "pass"


def test_wrong_digest_fails_closed(tmp_path):
    for name in ("realtime", "sdk-python", "protocol-grpc", "protocol-mcp"):
        _write(tmp_path, name, digest=("sha256:" + "c" * 64 if name == "realtime" else DIGEST))
    result = collect(tmp_path, digest=DIGEST, source_sha=SHA, now=NOW)
    row = next(row for row in result["evidence"] if row["id"] == "realtime")
    assert result["status"] == "fail"
    assert any("does not name candidate digest" in error for error in row["errors"])


def test_conflicting_source_revision_fails_closed(tmp_path):
    for name in ("realtime", "sdk-python", "protocol-grpc", "protocol-mcp"):
        _write(tmp_path, name)
    receipt_path = tmp_path / "realtime" / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["cells"] = [{"source_sha": "c" * 40}]
    receipt_path.write_text(json.dumps(receipt) + "\n")

    result = collect(tmp_path, digest=DIGEST, source_sha=SHA, now=NOW)
    row = next(row for row in result["evidence"] if row["id"] == "realtime")
    assert result["status"] == "fail"
    assert any("cells[0].source_sha" in error for error in row["errors"])


def test_missing_receipt_fails_closed(tmp_path):
    for name in ("realtime", "sdk-python", "protocol-grpc"):
        _write(tmp_path, name)
    result = collect(tmp_path, digest=DIGEST, source_sha=SHA, now=NOW)
    assert result["status"] == "fail"
    missing = next(row for row in result["evidence"] if row["id"] == "protocol-mcp")
    assert "required post-candidate receipt is missing" in missing["errors"]


def test_stale_receipt_fails_closed(tmp_path):
    for name in ("realtime", "sdk-python", "protocol-grpc", "protocol-mcp"):
        _write(tmp_path, name, generated="2026-09-02T11:00:00Z")
    result = collect(tmp_path, digest=DIGEST, source_sha=SHA, now=NOW)
    assert result["status"] == "fail"
    assert all("receipt is stale" in error for row in result["evidence"] for error in row["errors"])
